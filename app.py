"""AI智慧农业 —— Flask 后端（SSE 流式 + 工具调用 + DeepSeek 推理模式）"""

import os, json, threading, uuid
from flask import Flask, request, Response, jsonify, stream_with_context, send_from_directory
from flask_cors import CORS
from openai import OpenAI
from tools import TOOL_DEFS, call_tool, init_db, TOOL_DISPLAY_NAMES, start_sensor_collector

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ═══════════════════ 初始化 ═══════════════════
start_sensor_collector(10)
init_db()

API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("API_BASE", "https://api.deepseek.com")
MODEL    = os.environ.get("MODEL", "deepseek-chat")

# 推理模式专用模型（DeepSeek-R1 系列支持推理）
REASONING_MODEL = os.environ.get("REASONING_MODEL", "deepseek-reasoner")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

SYSTEM = """你是「农智」—— AI 智慧农业助手。
你管理的农场分为 东北、西北、东南、西南 四个区域，每个区域配有温度、湿度、CO₂、光照传感器。

你的能力：
1. 查询任意区域的实时传感器数据或历史趋势
2. 查看某区域所有传感器概览
3. 对指定区域浇水（需指定水量）
4. 读写系统操作日志

请根据传感器数据给出专业农业建议。始终使用中文回复。"""

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


def _build_llm_kwargs(session: dict) -> dict:
    """
    根据会话设置构建 LLM 调用参数。
    推理模式与普通模式的参数差异在此统一处理。
    """
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
    })


@app.post("/api/session")
def create_session():
    sid = str(uuid.uuid4())
    get_session(sid)
    return jsonify({"session_id": sid})


@app.delete("/api/session/<sid>")
def delete_session(sid: str):
    with sessions_lock:
        sessions.pop(sid, None)
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

    return jsonify({
        "reasoning": session["reasoning"],
        "reasoning_effort": session["reasoning_effort"],
    })


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

    def generate():
        for _round in range(10):

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

            # ── 无工具调用 → 结束 ──
            if not tool_calls_list:
                yield sse_event("round_done", {})
                break

            # ── 执行工具 ──
            for tc in tool_calls_list:
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

            yield sse_event("round_done", {})
            # 继续下一轮，让模型处理工具返回结果

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
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)