"""AI智慧农业 —— Flask 后端（SSE 流式 + 工具调用 + DeepSeek 推理模式）"""
from venv import logger

import chromadb
from chromadb.utils import embedding_functions
from datetime import datetime
import hashlib
import logging

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
# logger.addHandler(logging.StreamHandler())


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

API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")#xxKey
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
    conn = sqlite3.connect(DB_PATH) #farm.db农场数据库
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
        ).fetchall() #"execute() - 执行SQL查询"，"FROM conversations - 从conversations表查询",".fetchall() - 获取所有结果行"
    # # 打印调试信息
    # print("=" * 50)
    # print(f"[DEBUG] 查询到 {len(rows)} 条会话记录")
    # for r in rows:
    #     print(f"  - ID: {r['id']}, Title: {r['title']}, Updated: {r['updated_at']}")
    # print("=" * 50)

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

# ── 新增ChromaDB 相关代码 ──
# 初始化 ChromaDB 客户端


#chroma_client = chromadb.PersistentClient(path="./chroma_db")
#embedding_fn = embedding_functions.DefaultEmbeddingFunction()
# 方式1：指定本地模型路径（推荐）
embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="./all-MiniLM-L6-v2",  # 替换为你的实际路径
    device="cpu"  # 或 "cuda" 如果有 GPU
)
# 初始化 ChromaDB 客户端
chroma_client = chromadb.PersistentClient(path="./chroma_db")

# 创建 collection 时指定 embedding 函数
collection = chroma_client.get_or_create_collection(
    name="memory_conversations",
    embedding_function=embedding_fn
)

# 为每个会话创建或获取 collection
def get_memory_collection(sid: str):
    """获取会话的记忆库"""
    collection_name = f"memory_{sid}"
    return chroma_client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_fn
    )

def retrieve_similar_memories(sid: str, query: str, top_k: int = 3):
    """检索相似的对话记忆"""
    try:
        collection = get_memory_collection(sid)
        results = collection.query(
            query_texts=[query],
            n_results=top_k
        )

        memories = []
        if results['documents']:
            for doc, metadata, distance in zip(
                    results['documents'][0],
                    results['metadatas'][0],
                    results['distances'][0]
            ):
                memories.append({
                    'content': doc,
                    'metadata': metadata,
                    'similarity': 1 - distance  # 距离转相似度
                })
        return memories
    except Exception as e:
        logger.error(f"记忆检索失败: {e}")
        return []

def format_memories_for_context(memories: list) -> str:
    """将检索到的记忆格式化为上下文"""
    if not memories:
        return ""

    context = "\n## 相关历史记忆\n"
    for i, mem in enumerate(memories, 1):
        if mem['similarity'] > 0.7:  # 高相似度才注入
            context += f"- {mem['content']}\n"
    return context

def inject_memory_to_session(session, memory_context: str):
    """将记忆注入到会话上下文中"""
    if not memory_context:
        return

    # 方法1：修改最后一条系统消息
    for i, msg in enumerate(session["msgs"]):
        if msg["role"] == "system":
            if "[记忆上下文]" not in msg["content"]:
                session["msgs"][i]["content"] += f"\n\n{memory_context}"
            return

    # 方法2：如果没有系统消息，插入一条
    session["msgs"].insert(0, {
        "role": "system",
        "content": f"你是AI助手。以下是相关的历史对话记忆：\n{memory_context}"
    })

def save_conversation_to_memory(sid: str, user_query: str, assistant_response: str,
                                reasoning: str = None, tool_calls: list = None,
                                metadata: dict = None):
    """将对话保存到 ChromaDB 作为长期记忆"""
    try:
        collection = get_memory_collection(sid)

        # 构建要保存的文本（问题和回答的组合）
        memory_text = f"用户问：{user_query}\n助手答：{assistant_response}"

        # 如果有推理过程，也加入
        if reasoning:
            memory_text += f"\n推理过程：{reasoning}"

        # 准备元数据
        doc_metadata = {
            "timestamp": datetime.now().isoformat(),
            "user_query": user_query[:200],  # 截断防止过长
            "assistant_response": assistant_response[:200],
            "has_reasoning": bool(reasoning),
            "has_tool_calls": bool(tool_calls),
            **(metadata or {})
        }

        # 生成唯一ID（使用时间戳+哈希）
        doc_id = f"{int(datetime.now().timestamp())}_{hashlib.md5(user_query.encode()).hexdigest()[:8]}"

        # 插入到 ChromaDB
        collection.add(
            documents=[memory_text],
            metadatas=[doc_metadata],
            ids=[doc_id]
        )

        logger.info(f"已保存对话记忆: {doc_id}")

    except Exception as e:
        logger.error(f"保存到 ChromaDB 失败: {e}")

