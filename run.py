"""AI智慧农业 —— Streamlit 对话界面（流式思考 + 工具调用提示）"""

import os, json, time
import streamlit as st
from openai import OpenAI
from tools import TOOL_DEFS, call_tool, init_db, TOOL_DISPLAY_NAMES, start_sensor_collector

# ═══════════════════ 初始化 ═══════════════════

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
start_sensor_collector(10)
init_db()

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("API_BASE", "https://api.deepseek.com")
MODEL    = os.environ.get("MODEL", "deepseek-chat")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

SYSTEM = """你是「农智」—— AI 智慧农业助手。
你管理的农场分为 东北、西北、东南、西南 四个区域，每个区域配有温度、湿度、CO₂、光照传感器。

你的能力：
1. 查询任意区域的实时传感器数据或历史趋势
2. 查看某区域所有传感器概览
3. 对指定区域浇水（需指定水量）
4. 读写系统操作日志

请根据传感器数据给出专业农业建议。始终使用中文回复。"""

# ═══════════════════ 页面 ═══════════════════

st.set_page_config(page_title="🌾 AI智慧农业", page_icon="🌾")
st.title("🌾 AI 智慧农业助手")

with st.sidebar:
    st.header("ℹ️ 系统信息")
    st.markdown(
        "**区域** 东北 · 西北 · 东南 · 西南\n\n"
        "**传感器** 温度 · 湿度 · CO₂ · 光照\n\n"
        "**操作** 浇水 · 日志读写"
    )
    st.divider()
    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state.clear()
        st.rerun()

if not API_KEY:
    st.error("⚠️ 请设置环境变量 `DEEPSEEK_API_KEY`，或在 `.env` 中写入。")
    st.stop()

# ═══════════════════ 会话状态 ═══════════════════

if "msgs" not in st.session_state:
    st.session_state.msgs = [{"role": "system", "content": SYSTEM}]
    st.session_state.tool_names = {}

# ═══════════════════ 渲染历史消息 ═══════════════════

def render_history():
    """渲染 st.session_state.msgs 中所有非 system 消息"""
    for m in st.session_state.msgs:
        role = m["role"]
        content = m.get("content")

        if role == "system":
            continue

        elif role == "user":
            st.chat_message("user").write(content)

        elif role == "assistant":
            # assistant 消息可能只有 tool_calls 没有 content
            has_tool_calls = bool(m.get("tool_calls"))
            if content:
                st.chat_message("assistant").write(content)
            # 如果只有 tool_calls 没有 content，不渲染气泡（工具结果会单独显示）

        elif role == "tool":
            name = st.session_state.tool_names.get(m.get("tool_call_id"), "工具")
            display = TOOL_DISPLAY_NAMES.get(name, f"🔧 {name}")
            with st.chat_message("assistant", avatar="🔧"):
                with st.expander(f"{display} — 返回结果", expanded=False):
                    try:
                        st.json(json.loads(content))
                    except Exception:
                        st.code(content)


render_history()

# 引导提示
if len(st.session_state.msgs) == 1:
    st.caption("💡 试试：「查看东南区概况」「东北区过去6小时温度趋势」「给西北区浇水30升」「查看操作日志」")

# ═══════════════════ 用户输入 & 多轮工具调用（流式） ═══════════════════

if prompt := st.chat_input("请输入您的问题…"):
    # 显示并记录用户消息
    st.chat_message("user").write(prompt)
    st.session_state.msgs.append({"role": "user", "content": prompt})

    # 最多进行 10 轮工具调用
    for round_idx in range(10):

        # ── 流式请求 ──
        try:
            stream = client.chat.completions.create(
                model=MODEL,
                messages=st.session_state.msgs,
                tools=TOOL_DEFS,
                stream=True,
            )
        except Exception as e:
            st.error(f"API 调用失败：{e}")
            st.stop()

        # ── 逐 chunk 收集 ──
        full_content = ""
        tool_calls_dict = {}

        # 只在有文本内容时才创建气泡
        text_bubble = None
        text_placeholder = None

        for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue

            # 流式文本：首次出现文本时创建气泡
            if delta.content:
                full_content += delta.content
                if text_bubble is None:
                    text_bubble = st.chat_message("assistant")
                    text_placeholder = text_bubble.empty()
                text_placeholder.markdown(full_content + "▌")

            # 工具调用分片
            if delta.tool_calls:
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": None,
                            "name": None,
                            "arguments": ""
                        }
                    if tc_chunk.id:
                        tool_calls_dict[idx]["id"] = tc_chunk.id
                    if tc_chunk.function and tc_chunk.function.name:
                        tool_calls_dict[idx]["name"] = tc_chunk.function.name
                    if tc_chunk.function and tc_chunk.function.arguments:
                        tool_calls_dict[idx]["arguments"] += tc_chunk.function.arguments

        # ── 流结束：移除光标 ──
        if text_placeholder and full_content:
            text_placeholder.markdown(full_content)

        # ── 构建 assistant 消息 ──
        tool_calls_list = []
        if tool_calls_dict:
            for idx in sorted(tool_calls_dict.keys()):
                tc = tool_calls_dict[idx]
                tool_calls_list.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })

        assistant_msg = {"role": "assistant", "content": full_content or None}
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        st.session_state.msgs.append(assistant_msg)

        # ── 无工具调用 → 结束 ──
        if not tool_calls_list:
            break

        # ── 执行工具调用（每个工具独立气泡） ──
        for tc in tool_calls_list:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])
            display_name = TOOL_DISPLAY_NAMES.get(fn_name, f"🔧 {fn_name}")
            args_hint = "、".join(f"{k}={v}" for k, v in fn_args.items())

            with st.chat_message("assistant", avatar="🔧"):
                with st.status(
                    f"⏳ 正在调用 {display_name}（{args_hint}）…",
                    expanded=False,
                    state="running",
                ) as status_widget:
                    st.write(f"**函数**: `{fn_name}`")
                    st.write(f"**参数**:")
                    st.json(fn_args)

                    result = call_tool(fn_name, fn_args)

                    status_widget.update(
                        label=f"✅ {display_name} — 调用完成",
                        state="complete",
                        expanded=False,
                    )
                    st.write("**返回结果**:")
                    try:
                        st.json(json.loads(result))
                    except Exception:
                        st.code(result)

            st.session_state.tool_names[tc["id"]] = fn_name
            st.session_state.msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })


        # 继续下一轮

    # 所有轮次结束后刷新
    st.rerun()