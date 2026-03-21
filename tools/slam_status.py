"""工具：检测 SLAM 建图状态 & 管理地图（重置/保存）

三重检测（参考 rosbridge 最佳实践）:
  1. 查 /rosapi/nodes  — 哪种 SLAM 节点在跑
  2. 查 /rosapi/topics — SLAM 特征 topic 是否存在
  3. 监听 /map 3 秒    — 是否在活跃建图（有地图更新）

通过查询 ROS2 运行中的 topics 和 nodes 判断 SLAM 是否在运行，
而不是尝试调用 start/stop 服务。这样即使没有 command_launcher 也能准确检测。
"""

import threading

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# ═══════════════════ SLAM 特征指纹 ═══════════════════
# 每种 SLAM 实现会产生特定的 topics / nodes

SLAM_SIGNATURES = {
    "slam_toolbox": {
        "display": "SLAM Toolbox",
        "topics": ["/slam_toolbox/graph_visualization", "/slam_toolbox/scan_visualization"],
        "nodes":  ["/slam_toolbox", "/async_slam_toolbox_node", "/sync_slam_toolbox_node"],
        "reset_service": "/slam_toolbox/reset",
        "reset_type": "std_srvs/srv/Empty",
    },
    "gmapping": {
        "display": "GMapping",
        "topics": ["/slam_gmapping/entropy"],
        "nodes":  ["/slam_gmapping"],
        "reset_service": None,
        "reset_type": None,
    },
    "cartographer": {
        "display": "Cartographer",
        "topics": ["/constraint_list", "/landmark_poses_list", "/trajectory_node_list"],
        "nodes":  ["/cartographer_node", "/cartographer_occupancy_grid_node"],
        "reset_service": None,
        "reset_type": None,
    },
    "rtabmap": {
        "display": "RTAB-Map",
        "topics": ["/rtabmap/mapData", "/rtabmap/mapGraph", "/rtabmap/info"],
        "nodes":  ["/rtabmap", "/rtabmap_ros"],
        "reset_service": "/rtabmap/reset",
        "reset_type": "std_srvs/srv/Empty",
    },
}

COMMON_MAP_TOPICS = ["/map", "/map_metadata"]


# ═══════════════════ 内部辅助函数 ═══════════════════

