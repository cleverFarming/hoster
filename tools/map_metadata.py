"""工具：通过 rosbridge 订阅 /map_metadata 获取地图元数据"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

MAP_META_TOPICS = [
    ("/map_metadata", "nav_msgs/msg/MapMetaData"),
]


def _parse_map_metadata(msg: dict) -> dict:
    """解析 nav_msgs/MapMetaData"""
    resolution = msg.get("resolution", 0) or 0
    width = msg.get("width", 0) or 0
    height = msg.get("height", 0) or 0
    origin = msg.get("origin", {}) or {}
    pos = origin.get("position", {}) or {}
    ox = pos.get("x", 0) or 0
    oy = pos.get("y", 0) or 0
    oz = pos.get("z", 0) or 0

    # 实际物理尺寸（米）
    width_m = width * resolution
    height_m = height * resolution

    return {
        "resolution": round(resolution, 4),
        "width_cells": int(width),
        "height_cells": int(height),
        "width_m": round(width_m, 2),
        "height_m": round(height_m, 2),
        "origin": {"x": round(ox, 2), "y": round(oy, 2), "z": round(oz, 2)},
    }


@ToolRegistry.register(
    name="get_map_metadata",
    display_name="🗺️ 地图元数据",
    description="获取导航地图的元数据（尺寸、分辨率、原点）。当用户询问地图大小、地图分辨率、导航地图信息时使用",
    parameters={},
    required=[],
)
def get_map_metadata() -> dict:
    """订阅 /map_metadata 获取 MapMetaData 消息（通常为 latched，一条即可）"""
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    for topic, msg_type in MAP_META_TOPICS:
        try:
            msg = rosbridge.subscribe_once(topic, msg_type, timeout=ROSBRIDGE_TIMEOUT)
            if msg:
                parsed = _parse_map_metadata(msg)
                summary = f"{parsed['width_m']}m×{parsed['height_m']}m，分辨率 {parsed['resolution']}m/格"
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

    return {"success": False, "error": "未收到地图元数据", "hint": "请确认 /map_metadata 存在"}
