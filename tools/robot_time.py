"""工具：获取机器人端 ROS 时间（通过 rosbridge /rosapi/get_time）"""

from datetime import datetime

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT


@ToolRegistry.register(
    name="get_robot_time",
    display_name="🕐 获取机器人时间",
    description="获取机器人端当前时间（ROS 时间）。当用户询问小人的时间、机器人时间、ROS 时间时使用",
    parameters={},
    required=[],
)
def get_robot_time() -> dict:
    """
    调用 rosapi 内置服务 /rosapi/get_time，无需机器人实现 /farm/* 服务。
    返回 sec + nanosec 及可读格式。
    """
    if not is_rosbridge_connected():
        return {
            "success": False,
            "error": "rosbridge 未连接",
            "hint": "请检查 ROSBRIDGE_URL 及机器人端 rosbridge 是否启动",
        }

    try:
        resp = rosbridge.call_service(
            "/rosapi/get_time",
            args={},
            timeout=ROSBRIDGE_TIMEOUT,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    time_obj = resp.get("time", {}) or {}
    sec = time_obj.get("sec", 0) or 0
    nanosec = time_obj.get("nanosec", 0) or 0

    # ROS 时间 0 表示仿真未启动，使用系统时间
    if sec == 0 and nanosec == 0:
        dt = datetime.now()
        sec = int(dt.timestamp())
        nanosec = dt.microsecond * 1000
        source = "系统时间（ROS 时间未初始化）"
    else:
        source = "ROS 时间"
        dt = datetime.fromtimestamp(sec + nanosec / 1e9)

    readable = dt.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "success": True,
        "sec": sec,
        "nanosec": nanosec,
        "readable": readable,
        "source": source,
        "summary_cn": f"机器人时间: {readable} ({source})",
    }
