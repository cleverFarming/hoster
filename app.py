"""AI智慧农业 —— Flask 后端（SSE 流式 + 工具调用 + DeepSeek 推理模式）"""

import os, json, threading, uuid, sqlite3
from flask import Flask, request, Response, jsonify, stream_with_context, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from tools import (
    TOOL_DEFS, call_tool, init_db, TOOL_DISPLAY_NAMES,
    connect_rosbridge, disconnect_rosbridge, is_rosbridge_connected,
)
from tools._constants import DB_PATH

try:
    from dotenv import load_dotenv
    load_dotenv()  # 项目根目录 .env
    load_dotenv(".venv/.env")  # 也支持 .venv 下的 .env
except ImportError:
    pass

# ═══════════════════ 初始化 ═══════════════════
init_db()
# 连接 rosbridge WebSocket（ROS2 机器人）
# 环境变量 ROSBRIDGE_URL 可覆盖默认地址 ws://localhost:9090
connect_rosbridge()

API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("API_BASE", "https://api.deepseek.com")
MODEL    = os.environ.get("MODEL", "deepseek-chat")

# 推理模式专用模型（DeepSeek-R1 系列支持推理）
REASONING_MODEL = os.environ.get("REASONING_MODEL", "deepseek-reasoner")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

SYSTEM = """你是一个 AI 智能助手，可以与用户进行自然对话，并通过工具与机器人交互。

你的能力：
1. 读写系统操作日志
2. 机器人相关：获取位置、时间、激光雷达/障碍物、IMU 姿态、关节状态、地图元数据
3. 机器人指令：启动/停止底盘、相机、雷达、跟随、建图、导航等（需机器人运行 command_launcher）
4. 植物识别：通过摄像头拍照识别画面中的植物/花卉/草本，给出名称、特征、养护建议

始终使用中文回复。"""

# ═══════════════════ Flask ═══════════════════

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# 内存会话存储
# {
#   session_id: {
#     "msgs": [...],
#     "tool_names": {...},
#     "reasoning": False,          # 是否开启推理模式
#     "reasoning_effort": "medium" # 推理力度: low / medium / high
#   }
# }
sessions: dict[str, dict] = {}
sessions_lock = threading.Lock()
abort_flags: dict[str, bool] = {}          # 中途停止标志

# ═══════════════════ 前端托管 ═══════════════════

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ═══════════════════ 工具函数 ═══════════════════

def get_session(sid: str) -> dict:
    with sessions_lock:
        if sid not in sessions:
            sessions[sid] = {
                "msgs": [{"role": "system", "content": SYSTEM}],
                "tool_names": {},
                "reasoning": False,
                "reasoning_effort": "medium",
            }
        return sessions[sid]


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ═══════════════════ 会话持久化 ═══════════════════

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _save_conversation(sid: str, title: str = "新对话",
                       reasoning: bool = False,
                       reasoning_effort: str = "medium"):
    """创建或更新会话元数据"""
    with _db_conn() as conn:
        conn.execute(
            """INSERT INTO conversations (id, title, reasoning, reasoning_effort)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 updated_at = datetime('now', 'localtime'),
                 reasoning = excluded.reasoning,
                 reasoning_effort = excluded.reasoning_effort""",
            (sid, title, int(reasoning), reasoning_effort),
        )


