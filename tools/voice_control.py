"""工具：M2 麦克风语音模块控制

集成 WHEELTEC M2 系列麦克风阵列到 web 应用：
  - 启动/停止语音模块（mic_init + mic_base）
  - 查询语音模块状态
  - 获取最近的语音识别结果
  - 修改唤醒词

M2 模块架构：
  - mic_init.launch.py → 启动 wheeltec_mic（串口通信）+ voice_control（语音识别）
  - base.launch.py → 启动 call_recognition（唤醒控制）+ command_recognition（命令转动作）
  - R818 降噪板通过 /dev/wheeltec_mic 串口（115200）与上位机通信
  - 默认唤醒词："小微小微"
  - 离线命令词识别（讯飞 iFlytek SDK）
"""

import time
import threading

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT


# ═══════════════════ 内部辅助 ═══════════════════


def _get_ros_nodes() -> list:
    """获取当前 ROS2 节点列表"""
    try:
        resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("nodes", [])
    except Exception:
        return []


def _get_ros_topics() -> list:
    """获取当前 ROS2 话题列表"""
    try:
        resp = rosbridge.call_service("/rosapi/topics", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("topics", [])
    except Exception:
        return []


def _start_command(command: str, timeout: float = 15.0) -> dict:
    """通过 command_launcher 启动指令"""
    svc = f"/robot/start_{command}"
    try:
        resp = rosbridge.call_service(svc, args={}, timeout=timeout)
        return {"success": resp.get("success", False), "message": resp.get("message", "")}
    except TimeoutError:
        return {"success": False, "error": f"启动 {command} 超时", "hint": "确认 command_launcher 在运行"}
    except RuntimeError as e:
        err = str(e)
        if "已在运行" in err:
            return {"success": True, "message": f"{command} 已在运行", "already_running": True}
        return {"success": False, "error": err}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _stop_command(command: str) -> dict:
    """通过 command_launcher 停止指令"""
    svc = f"/robot/stop_{command}"
    try:
        resp = rosbridge.call_service(svc, args={}, timeout=ROSBRIDGE_TIMEOUT)
        return {"success": resp.get("success", True), "message": resp.get("message", "")}
    except Exception as e:
        return {"success": False, "error": str(e)}


# 语音模块相关节点和话题特征
MIC_NODE_KEYWORDS = ["wheeltec_mic", "voice_control", "call_recognition", "command_recognition"]
MIC_TOPIC_KEYWORDS = ["/voice_words", "/mic_", "/awake_", "/wheeltec_mic"]


def _is_voice_module_running(nodes: list = None, topics: list = None) -> dict:
    """检测语音模块各组件是否在运行"""
    if nodes is None:
        nodes = _get_ros_nodes()
    if topics is None:
        topics = _get_ros_topics()

    # 检测各个关键节点
    mic_serial = any("wheeltec_mic" in n for n in nodes)
    voice_ctrl = any("voice_control" in n for n in nodes)
    call_recog = any("call_recognition" in n for n in nodes)
    cmd_recog = any("command_recognition" in n for n in nodes)

    # mic_init 组件：wheeltec_mic + voice_control
    mic_init_ok = mic_serial or voice_ctrl
    # base 组件：call_recognition + command_recognition
    mic_base_ok = call_recog or cmd_recog

    return {
        "mic_init_running": mic_init_ok,
        "mic_base_running": mic_base_ok,
        "all_running": mic_init_ok and mic_base_ok,
        "components": {
            "wheeltec_mic_serial": mic_serial,
            "voice_control": voice_ctrl,
            "call_recognition": call_recog,
            "command_recognition": cmd_recog,
        },
    }


# ═══════════════════ 工具 1：启动语音模块 ═══════════════════


@ToolRegistry.register(
    name="start_voice_module",
    display_name="🎙️ 启动语音模块",
    description=(
        "启动 M2 麦克风语音模块。自动完成: 启动 mic_init（串口通信+语音识别）→ 启动 mic_base（唤醒+命令控制）。"
        "当用户要求'启动语音'、'打开麦克风'、'语音控制'、'语音识别'时使用。"
        "启动后默认唤醒词为'小微小微'，说出唤醒词后可以语音控制机器人。"
        "支持的语音命令：小车前进/后退/左转/右转/停止、小车去I/J/K点、"
        "小车雷达跟随、开始建图/关闭建图、开始导航/关闭导航等。"
    ),
    parameters={},
)
def start_voice_module() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    steps_log = []
    nodes = _get_ros_nodes()
    topics = _get_ros_topics()
    status = _is_voice_module_running(nodes, topics)

    # 第1步：启动 mic_init（串口通信 + 语音识别引擎）
    if status["mic_init_running"]:
        steps_log.append("mic_init 已在运行，跳过启动")
    else:
        result = _start_command("mic_init", timeout=20.0)
        if result.get("success"):
            steps_log.append(f"mic_init 已启动: {result.get('message', '')}")
            time.sleep(5)  # 等待串口初始化和离线识别资源加载
        else:
            return {
                "success": False,
                "error": f"启动 mic_init 失败: {result.get('error', '')}",
                "hint": "确认 M2 麦克风已连接到 /dev/wheeltec_mic，command_launcher 在运行",
                "steps": steps_log,
            }

    # 第2步：启动 mic_base（唤醒控制 + 命令识别转动作）
    if status["mic_base_running"]:
        steps_log.append("mic_base 已在运行，跳过启动")
    else:
        result = _start_command("mic_base", timeout=15.0)
        if result.get("success"):
            steps_log.append(f"mic_base 已启动: {result.get('message', '')}")
            time.sleep(3)
        else:
            return {
                "success": False,
                "error": f"启动 mic_base 失败: {result.get('error', '')}",
                "hint": result.get("hint", ""),
                "steps": steps_log,
            }

    return {
        "success": True,
        "steps": steps_log,
        "wake_word": "小微小微",
        "summary_cn": (
            "语音模块已启动！默认唤醒词为'小微小微'。"
            "说出唤醒词后，可以语音命令控制机器人（如'小车前进'、'小车停'等）。"
            "支持的命令：前进/后退/左转/右转/停止、去I/J/K点导航、"
            "雷达跟随、开始建图、开始导航等。"
        ),
    }


# ═══════════════════ 工具 2：停止语音模块 ═══════════════════


@ToolRegistry.register(
    name="stop_voice_module",
    display_name="🔇 停止语音模块",
    description=(
        "停止语音模块。当用户要求'关闭语音'、'停止麦克风'、'关闭语音控制'时使用。"
    ),
    parameters={},
)
def stop_voice_module() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    steps_log = []

    # 先停 mic_base（命令控制），再停 mic_init（底层串口）
    for name in ["mic_base", "mic_init"]:
        result = _stop_command(name)
        msg = result.get("message", "")
        steps_log.append(f"停止 {name}: {msg}")

    return {
        "success": True,
        "steps": steps_log,
        "summary_cn": "语音模块已停止",
    }


# ═══════════════════ 工具 3：语音模块状态 ═══════════════════


@ToolRegistry.register(
    name="check_voice_status",
    display_name="🎙️ 语音模块状态",
    description=(
        "查询语音模块的运行状态：麦克风串口通信、语音识别引擎、唤醒控制、命令识别等。"
        "当用户询问'语音状态'、'麦克风状态'、'语音模块怎么样了'时使用。"
    ),
    parameters={},
)
def check_voice_status() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    nodes = _get_ros_nodes()
    topics = _get_ros_topics()
    status = _is_voice_module_running(nodes, topics)

    # 尝试调用设备信息服务
    device_info = None
    try:
        resp = rosbridge.call_service(
            "/get_device_info",
            args={},
            timeout=5.0,
        )
        if resp:
            device_info = resp.get("result", str(resp))
    except Exception:
        pass

    # 查找语音相关话题
    voice_topics = [t for t in topics if any(kw in t.lower() for kw in
                    ["voice", "mic", "awake", "speech", "asr"])]

    # 组装描述
    parts = []
    comp = status["components"]
    if comp["wheeltec_mic_serial"]:
        parts.append("麦克风串口通信正常")
    else:
        parts.append("麦克风串口未启动")

    if comp["voice_control"]:
        parts.append("语音识别引擎运行中")
    else:
        parts.append("语音识别引擎未启动")

    if comp["call_recognition"]:
        parts.append("唤醒控制运行中")
    else:
        parts.append("唤醒控制未启动")

    if comp["command_recognition"]:
        parts.append("命令识别运行中")
    else:
        parts.append("命令识别未启动")

    if status["all_running"]:
        overall = "语音模块完全运行中"
    elif status["mic_init_running"] or status["mic_base_running"]:
        overall = "语音模块部分运行中"
    else:
        overall = "语音模块未启动"

    return {
        "success": True,
        "overall": overall,
        "all_running": status["all_running"],
        "components": status["components"],
        "device_info": device_info,
        "voice_topics": voice_topics,
        "summary_cn": f"{overall}。" + "；".join(parts),
    }


# ═══════════════════ 工具 4：修改唤醒词 ═══════════════════


@ToolRegistry.register(
    name="set_wake_word",
    display_name="🗣️ 修改唤醒词",
    description=(
        "修改语音模块的唤醒词。需要语音模块已启动。"
        "唤醒词需要用拼音+声调格式，如 'xiao3 wei1 xiao3 wei1'。"
        "当用户要求'改唤醒词'、'换唤醒词'、'设置唤醒词'时使用。"
    ),
    parameters={
        "wake_word_pinyin": {
            "type": "string",
            "description": "唤醒词的拼音+声调，如 'xiao3 wei1 xiao3 wei1'（小微小微）、'ni3 hao3 xiao3 wei1'（你好小微）",
        },
        "threshold": {
            "type": "string",
            "description": "唤醒阈值，默认 '900'，越高越严格（不建议超过 1450）",
            "default": "900",
        },
    },
)
def set_wake_word(wake_word_pinyin: str, threshold: str = "900") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 通过 ROS2 服务修改唤醒词
    try:
        resp = rosbridge.call_service(
            "/set_awake_word",
            args={
                "awake_word": wake_word_pinyin,
                "threshold": threshold,
            },
            timeout=10.0,
        )
        result_msg = resp.get("result", str(resp))
        return {
            "success": True,
            "wake_word_pinyin": wake_word_pinyin,
            "threshold": threshold,
            "response": result_msg,
            "summary_cn": (
                f"唤醒词已修改为 '{wake_word_pinyin}'（阈值 {threshold}）。"
                "注意：修改后设备会自动重启，需要关闭并重新启动语音节点才能使用新唤醒词。"
            ),
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "确认语音模块已启动（先调用 start_voice_module）",
        }


# ═══════════════════ 工具 5：获取语音识别结果 ═══════════════════


@ToolRegistry.register(
    name="listen_voice_command",
    display_name="👂 监听语音命令",
    description=(
        "监听并获取最新的语音识别结果。需要语音模块已启动。"
        "会等待最多 listen_seconds 秒来捕获一条语音命令。"
        "当用户要求'听一下'、'获取语音'、'语音识别结果'时使用。"
    ),
    parameters={
        "listen_seconds": {
            "type": "number",
            "description": "监听时长（秒），默认 10",
            "default": 10,
        },
    },
)
def listen_voice_command(listen_seconds: float = 10) -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 尝试多个可能的语音结果话题
    candidate_topics = [
        ("/voice_words", "std_msgs/msg/String"),
        ("/voice_result", "std_msgs/msg/String"),
        ("/speech_recognition/result", "std_msgs/msg/String"),
    ]

    # 先检查哪些话题实际存在
    topics = _get_ros_topics()
    active_topic = None
    active_type = None

    for topic, msg_type in candidate_topics:
        if topic in topics:
            active_topic = topic
            active_type = msg_type
            break

    # 也查找任何包含 voice/speech/asr 的 String 类型话题
    if not active_topic:
        for t in topics:
            if any(kw in t.lower() for kw in ["voice", "speech", "asr", "recognition"]):
                active_topic = t
                active_type = "std_msgs/msg/String"
                break

    if not active_topic:
        return {
            "success": False,
            "error": "未找到语音识别结果话题",
            "available_topics": [t for t in topics if "mic" in t.lower() or "voice" in t.lower()],
            "hint": "确认语音模块已启动（先调用 start_voice_module）",
        }

    # 订阅话题，等待语音结果
    results = []
    event = threading.Event()

    def _on_msg(msg):
        data = msg.get("data", "") if isinstance(msg, dict) else str(msg)
        if data:
            results.append({
                "text": data,
                "timestamp": time.time(),
            })
            event.set()

    try:
        rosbridge.subscribe(active_topic, active_type, _on_msg, queue_length=5)
        event.wait(timeout=listen_seconds)
    except Exception as e:
        return {"success": False, "error": f"监听失败: {e}"}
    finally:
        try:
            rosbridge.unsubscribe(active_topic)
        except Exception:
            pass

    if results:
        return {
            "success": True,
            "topic": active_topic,
            "results": results,
            "count": len(results),
            "summary_cn": f"监听到 {len(results)} 条语音命令：" + "、".join(
                r["text"] for r in results
            ),
        }
    else:
        return {
            "success": True,
            "topic": active_topic,
            "results": [],
            "count": 0,
            "summary_cn": f"在 {listen_seconds} 秒内未检测到语音命令（确保已说出唤醒词'小微小微'后再说命令）",
        }
