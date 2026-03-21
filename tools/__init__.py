"""
tools 包 —— 自动发现并注册所有工具模块

新增工具:
    1. 在 tools/ 下新建 .py 文件（文件名不以 _ 开头）
    2. 用 @ToolRegistry.register(...) 装饰函数
    3. 完成！无需修改任何其他文件

对外 API:
    TOOL_DEFS            list[dict]       OpenAI function-calling schemas
    TOOL_DISPLAY_NAMES   dict[str, str]   {name: display_name}
    call_tool(name, args) -> str          统一调度，返回 JSON
    init_db()                             初始化数据库表
    connect_rosbridge(url, timeout)       连接 rosbridge WebSocket
    disconnect_rosbridge()                断开 rosbridge 连接
    is_rosbridge_connected() -> bool      连接状态
"""

import importlib
import pkgutil

from ._registry  import ToolRegistry
from ._db        import init_db
from ._ws_client import (
    connect_rosbridge,
    disconnect_rosbridge,
    is_rosbridge_connected,
)

# ── 自动导入所有非 _ 开头的子模块，触发 @register 装饰器 ──
for _, _mod_name, _ in pkgutil.iter_modules(__path__):
    if not _mod_name.startswith("_"):
        importlib.import_module(f".{_mod_name}", __name__)

# ── 公开接口（导入后即不变） ──
TOOL_DEFS          = ToolRegistry.get_schemas()
TOOL_DISPLAY_NAMES = ToolRegistry.get_display_names()
call_tool          = ToolRegistry.call

__all__ = [
    "TOOL_DEFS",
    "TOOL_DISPLAY_NAMES",
    "call_tool",
    "init_db",
    "connect_rosbridge",
    "disconnect_rosbridge",
    "is_rosbridge_connected",
]
