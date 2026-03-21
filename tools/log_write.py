"""工具：写入操作日志（通过 ROS2 服务）"""

from ._registry  import ToolRegistry
from ._constants import ROS_SERVICES, ROSBRIDGE_TIMEOUT
from ._ws_client import rosbridge


@ToolRegistry.register(
    name="write_log",
    display_name="📝 写入操作日志",
    description="向系统日志写入一条操作记录",
    parameters={
        "operation_type": {"type": "string", "description": "操作类型，如：施肥、巡检、告警"},
        "details":        {"type": "string", "description": "操作详情"},
    },
    required=["operation_type", "details"],
)
def write_log(operation_type: str, details: str) -> dict:
    """
    调用 ROS2 服务: /farm/write_log
    请求 args:  {"operation_type": "施肥", "details": "东北区域施有机肥 5kg"}
    期望返回 values: {"status": "success", "message": "日志已写入"}
    """
    resp = rosbridge.call_service(
        ROS_SERVICES["write_log"],
        args={"operation_type": operation_type, "details": details},
        timeout=ROSBRIDGE_TIMEOUT,
    )
    return resp
