"""共享常量 & 可复用的 JSON Schema 片段 & ROS2 服务映射"""

import os

# ── 数据库（保留用于本地日志缓存） ──
DB_PATH = "farm.db"

# ═══════════════════ ROS2 / rosbridge 配置 ═══════════════════

ROSBRIDGE_URL     = os.environ.get("ROSBRIDGE_URL",     "ws://localhost:9090")
ROSBRIDGE_TIMEOUT = float(os.environ.get("ROSBRIDGE_TIMEOUT", "10"))

# ROS2 服务名映射 —— 工具名 → ROS2 service 路径
# 机器人端需实现对应的 service server
ROS_SERVICES = {
    "get_time":           "/farm/get_time",
    "read_logs":          "/farm/read_logs",
    "write_log":          "/farm/write_log",
}