def _get_ros_topics() -> list:
    """通过 /rosapi/topics 获取当前所有 topics"""
    try:
        resp = rosbridge.call_service("/rosapi/topics", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("topics", [])
    except Exception:
        return []


def _get_ros_nodes() -> list:
    """通过 /rosapi/nodes 获取当前所有 nodes"""
    try:
        resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("nodes", [])
    except Exception:
        return []


def _check_map_active(wait_sec: float = 3.0) -> dict:
    """
    监听 /map topic wait_sec 秒，统计收到的地图更新次数。
    有更新 = 正在活跃建图；无更新 = SLAM 可能在跑但地图没变化。
    """
    count = [0]
    event = threading.Event()

    def _on_map(msg):
        count[0] += 1
        # 收到第一条就足以证明在活跃建图，提前结束等待
        event.set()

    try:
        rosbridge.subscribe("/map", "nav_msgs/msg/OccupancyGrid", _on_map, queue_length=1)
        event.wait(timeout=wait_sec)
    except Exception:
        pass
    finally:
        try:
            rosbridge.unsubscribe("/map")
        except Exception:
            pass

    return {
        "active": count[0] > 0,
        "map_updates": count[0],
        "wait_sec": wait_sec,
    }


def _detect_slam(topics: list, nodes: list) -> list:
    """根据特征指纹匹配当前在跑的 SLAM"""
    detected = []
    for slam_id, sig in SLAM_SIGNATURES.items():
        # 精确匹配 + 模糊匹配（node 名可能带命名空间前缀）
        matched_topics = [t for t in sig["topics"] if t in topics]
        matched_nodes_exact = [n for n in sig["nodes"] if n in nodes]
        matched_nodes_fuzzy = [
            n for n in nodes
            if any(key in n for key in sig["nodes"])
        ] if not matched_nodes_exact else matched_nodes_exact

        if matched_topics or matched_nodes_exact or matched_nodes_fuzzy:
            detected.append({
                "slam_type": slam_id,
                "display": sig["display"],
                "matched_topics": matched_topics,
                "matched_nodes": matched_nodes_exact or matched_nodes_fuzzy,
                "can_reset": sig["reset_service"] is not None,
                "reset_service": sig["reset_service"],
            })

    return detected


# ═══════════════════ 工具 1：检测 SLAM 状态 ═══════════════════

@ToolRegistry.register(
    name="check_slam_status",
    display_name="🗺️ 检测 SLAM 状态",
    description=(
        "检测 SLAM 建图是否在运行、使用哪种算法、是否在活跃建图。"
        "三重检测：查 nodes + 查 topics + 监听 /map 数据更新。"
        "当用户询问'SLAM 开了没'、'在建图吗'、'地图状态'时使用。"
    ),
    parameters={
        "check_active": {
            "type": "boolean",
            "description": "是否监听 /map 检测活跃建图（需等待约3秒），默认 true",
            "default": True,
        },
    },
)
def check_slam_status(check_active: bool = True) -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # ── 第1步：查 topics ──
    topics = _get_ros_topics()
    # ── 第2步：查 nodes ──
    nodes = _get_ros_nodes()

    if not topics and not nodes:
        return {
            "success": False,
            "error": "无法获取 ROS2 topics/nodes",
            "hint": "请确认 rosbridge 和 rosapi 正在运行",
        }

    # 匹配 SLAM 特征
    detected = _detect_slam(topics, nodes)
    has_map = any(t in topics for t in COMMON_MAP_TOPICS)

    # ── 第3步：监听 /map 检测活跃建图 ──
    map_activity = None
    if check_active and has_map:
        map_activity = _check_map_active(wait_sec=3.0)

    is_active = map_activity["active"] if map_activity else False
    map_updates = map_activity["map_updates"] if map_activity else 0

    # ── 组装结果 ──
    if detected:
        names = [d["display"] for d in detected]
        summary = f"SLAM 正在运行: {', '.join(names)}"
        if has_map:
            if is_active:
                summary += f"，地图正在活跃更新（3秒内收到 {map_updates} 次更新）"
            else:
                summary += "，/map topic 存在但 3 秒内无更新（可能已暂停建图或地图稳定）"
        resetable = [d for d in detected if d["can_reset"]]
        if resetable:
            summary += f"。支持在线重置: {', '.join(d['display'] for d in resetable)}"
        return {
            "success": True,
            "slam_running": True,
            "active_mapping": is_active,
            "map_updates_3s": map_updates,
            "detected": detected,
            "has_map_topic": has_map,
            "summary_cn": summary,
        }

    if has_map:
        status_desc = "正在活跃更新" if is_active else "存在但无新数据"
        return {
            "success": True,
            "slam_running": False,
            "active_mapping": is_active,
            "map_updates_3s": map_updates,
            "detected": [],
            "has_map_topic": True,
            "summary_cn": f"/map topic {status_desc}，可能是导航加载的静态地图或未识别的建图算法",
        }

    return {
        "success": True,
        "slam_running": False,
        "active_mapping": False,
        "map_updates_3s": 0,
        "detected": [],
        "has_map_topic": False,
        "summary_cn": "SLAM 未运行，未检测到建图相关 topics、nodes，/map 也不存在",
    }


# ═══════════════════ 工具 2：重置地图 ═══════════════════

@ToolRegistry.register(
    name="reset_slam_map",
    display_name="🔄 重置 SLAM 地图",
    description=(
        "重置当前 SLAM 建图，清空已有地图重新开始绘制。"
        "支持 SLAM Toolbox（/slam_toolbox/reset）和 RTAB-Map（/rtabmap/reset）的在线重置。"
        "GMapping 和 Cartographer 不支持在线重置，需停止后重新启动。"
        "当用户要求'重新建图'、'清空地图'、'重置地图'时使用。"
    ),
    parameters={
        "slam_type": {
            "type": "string",
            "enum": ["auto", "slam_toolbox", "rtabmap"],
            "description": "指定 SLAM 类型，auto=自动检测当前运行的 SLAM",
            "default": "auto",
        },
    },
)
def reset_slam_map(slam_type: str = "auto") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 自动检测
    if slam_type == "auto":
        status = check_slam_status(check_active=False)
        if not status.get("slam_running"):
            return {
                "success": False,
                "error": "当前没有 SLAM 在运行，无法重置",
                "hint": "请先启动 SLAM 建图",
            }
        for d in status.get("detected", []):
            if d.get("can_reset") and d.get("reset_service"):
                slam_type = d["slam_type"]
                break
        else:
            running = [d["display"] for d in status.get("detected", [])]
            return {
                "success": False,
                "error": f"当前运行的 SLAM ({', '.join(running)}) 不支持在线重置",
                "hint": "需要通过 robot_command 停止后重新启动该 SLAM",
            }

    sig = SLAM_SIGNATURES.get(slam_type)
    if not sig or not sig.get("reset_service"):
        return {
            "success": False,
            "error": f"{slam_type} 不支持在线重置地图",
            "hint": "需要停止后重新启动该 SLAM 来重建地图",
        }

    reset_svc = sig["reset_service"]
    try:
        # std_srvs/srv/Empty 不需要参数，也不返回数据
        rosbridge.call_service(reset_svc, args={}, timeout=ROSBRIDGE_TIMEOUT)
        return {
            "success": True,
            "slam_type": slam_type,
            "display": sig["display"],
            "reset_service": reset_svc,
            "summary_cn": f"已重置 {sig['display']} 地图，地图已清空，小车移动后将重新绘制",
        }
    except TimeoutError:
        return {
            "success": False,
            "error": f"调用 {reset_svc} 超时",
            "hint": f"请确认 {sig['display']} 正在运行",
        }
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}