# 可选：定期清理旧记忆或限制记忆数量
def cleanup_old_memories(sid: str, keep_count: int = 100):
    """清理旧的记忆，只保留最近的 keep_count 条"""
    try:
        collection = get_memory_collection(sid)
        # ChromaDB 需要按 ID 删除，需要先获取所有 ID
        all_ids = collection.get()['ids']
        if len(all_ids) > keep_count:
            ids_to_delete = all_ids[:-keep_count]
            collection.delete(ids=ids_to_delete)
            logger.info(f"清理了 {len(ids_to_delete)} 条旧记忆")
    except Exception as e:
        logger.error(f"清理记忆失败: {e}")

def inject_memories_to_context(session: dict, memories: list):
    """
    将检索到的相似记忆注入到对话上下文中

    参数:
        session: 当前会话对象
        memories: 检索到的记忆列表，格式为 [{'content': str, 'similarity': float, 'metadata': dict}]
    """
    if not memories:
        return

    # 1. 格式化记忆为可读文本
    memory_context = format_memories_for_context(memories)

    if not memory_context:
        return

    # 2. 注入到系统消息中
    injected = False

    for i, msg in enumerate(session["msgs"]):
        if msg["role"] == "system":
            # 检查是否已经注入过相似记忆（避免重复）
            if "[相关记忆]" in msg["content"]:
                # 更新已有的记忆部分
                lines = msg["content"].split("\n")
                new_lines = []
                skip_memory_section = False

                for line in lines:
                    if line.startswith("## 相关历史记忆"):
                        skip_memory_section = True
                        new_lines.append(memory_context)
                    elif skip_memory_section and line.startswith("##"):
                        skip_memory_section = False
                        new_lines.append(line)
                    elif not skip_memory_section:
                        new_lines.append(line)

                session["msgs"][i]["content"] = "\n".join(new_lines)
            else:
                # 追加到现有系统消息
                session["msgs"][i]["content"] += f"\n\n{memory_context}"

            injected = True
            break

    # 3. 如果没有系统消息，创建一条
    if not injected:
        system_msg = {
            "role": "system",
            "content": f"""你是AI助手，请基于以下相关历史记忆来回答用户问题。

{memory_context}

注意：这些是用户的历史对话记录，可以作为参考来提供更连贯和个性化的回答。"""
        }
        session["msgs"].insert(0, system_msg)

    logger.debug(f"已注入 {len(memories)} 条相关记忆到上下文")

def format_memories_for_context(memories: list, max_memories: int = 3) -> str:
    """
    将记忆列表格式化为适合注入上下文的文本

    参数:
        memories: 记忆列表
        max_memories: 最大注入数量
    """
    if not memories:
        return ""

    # 过滤低相似度的记忆（可选）
    relevant_memories = [
        m for m in memories
        if m.get('similarity', 0) > 0.6  # 只使用相似度高于0.6的记忆
    ]

    if not relevant_memories:
        relevant_memories = memories[:max_memories]
    else:
        relevant_memories = relevant_memories[:max_memories]

    context_parts = ["## 相关历史记忆\n"]
    context_parts.append("以下是用户之前的对话记录，可能对回答当前问题有帮助：\n")

    for i, mem in enumerate(relevant_memories, 1):
        content = mem.get('content', '')
        similarity = mem.get('similarity', 0)

        # 解析记忆内容（假设格式为 "用户问：xxx\n助手答：xxx"）
        parts = content.split('\n助手答：')
        if len(parts) == 2:
            user_question = parts[0].replace('用户问：', '')
            assistant_answer = parts[1][:150]  # 截断过长内容

            context_parts.append(f"{i}. 用户曾问：{user_question}")
            context_parts.append(f"   你曾回答：{assistant_answer}{'...' if len(parts[1]) > 150 else ''}")
            context_parts.append(f"   (相关度: {similarity:.2%})\n")
        else:
            # 如果格式不标准，直接使用原内容
            context_parts.append(f"{i}. {content[:200]}...\n")

    return "\n".join(context_parts)

