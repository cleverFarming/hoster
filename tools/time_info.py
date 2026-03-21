"""工具：获取当前时间 & 农事建议（通过 ROS2 服务）"""

from ._registry  import ToolRegistry
from ._constants import ROS_SERVICES, ROSBRIDGE_TIMEOUT
from ._ws_client import rosbridge


@ToolRegistry.register(
    name="get_current_time",
    display_name="🕐 获取当前时间",
    description=(
        "获取当前日期、时间、星期、季节，以及对应的农事建议提示。"
        "当用户询问现在几点、今天几号、什么季节等时间相关问题时使用。"
    ),
    parameters={
        "timezone": {
            "type": "string",
            "description": "时区名称，默认 Asia/Shanghai",
            "default": "Asia/Shanghai",
        },
    },
)
def get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    """
    调用 ROS2 服务: /farm/get_time
    请求 args:  {"timezone": "Asia/Shanghai"}
    期望返回 values: {
        "date": "2026年03月14日", "time": "10:00:00",
        "weekday": "星期六", "period": "上午", "season": "春季",
        "datetime_full": "2026-03-14 10:00:00",
        "timestamp": 1773648000,
        "farm_hint": "光照渐强，注意观察作物状态",
        "season_hint": "春耕播种期，注意倒春寒",
        "timezone": "Asia/Shanghai"
    }
    """
    resp = rosbridge.call_service(
        ROS_SERVICES["get_time"],
        args={"timezone": timezone},
        timeout=ROSBRIDGE_TIMEOUT,
    )
    return resp