def _save_message(sid: str, role: str, content: str = None,
                  reasoning_content: str = None,
                  tool_calls: list = None,
                  tool_call_id: str = None,
                  display_name: str = None):
    """持久化单条消息"""
    with _db_conn() as conn:
        conn.execute(
            """INSERT INTO conversation_messages
               (conversation_id, role, content, reasoning_content,
                tool_calls, tool_call_id, display_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, role, content, reasoning_content,
             json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
             tool_call_id, display_name),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now', 'localtime') WHERE id = ?",
            (sid,),
        )



COMPACT_THRESHOLD = 50   # 消息条数超过此值时触发压缩
COMPACT_KEEP      = 10   # 压缩后保留最近 N 条原始消息


def _compact_messages(session: dict):
    """
    当消息条数超过 COMPACT_THRESHOLD 时，用大模型将较早的消息
    压缩为一条摘要，只保留最近 COMPACT_KEEP 条原始消息。
    """
    msgs = session["msgs"]
    # msgs[0] 是 system prompt，不计入
    if len(msgs) - 1 <= COMPACT_THRESHOLD:
        return

    system_msg = msgs[0]
    # 保留最近 COMPACT_KEEP 条，其余交给模型总结
    old_msgs = msgs[1:-COMPACT_KEEP]
    recent_msgs = msgs[-COMPACT_KEEP:]

    # 构造摘要请求（仅用 user/assistant 的文本内容，忽略工具细节）
    summary_lines = []
    for m in old_msgs:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "user" and content:
            summary_lines.append(f"用户: {content[:200]}")
        elif role == "assistant" and content:
            summary_lines.append(f"助手: {content[:200]}")
        elif role == "tool" and content:
            display = m.get("display_name", "工具")
            summary_lines.append(f"[{display}调用结果]: {content[:100]}")

    if not summary_lines:
        return

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": (
                    "你是对话摘要助手。请将以下对话历史压缩为一段简洁的中文摘要，"
                    "保留关键信息（用户意图、重要数据、已执行的操作和结果），"
                    "省略寒暄和重复内容。直接输出摘要，不要加前缀。"
                )},
                {"role": "user", "content": "\n".join(summary_lines)},
            ],
            stream=False,
        )
        summary = resp.choices[0].message.content.strip()
    except Exception:
        # 摘要失败则不压缩，等下次再试
        return

    # 重建消息列表：system + 摘要 + 最近消息
    compact_msg = {
        "role": "user",
        "content": f"[以下是之前对话的摘要]\n{summary}\n[摘要结束，以下是最近的对话]",
    }
    session["msgs"] = [system_msg, compact_msg] + recent_msgs


def _build_llm_kwargs(session: dict) -> dict:
    """
    根据会话设置构建 LLM 调用参数。
    推理模式与普通模式的参数差异在此统一处理。
    """
    # 超过阈值时先压缩上下文
    _compact_messages(session)

    reasoning_on = session.get("reasoning", False)

    kwargs = {
        "model":    REASONING_MODEL if reasoning_on else MODEL,
        "messages": session["msgs"],
        "stream":   True,
    }

    if reasoning_on:
        # ── DeepSeek 推理模式参数 ──
        # deepseek-reasoner 支持工具调用（2025年起）
        # 传入 tools 定义，让模型在推理后决定是否调用工具
        kwargs["tools"] = TOOL_DEFS

        # 推理力度控制（如果 API 支持）
        # DeepSeek R1 系列可通过 extra_body 传递
        effort = session.get("reasoning_effort", "medium")
        kwargs["extra_body"] = {
            "reasoning_effort": effort,
        }
    else:
        # ── 普通模式 ──
        kwargs["tools"] = TOOL_DEFS

    return kwargs


# ═══════════════════ API 路由 ═══════════════════

@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "model": MODEL,
        "reasoning_model": REASONING_MODEL,
        "rosbridge_connected": is_rosbridge_connected(),
    })


@app.post("/api/session")
def create_session():
    sid = str(uuid.uuid4())
    get_session(sid)
    # 不立即写入数据库，等第一条消息时再持久化
    return jsonify({"session_id": sid})


@app.delete("/api/session/<sid>")
def delete_session(sid: str):
    with sessions_lock:
        sessions.pop(sid, None)
    with _db_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (sid,))
    return jsonify({"ok": True})


@app.delete("/api/conversations")
def clear_all_conversations():
    """清空全部历史会话"""
    with sessions_lock:
        sessions.clear()
    with _db_conn() as conn:
        conn.execute("DELETE FROM conversation_messages")
        conn.execute("DELETE FROM conversations")
    return jsonify({"ok": True})


@app.get("/api/session/<sid>/messages")
def get_messages(sid: str):
    session = get_session(sid)
    msgs = [m for m in session["msgs"] if m["role"] != "system"]
    enriched = []
    for m in msgs:
        em = dict(m)
        if m["role"] == "tool":
            tid = m.get("tool_call_id", "")
            raw_name = session["tool_names"].get(tid, "")
            em["display_name"] = TOOL_DISPLAY_NAMES.get(raw_name, raw_name or "工具")
        enriched.append(em)
    return jsonify({
        "messages": enriched,
        "reasoning": session.get("reasoning", False),
        "reasoning_effort": session.get("reasoning_effort", "medium"),
    })


# ── 推理模式控制端点 ──

@app.get("/api/session/<sid>/reasoning")
def get_reasoning(sid: str):
    """查询当前会话的推理模式状态"""
    session = get_session(sid)
    return jsonify({
        "reasoning": session.get("reasoning", False),
        "reasoning_effort": session.get("reasoning_effort", "medium"),
        "reasoning_model": REASONING_MODEL,
    })


@app.post("/api/session/<sid>/reasoning")
def set_reasoning(sid: str):
    """
    开启/关闭推理模式
    Body: { "reasoning": true/false, "reasoning_effort": "low"|"medium"|"high" }
    """
    session = get_session(sid)
    body = request.get_json(force=True)

    if "reasoning" in body:
        session["reasoning"] = bool(body["reasoning"])

    if "reasoning_effort" in body:
        effort = body["reasoning_effort"]
        if effort in ("low", "medium", "high"):
            session["reasoning_effort"] = effort
        else:
            return jsonify({"error": "reasoning_effort 必须是 low/medium/high"}), 400

    # 同步到数据库
    _save_conversation(sid, reasoning=session["reasoning"],
                       reasoning_effort=session["reasoning_effort"])

    return jsonify({
        "reasoning": session["reasoning"],
        "reasoning_effort": session["reasoning_effort"],
    })


# ── 会话历史 ──

@app.get("/api/conversations")
def list_conversations():
    """列出所有历史会话，按更新时间倒序"""
    with _db_conn() as conn:
        rows = conn.execute(
            """SELECT id, title, created_at, updated_at
               FROM conversations ORDER BY updated_at DESC"""
        ).fetchall()
    return jsonify({
        "conversations": [dict(r) for r in rows],
    })


@app.get("/api/conversations/<sid>")
def load_conversation(sid: str):
    """加载指定会话的完整消息历史"""
    with _db_conn() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (sid,)
        ).fetchone()
        if not conv:
            return jsonify({"error": "会话不存在"}), 404

        rows = conn.execute(
            """SELECT role, content, reasoning_content, tool_calls,
                      tool_call_id, display_name
               FROM conversation_messages
               WHERE conversation_id = ?
               ORDER BY id""",
            (sid,),
        ).fetchall()

    # 恢复内存会话
    session = get_session(sid)
    session["reasoning"] = bool(conv["reasoning"])
    session["reasoning_effort"] = conv["reasoning_effort"]

    # 重建 msgs（LLM 上下文）
    msgs = [{"role": "system", "content": SYSTEM}]
    enriched = []  # 前端展示用
    for r in rows:
        msg = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"])
        if r["reasoning_content"]:
            msg["reasoning_content"] = r["reasoning_content"]
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        msgs.append(msg)

        em = dict(msg)
        if r["display_name"]:
            em["display_name"] = r["display_name"]
        enriched.append(em)

    session["msgs"] = msgs

    return jsonify({
        "conversation": dict(conv),
        "messages": enriched,
        "reasoning": session["reasoning"],
        "reasoning_effort": session["reasoning_effort"],
    })


@app.patch("/api/conversations/<sid>")
def rename_conversation(sid: str):
    """重命名会话"""
    body = request.get_json(force=True)
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "标题不能为空"}), 400
    with _db_conn() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (title, sid),
        )
    return jsonify({"ok": True, "title": title})


# ── 停止生成 ──

@app.post("/api/chat/stop")
def stop_chat():
    body = request.get_json(force=True)
    sid = body.get("session_id", "")
    if sid:
        abort_flags[sid] = True
    return jsonify({"ok": True})


# ── 主聊天端点 ──

@app.post("/api/chat")
def chat():
    """
    SSE 事件类型：
      reasoning_delta  { content }              ← 推理过程流式片段（仅推理模式）
      reasoning_done   { content }              ← 推理过程完整文本（仅推理模式）
      text_delta       { content }
      text_done        { content }
      tool_start       { id, name, display_name, args }
      tool_done        { id, name, display_name, result }
      round_done       {}
      done             { reasoning_enabled }
      error            { message }
    """
    if not API_KEY:
        return jsonify({"error": "未配置 DEEPSEEK_API_KEY"}), 500

    body    = request.get_json(force=True)
    sid     = body.get("session_id", "")
    content = body.get("content", "").strip()

    if not sid or not content:
        return jsonify({"error": "缺少 session_id 或 content"}), 400

    session = get_session(sid)
    reasoning_on = session.get("reasoning", False)

    session["msgs"].append({"role": "user", "content": content})

    # 首次发消息时才持久化会话（避免空会话写入数据库）
    _save_conversation(sid, title=content[:30].replace("\n", " ").strip() + ("…" if len(content) > 30 else ""),
                       reasoning=session.get("reasoning", False),
                       reasoning_effort=session.get("reasoning_effort", "medium"))
    _save_message(sid, "user", content=content)

    def generate():
        abort_flags.pop(sid, None)          # 重置停止标志

        for _round in range(10):
            if abort_flags.get(sid):
                break

            # ── 构建 LLM 请求参数 ──
            llm_kwargs = _build_llm_kwargs(session)

            try:
                stream = client.chat.completions.create(**llm_kwargs)
            except Exception as e:
                yield sse_event("error", {"message": str(e)})
                return

            full_content     = ""
            full_reasoning   = ""
            tool_calls_dict  = {}

            for chunk in stream:
                # ── 中途停止检查 ──
                if abort_flags.get(sid):
                    try: stream.close()
                    except Exception: pass
                    break

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                # ── 推理内容（reasoning_content）──
                # DeepSeek reasoner 模型在 delta 中通过
                # delta.reasoning_content 返回思考过程
                reasoning_piece = getattr(delta, "reasoning_content", None)
                if reasoning_piece:
                    full_reasoning += reasoning_piece
                    yield sse_event("reasoning_delta", {"content": reasoning_piece})

                # ── 正文内容 ──
                if delta.content:
                    full_content += delta.content
                    yield sse_event("text_delta", {"content": delta.content})

                # ── 工具调用片段 ──
                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": None, "name": None, "arguments": ""
                            }
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] = tc_chunk.id
                        if tc_chunk.function and tc_chunk.function.name:
                            tool_calls_dict[idx]["name"] = tc_chunk.function.name
                        if tc_chunk.function and tc_chunk.function.arguments:
                            tool_calls_dict[idx]["arguments"] += tc_chunk.function.arguments

            # ── 流结束，发送完成事件 ──
            if full_reasoning:
                yield sse_event("reasoning_done", {"content": full_reasoning})

            if full_content:
                yield sse_event("text_done", {"content": full_content})

            # ── 整理工具调用列表 ──
            tool_calls_list = [
                {
                    "id":   tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
                for _, tc in sorted(tool_calls_dict.items())
            ]

            # ── 存入历史 ──
            assistant_msg: dict = {
                "role": "assistant",
                "content": full_content or None,
            }
            if tool_calls_list:
                assistant_msg["tool_calls"] = tool_calls_list
            # 如果有推理内容，也存入消息中（前端回看用）
            if full_reasoning:
                assistant_msg["reasoning_content"] = full_reasoning
            session["msgs"].append(assistant_msg)

            # 持久化助手消息
            _save_message(
                sid, "assistant",
                content=full_content or None,
                reasoning_content=full_reasoning or None,
                tool_calls=tool_calls_list if tool_calls_list else None,
            )

            # ── 无工具调用 → 结束 ──
            if not tool_calls_list:
                yield sse_event("round_done", {})
                break

            # ── 执行工具 ──
            for tc in tool_calls_list:
                if abort_flags.get(sid):
                    break
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                display = TOOL_DISPLAY_NAMES.get(fn_name, fn_name)

                yield sse_event("tool_start", {
                    "id": tc["id"],
                    "name": fn_name,
                    "display_name": display,
                    "args": fn_args,
                })

                result = call_tool(fn_name, fn_args)
                try:
                    result_obj = json.loads(result)
                except Exception:
                    result_obj = result

                yield sse_event("tool_done", {
                    "id": tc["id"],
                    "name": fn_name,
                    "display_name": display,
                    "result": result_obj,
                })

                session["tool_names"][tc["id"]] = fn_name
                session["msgs"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
                _save_message(
                    sid, "tool", content=result,
                    tool_call_id=tc["id"],
                    display_name=TOOL_DISPLAY_NAMES.get(fn_name, fn_name),
                )

            yield sse_event("round_done", {})
            # 继续下一轮，让模型处理工具返回结果

        abort_flags.pop(sid, None)          # 清理停止标志
        yield sse_event("done", {
            "reasoning_enabled": reasoning_on,
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════ 入口 ═══════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)