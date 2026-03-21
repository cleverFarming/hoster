"""工具：摄像头状态检测 & 画面截图/视频流

截图方式：通过 web_video_server 的 /snapshot HTTP 接口获取 JPEG（~63KB，比原始 ROS image 快十几倍）
视频流：通过 web_video_server 的 /stream 接口提供 MJPEG 流
前端可直接用 <img src="snapshot_url"> 或 <img src="stream_url"> 嵌入显示
"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# ═══════════════════ 配置 ═══════════════════

# 常见的摄像头 image topic（按优先级）
CAMERA_TOPICS = [
    ("/camera/color/image_raw",   "彩色摄像头"),
    ("/camera/image_raw",         "主摄像头"),
    ("/image_raw",                "默认摄像头"),
    ("/camera/depth/image_raw",   "深度摄像头"),
]

WEB_VIDEO_PORT = 8080


def _get_robot_host() -> str:
    """从 rosbridge URL 提取机器人 IP"""
    url = rosbridge.url
    return url.replace("ws://", "").replace("wss://", "").split(":")[0]


def _get_ros_topics() -> list:
    try:
        resp = rosbridge.call_service("/rosapi/topics", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("topics", [])
    except Exception:
        return []


def _find_camera_topics(all_topics: list) -> list:
    """匹配已知 + 模糊搜索"""
    found = []
    for topic, desc in CAMERA_TOPICS:
        if topic in all_topics:
            found.append({"topic": topic, "description": desc})

    known = {item["topic"] for item in found}
    for t in all_topics:
        if t not in known and ("image" in t.lower() and "compressed" not in t.lower()):
            found.append({"topic": t, "description": "其他摄像头"})

    return found


def _check_web_video_server() -> bool:
    """检测 web_video_server 节点是否在跑"""
    try:
        resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
        nodes = resp.get("nodes", [])
        return any("web_video" in n.lower() for n in nodes)
    except Exception:
        return False


def _build_urls(host: str, topic: str) -> dict:
    """为指定 topic 生成 snapshot / stream URL"""
    base = f"http://{host}:{WEB_VIDEO_PORT}"
    return {
        "snapshot_url": f"{base}/snapshot?topic={topic}",
        "stream_url":   f"{base}/stream?topic={topic}",
    }


# ═══════════════════ 工具 1：检测摄像头状态 ═══════════════════

@ToolRegistry.register(
    name="check_camera_status",
    display_name="📷 检测摄像头",
    description=(
        "检测摄像头是否开启、有哪些图像 topic、web_video_server 是否可用。"
        "返回视频流和截图的 HTTP 地址，可直接在网页中嵌入显示。"
        "当用户询问'摄像头开了吗'、'相机状态'、'能看到画面吗'时使用。"
    ),
    parameters={},
    required=[],
)
def check_camera_status() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    all_topics = _get_ros_topics()
    if not all_topics:
        return {"success": False, "error": "无法获取 topics"}

    camera_topics = _find_camera_topics(all_topics)
    has_web_video = _check_web_video_server()
    host = _get_robot_host()

    if not camera_topics:
        return {
            "success": True,
            "camera_running": False,
            "has_web_video": has_web_video,
            "topics": [],
            "summary_cn": "未检测到摄像头 image topic，摄像头可能未启动。可通过 robot_command 启动 camera。",
        }

    # 为每个 topic 生成 URL
    cameras = []
    for cam in camera_topics:
        urls = _build_urls(host, cam["topic"]) if has_web_video else {}
        cameras.append({**cam, **urls})

    summary_parts = [f"摄像头已开启，检测到 {len(cameras)} 个图像 topic"]
    if has_web_video:
        primary = cameras[0]
        summary_parts.append(f"web_video_server 已运行")
        summary_parts.append(f"截图: {primary.get('snapshot_url', '')}")
        summary_parts.append(f"视频流: {primary.get('stream_url', '')}")
    else:
        summary_parts.append("web_video_server 未运行，请通过 robot_command 启动 web_video_server（需先启动 camera）")

    return {
        "success": True,
        "camera_running": True,
        "has_web_video": has_web_video,
        "cameras": cameras,
        "summary_cn": "。".join(summary_parts),
    }


# ═══════════════════ 工具 2：获取摄像头截图 ═══════════════════

@ToolRegistry.register(
    name="capture_camera_image",
    display_name="📸 摄像头截图",
    description=(
        "获取摄像头当前画面截图。通过 web_video_server 的 HTTP snapshot 接口获取 JPEG 图片。"
        "返回可直接在网页 <img> 标签中使用的图片 URL。"
        "当用户要求'拍一张照片'、'看看前面是什么'、'截个图'时使用。"
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "图像 topic，留空自动选择（通常为 /camera/color/image_raw）",
            "default": "",
        },
    },
)
def capture_camera_image(topic: str = "") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 检测 web_video_server
    if not _check_web_video_server():
        return {
            "success": False,
            "error": "web_video_server 未运行",
            "hint": "请先通过 robot_command 依次启动 camera 和 web_video_server",
        }

    # 自动选择 topic
    if not topic:
        all_topics = _get_ros_topics()
        camera_topics = _find_camera_topics(all_topics)
        if not camera_topics:
            return {
                "success": False,
                "error": "未找到摄像头 image topic",
                "hint": "请先启动摄像头",
            }
        topic = camera_topics[0]["topic"]

    host = _get_robot_host()
    urls = _build_urls(host, topic)

    return {
        "success": True,
        "topic": topic,
        **urls,
        "summary_cn": f"截图地址: {urls['snapshot_url']}  视频流: {urls['stream_url']}",
    }


# ═══════════════════ 工具 3：获取视频流地址 ═══════════════════

@ToolRegistry.register(
    name="get_camera_stream",
    display_name="📹 摄像头视频流",
    description=(
        "获取摄像头实时视频流地址（MJPEG 格式），可直接嵌入网页播放。"
        "支持彩色摄像头和深度摄像头。"
        "当用户要求'看实时画面'、'打开摄像头视频'、'实时监控'时使用。"
    ),
    parameters={
        "camera_type": {
            "type": "string",
            "enum": ["color", "depth", "all"],
            "description": "摄像头类型：color=彩色, depth=深度, all=全部可用",
            "default": "color",
        },
    },
)
def get_camera_stream(camera_type: str = "color") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    if not _check_web_video_server():
        return {
            "success": False,
            "error": "web_video_server 未运行",
            "hint": "请先通过 robot_command 依次启动 camera 和 web_video_server",
        }

    all_topics = _get_ros_topics()
    camera_topics = _find_camera_topics(all_topics)
    if not camera_topics:
        return {"success": False, "error": "未找到摄像头 topic"}

    host = _get_robot_host()

    # 按类型过滤
    if camera_type == "depth":
        filtered = [c for c in camera_topics if "depth" in c["topic"].lower()]
    elif camera_type == "color":
        filtered = [c for c in camera_topics if "depth" not in c["topic"].lower()]
    else:
        filtered = camera_topics

    if not filtered:
        filtered = camera_topics  # 回退到全部

    streams = []
    for cam in filtered:
        urls = _build_urls(host, cam["topic"])
        streams.append({
            "topic": cam["topic"],
            "description": cam["description"],
            **urls,
        })

    summary_parts = [f"共 {len(streams)} 路视频流可用"]
    for s in streams:
        summary_parts.append(f"{s['description']}: {s['stream_url']}")

    return {
        "success": True,
        "streams": streams,
        "summary_cn": "。".join(summary_parts),
    }
