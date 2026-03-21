"""工具：读取操作日志（通过 ROS2 服务）"""

from typing import Optional

from ._registry  import ToolRegistry
from ._constants import ROS_SERVICES, ROSBRIDGE_TIMEOUT
from ._ws_client import rosbridge


@ToolRegistry.register(
    name="read_logs",
    display_name="📖 读取操作日志",
    description="查询系统操作日志，可按类型筛选",
    parameters={
        "limit":          {"type": "integer", "description": "返回条数，默认 10"},
        "operation_type": {"type": "string",  "description": "按操作类型筛选（可选）"},
    },
)
def read_logs(limit: int = 10, operation_type: Optional[str] = None) -> dict:
    """
    调用 ROS2 服务: /farm/read_logs
    请求 args:  {"limit": 10, "operation_type": "浇水"}  (operation_type 可选)
    期望返回 values: {
        "count": 3,
        "logs": [
            {"time": "...", "type": "浇水", "detail": "...", "operator": "AI"},
            ...
        ]
    }
    """
    args = {"limit": limit}
    if operation_type:
        args["operation_type"] = operation_type

    resp = rosbridge.call_service(
        ROS_SERVICES["read_logs"],
        args=args,
        timeout=ROSBRIDGE_TIMEOUT,
    )
    return resp
