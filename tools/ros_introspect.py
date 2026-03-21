"""工具：查询 ROS2 系统状态（nodes / topics / services）

通过 rosbridge 的 /rosapi/* 服务获取当前 ROS2 系统的运行信息，
用于诊断、排查、了解机器人当前开启了哪些功能。
"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT


@ToolRegistry.register(
    name="get_ros_status",
    display_name="🔍 ROS2 系统状态",
    description=(
        "查询 ROS2 当前运行状态：活跃的 nodes（节点）、topics（话题）、services（服务）。"
        "可选择只查某一类或全部查。"
        "当用户询问'有哪些节点在跑'、'当前有什么 topic'、'ROS 状态'、'机器人开了哪些功能'时使用。"
    ),
    parameters={
        "query": {
            "type": "string",
            "enum": ["all", "nodes", "topics", "services"],
            "description": "查询类型：all=全部, nodes=节点, topics=话题, services=服务",
            "default": "all",
        },
        "filter": {
            "type": "string",
            "description": "可选关键词过滤，只返回包含该关键词的结果，如 'slam'、'camera'、'nav'",
        },
    },
)
def get_ros_status(query: str = "all", filter: str = "") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    result = {"success": True}
    kw = filter.lower().strip()

    # ── Nodes ──
    if query in ("all", "nodes"):
        try:
            resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
            nodes = resp.get("nodes", [])
            if kw:
                nodes = [n for n in nodes if kw in n.lower()]
            result["nodes"] = sorted(nodes)
            result["node_count"] = len(nodes)
        except Exception as e:
            result["nodes_error"] = str(e)

    # ── Topics ──
    if query in ("all", "topics"):
        try:
            resp = rosbridge.call_service("/rosapi/topics", args={}, timeout=ROSBRIDGE_TIMEOUT)
            topics = resp.get("topics", [])
            types = resp.get("types", [])
            pairs = list(zip(topics, types))
            if kw:
                pairs = [(t, tp) for t, tp in pairs if kw in t.lower() or kw in tp.lower()]
            pairs.sort()
            result["topics"] = [{"name": t, "type": tp} for t, tp in pairs]
            result["topic_count"] = len(pairs)
        except Exception as e:
            result["topics_error"] = str(e)

    # ── Services ──
    if query in ("all", "services"):
        try:
            resp = rosbridge.call_service("/rosapi/services", args={}, timeout=ROSBRIDGE_TIMEOUT)
            services = resp.get("services", [])
            if kw:
                services = [s for s in services if kw in s.lower()]
            result["services"] = sorted(services)
            result["service_count"] = len(services)
        except Exception as e:
            result["services_error"] = str(e)

    # ── 摘要 ──
    parts = []
    if "node_count" in result:
        parts.append(f"{result['node_count']} 个节点")
    if "topic_count" in result:
        parts.append(f"{result['topic_count']} 个话题")
    if "service_count" in result:
        parts.append(f"{result['service_count']} 个服务")
    filter_desc = f"（过滤: '{kw}'）" if kw else ""
    result["summary_cn"] = f"当前活跃: {', '.join(parts)}{filter_desc}"

    return result
