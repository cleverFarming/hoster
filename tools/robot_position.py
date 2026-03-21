"""工具：通过 rosbridge 订阅 /odom 获取机器人当前位置"""

import math

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT


# 优先尝试的里程计 topic（按顺序）
ODOM_TOPICS = [
    ("/odom", "nav_msgs/msg/Odometry"),
    ("/odom_combined", "nav_msgs/msg/Odometry"),
]


def _quat_to_yaw_deg(ox: float, oy: float, oz: float, ow: float) -> float:
    """四元数转航向角（度）"""
    yaw = math.atan2(2.0 * (ow * oz + ox * oy), 1.0 - 2.0 * (oy * oy + oz * oz))
    return math.degrees(yaw)


def _parse_odom(msg: dict) -> dict:
    """解析 nav_msgs/Odometry 消息为可读结构"""
    pose = msg.get("pose", {}).get("pose", {}) or {}
    pos = pose.get("position", {}) or {}
    ori = pose.get("orientation", {}) or {}

    x = pos.get("x", 0) or 0
    y = pos.get("y", 0) or 0
    z = pos.get("z", 0) or 0
    ox = ori.get("x", 0) or 0
    oy = ori.get("y", 0) or 0
    oz = ori.get("z", 0) or 0
    ow = ori.get("w", 1) or 1

    yaw_deg = _quat_to_yaw_deg(ox, oy, oz, ow)
    frame = msg.get("header", {}).get("frame_id", "odom")

    return {
        "frame_id": frame,
        "position": {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)},
        "yaw_deg": round(yaw_deg, 1),
        "position_cn": f"x={x:.2f}m, y={y:.2f}m, z={z:.2f}m",
        "heading_cn": f"朝向 {yaw_deg:.1f}°",
    }


@ToolRegistry.register(
    name="get_robot_position",
    display_name="📍 获取机器人位置",
    description="获取机器人当前位姿（通过订阅 /odom 或 /odom_combined）。当用户询问机器人位置、在哪里、当前坐标时使用",
    parameters={},
    required=[],
)
def get_robot_position() -> dict:
    """
    通过 rosbridge 订阅里程计 topic，解析 nav_msgs/Odometry 返回位置。
    无需机器人端 /farm/* 服务，只要有 /odom 或 /odom_combined 即可。
    """
    if not is_rosbridge_connected():
        return {
            "success": False,
            "error": "rosbridge 未连接",
            "hint": "请检查 ROSBRIDGE_URL 环境变量及机器人端 rosbridge 是否启动",
        }

    for topic, msg_type in ODOM_TOPICS:
        try:
            msg = rosbridge.subscribe_once(
                topic, msg_type, timeout=ROSBRIDGE_TIMEOUT
            )
            if msg:
                parsed = _parse_odom(msg)
                return {
                    "success": True,
                    "topic": topic,
                    **parsed,
                    "summary_cn": f"位置 {parsed['position_cn']}，{parsed['heading_cn']}",
                }
        except ConnectionError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            continue

    return {
        "success": False,
        "error": "未收到位置数据",
        "hint": "请确认机器人有发布 /odom 或 /odom_combined，且 rosbridge 可访问",
    }
