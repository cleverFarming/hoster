"""工具：通过 rosbridge 订阅 /joint_states 获取关节/轮子状态"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

JOINT_TOPICS = [
    ("/joint_states", "sensor_msgs/msg/JointState"),
]


def _parse_joint_states(msg: dict) -> dict:
    """解析 sensor_msgs/JointState"""
    names = msg.get("name", []) or []
    positions = msg.get("position", []) or []
    velocities = msg.get("velocity", []) or []
    efforts = msg.get("effort", []) or []

    joints = []
    for i, name in enumerate(names):
        pos = positions[i] if i < len(positions) else None
        vel = velocities[i] if i < len(velocities) else None
        eff = efforts[i] if i < len(efforts) else None
        joints.append({
            "name": name,
            "position": round(pos, 4) if pos is not None else None,
            "velocity": round(vel, 4) if vel is not None else None,
            "effort": round(eff, 4) if eff is not None else None,
        })

    return {
        "frame_id": msg.get("header", {}).get("frame_id", ""),
        "joints": joints,
        "count": len(joints),
    }


@ToolRegistry.register(
    name="get_joint_states",
    display_name="🦿 关节/轮子状态",
    description="获取机器人关节和轮子的当前位置、速度。当用户询问轮子状态、关节角度、机械臂姿态时使用",
    parameters={},
    required=[],
)
def get_joint_states() -> dict:
    """订阅 /joint_states 获取 JointState 消息"""
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    for topic, msg_type in JOINT_TOPICS:
        try:
            msg = rosbridge.subscribe_once(topic, msg_type, timeout=ROSBRIDGE_TIMEOUT)
            if msg:
                parsed = _parse_joint_states(msg)
                summary_parts = [f"{j['name']}={j.get('position', '?')}" for j in parsed["joints"][:6]]
                summary = ", ".join(summary_parts)
                if len(parsed["joints"]) > 6:
                    summary += f" ... 共{parsed['count']}个关节"
                return {
                    "success": True,
                    "topic": topic,
                    **parsed,
                    "summary_cn": summary,
                }
        except ConnectionError as e:
            return {"success": False, "error": str(e)}
        except Exception:
            continue

    return {"success": False, "error": "未收到关节数据", "hint": "请确认 /joint_states 存在"}