def inject_memories_to_context_advanced(session: dict, memories: list, strategy: str = "system"):
    """
    高级版本：支持多种注入策略

    策略:
        - "system": 注入到系统消息（默认）
        - "user_prefix": 在用户消息前添加
        - "assistant_prefix": 在助手消息前添加
        - "separate": 作为单独的消息插入
    """
    if not memories:
        return

    memory_context = format_memories_for_context(memories)

    if strategy == "system":
        # 策略1：注入到系统消息
        inject_to_system_message(session, memory_context)

    elif strategy == "user_prefix":
        # 策略2：在最后一条用户消息前添加
        for i in range(len(session["msgs"]) - 1, -1, -1):
            if session["msgs"][i]["role"] == "user":
                original_content = session["msgs"][i]["content"]
                session["msgs"][i]["content"] = f"{memory_context}\n\n{original_content}"
                break

    elif strategy == "assistant_prefix":
        # 策略3：在助手回复前添加（作为提示）
        session["msgs"].append({
            "role": "assistant",
            "content": f"根据历史记忆：{memory_context}"
        })

    elif strategy == "separate":
        # 策略4：作为单独的消息插入
        session["msgs"].insert(-1, {
            "role": "system",
            "content": f"以下是从长期记忆中检索到的相关信息：\n{memory_context}"
        })

def inject_to_system_message(session: dict, memory_context: str):
    """辅助函数：注入到系统消息"""
    for i, msg in enumerate(session["msgs"]):
        if msg["role"] == "system":
            if "[相关记忆]" in msg["content"]:
                # 更新已有记忆
                lines = msg["content"].split("\n")
                new_lines = []
                in_memory_section = False

                for line in lines:
                    if line.startswith("## 相关历史记忆"):
                        in_memory_section = True
                        new_lines.append(memory_context)
                    elif in_memory_section and (line.startswith("##") or not line.strip()):
                        in_memory_section = False
                        new_lines.append(line)
                    elif not in_memory_section:
                        new_lines.append(line)

                session["msgs"][i]["content"] = "\n".join(new_lines)
            else:
                session["msgs"][i]["content"] += f"\n\n{memory_context}"
            return

    # 没有系统消息，创建一条
    session["msgs"].insert(0, {
        "role": "system",
        "content": f"你是AI助手。\n\n{memory_context}"
    })


# ── 主聊天端点 ──

@app.post("/api/chat")
def chat(): #使用 SSE（Server-Sent Events，服务器推送事件）实现流式对话的 API
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
    # 检查 API Key
    if not API_KEY:
        return jsonify({"error": "未配置 DEEPSEEK_API_KEY"}), 500

    # 获取请求参数
    body    = request.get_json(force=True)
    sid     = body.get("session_id", "")
    content = body.get("content", "").strip()

    # 验证必填参数
    if not sid or not content:
        return jsonify({"error": "缺少 session_id 或 content"}), 400

    # 获取会话配置
    session = get_session(sid)
    reasoning_on = session.get("reasoning", False)

    # 保存用户消息到内存
    session["msgs"].append({"role": "user", "content": content})

    # 首次发消息时才持久化会话（避免空会话写入数据库）
    _save_conversation(sid, title=content[:30].replace("\n", " ").strip() + ("…" if len(content) > 30 else ""),
                       reasoning=session.get("reasoning", False),
                       reasoning_effort=session.get("reasoning_effort", "medium"))
    _save_message(sid, "user", content=content)

    def generate():
        abort_flags.pop(sid, None)          # 重置停止标志
        # 🆕 预加载记忆（可选）
        #preload_memories_to_session(session, sid)

        for _round in range(10):
            if abort_flags.get(sid):
                break

            # 🆕 1. 检索相似记忆
            similar_memories = retrieve_similar_memories(sid, content)
            if similar_memories:
                inject_memories_to_context(session, similar_memories)


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

            # 🆕 5. 保存到 ChromaDB（长期记忆）
            save_conversation_to_memory(
                sid=sid,
                user_query=content,
                assistant_response=full_content,
                reasoning=full_reasoning,
                tool_calls=tool_calls_list
            )

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