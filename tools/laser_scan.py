"""工具：通过 rosbridge 订阅 /scan 获取激光雷达数据（障碍物检测）"""

import math

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

SCAN_TOPICS = [
    ("/scan", "sensor_msgs/msg/LaserScan"),
]


def _parse_scan(msg: dict) -> dict:
    """解析 sensor_msgs/LaserScan，提取障碍物信息"""
    ranges = msg.get("ranges", []) or []
    angle_min = msg.get("angle_min", 0)
    angle_increment = msg.get("angle_increment", 0)
    range_min = msg.get("range_min", 0)
    range_max = msg.get("range_max", float("inf"))

    # 过滤有效读数（非 inf、非 nan、在 range_min~range_max 内）
    valid = [
        r for r in ranges
        if isinstance(r, (int, float)) and math.isfinite(r)
        and range_min <= r <= range_max
    ]
    if not valid:
        return {
            "closest_m": None,
            "farthest_m": None,
            "valid_count": 0,
            "total_count": len(ranges),
            "obstacle_detected": False,
        }

    closest = min(valid)
    farthest = max(valid)

    # 正前方通常为 angle≈0（即 index=len//2 若 angle_min=-π）
    fwd_idx = len(ranges) // 2
    fwd_dist = ranges[fwd_idx] if 0 <= fwd_idx < len(ranges) and math.isfinite(ranges[fwd_idx]) else None

    return {
        "closest_m": round(closest, 3),
        "farthest_m": round(farthest, 3),
        "forward_m": round(fwd_dist, 3) if fwd_dist is not None else None,
        "valid_count": len(valid),
        "total_count": len(ranges),
        "obstacle_detected": closest < 5.0,
        "frame_id": msg.get("header", {}).get("frame_id", "laser"),
    }


@ToolRegistry.register(
    name="get_laser_scan",
    display_name="📡 激光雷达/障碍物",
    description="获取激光雷达扫描数据，用于障碍物检测。当用户询问前方有没有障碍、最近障碍距离、激光雷达数据时使用",
    parameters={},
    required=[],
)
def get_laser_scan() -> dict:
    """订阅 /scan 获取 LaserScan 消息，解析最近/最远距离及正前方距离"""
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    for topic, msg_type in SCAN_TOPICS:
        try:
            msg = rosbridge.subscribe_once(topic, msg_type, timeout=ROSBRIDGE_TIMEOUT)
            if msg:
                parsed = _parse_scan(msg)
                summary = f"最近障碍 {parsed['closest_m']}m" if parsed.get("closest_m") else "无有效读数"
                if parsed.get("forward_m") is not None:
                    summary += f"，正前方 {parsed['forward_m']}m"
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

    return {"success": False, "error": "未收到激光雷达数据", "hint": "请确认 /scan topic 存在"}
