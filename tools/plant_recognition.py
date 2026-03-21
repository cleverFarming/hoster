"""工具：植物识别（基于 DeepSeek VL 多模态模型）

流程:
  1. 从机器人摄像头获取截图（通过 web_video_server HTTP 接口）
  2. 将图片编码为 base64
  3. 发送给 DeepSeek VL（或兼容的多模态模型）进行植物识别
  4. 返回植物名称、特征描述、养护建议

环境变量配置:
  VL_API_KEY      火山引擎 API Key
  VL_API_BASE     火山方舟 API 地址（默认 https://ark.cn-beijing.volces.com/api/v3）
  VL_MODEL        视觉模型名称（默认 doubao-1.5-vision-pro-250328）

配置步骤:
  1. 登录火山方舟 https://console.volcengine.com/ark
  2. 创建推理接入点，选择 doubao-1.5-vision-pro 模型
  3. 在 API Key 管理中创建 Key，填入 .env 的 VL_API_KEY
  接口格式：兼容 OpenAI SDK（image_url content type）
"""

import base64
import os
import urllib.request
import urllib.error

from openai import OpenAI

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# ═══════════════════ 配置 ═══════════════════

# 火山引擎豆包视觉模型（兼容 OpenAI SDK）
VL_API_KEY  = os.environ.get("VL_API_KEY", "")
VL_API_BASE = os.environ.get("VL_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
VL_MODEL    = os.environ.get("VL_MODEL", "doubao-1.5-vision-pro-250328")

# web_video_server 端口（与 camera.py 保持一致）
WEB_VIDEO_PORT = 8080

# 图片下载超时（秒）
IMAGE_TIMEOUT = 10

# ═══════════════════ 植物识别 Prompt ═══════════════════

PLANT_SYSTEM_PROMPT = """你是一位专业的植物学家和农业专家。
请仔细观察图片，识别其中的植物、花卉、草本等，并给出：

1. **植物名称**：中文名 + 学名（如能识别）
2. **分类信息**：科、属
3. **外观特征**：叶型、花色、株高等可观察到的特征
4. **生长状况**：根据图片判断健康状态（如有病虫害迹象请指出）
5. **养护建议**：适合的温度、湿度、光照、浇水频率等

如果图片中有多种植物，请逐一识别。
如果图片不清晰或无法识别具体种类，请说明并给出最接近的推测。
始终使用中文回复。"""

PLANT_USER_PROMPT_DEFAULT = "请识别这张图片中的植物，详细描述其名称、特征和养护建议。"


# ═══════════════════ 内部辅助 ═══════════════════


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


def _find_color_camera_topic(all_topics: list) -> str:
    """找到第一个彩色摄像头 topic"""
    priority = [
        "/camera/color/image_raw",
        "/camera/image_raw",
        "/image_raw",
    ]
    for t in priority:
        if t in all_topics:
            return t
    # 兜底：找任意含 image 且不含 depth/compressed 的 topic
    for t in all_topics:
        t_low = t.lower()
        if "image" in t_low and "depth" not in t_low and "compressed" not in t_low:
            return t
    return ""


def _download_snapshot(host: str, topic: str) -> bytes:
    """从 web_video_server 下载 JPEG 截图"""
    url = f"http://{host}:{WEB_VIDEO_PORT}/snapshot?topic={topic}"
    req = urllib.request.Request(url, headers={"Accept": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
        return resp.read()


def _check_web_video_server() -> bool:
    """检测 web_video_server 是否运行"""
    try:
        resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
        nodes = resp.get("nodes", [])
        return any("web_video" in n.lower() for n in nodes)
    except Exception:
        return False


def _call_vl_model(base64_image: str, user_prompt: str) -> str:
    """调用多模态模型识别图片"""
    client = OpenAI(api_key=VL_API_KEY, base_url=VL_API_BASE)

    messages = [
        {"role": "system", "content": PLANT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                    },
                },
                {
                    "type": "text",
                    "text": user_prompt,
                },
            ],
        },
    ]

    response = client.chat.completions.create(
        model=VL_MODEL,
        messages=messages,
        max_tokens=2048,
    )

    return response.choices[0].message.content


# ═══════════════════ 工具 1：识别摄像头画面中的植物 ═══════════════════


@ToolRegistry.register(
    name="recognize_plant",
    display_name="🌿 植物识别",
    description=(
        "拍摄摄像头当前画面并识别其中的植物、花卉、草本等。"
        "使用多模态视觉模型分析图片，返回植物名称、分类、特征描述和养护建议。"
        "当用户说'这是什么植物'、'识别一下花'、'看看这是什么草'、"
        "'帮我认一下植物'、'分析一下作物'时使用。"
    ),
    parameters={
        "prompt": {
            "type": "string",
            "description": "附加提问（如'这棵植物是否健康'），留空则使用默认植物识别提示",
            "default": "",
        },
        "topic": {
            "type": "string",
            "description": "摄像头 image topic，留空自动选择彩色摄像头",
            "default": "",
        },
    },
)
def recognize_plant(prompt: str = "", topic: str = "") -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    if not VL_API_KEY:
        return {
            "success": False,
            "error": "未配置视觉模型 API Key",
            "hint": (
                "植物识别需要火山引擎豆包视觉模型。"
                "请在 .env 中配置 VL_API_KEY：\n"
                "1. 登录火山方舟 https://console.volcengine.com/ark\n"
                "2. 创建接入点选择 doubao-1.5-vision-pro，获取 API Key\n"
                "3. 填入 .env 的 VL_API_KEY，重启服务"
            ),
        }

    # ── 1. 检查 web_video_server ──
    if not _check_web_video_server():
        return {
            "success": False,
            "error": "web_video_server 未运行",
            "hint": "请先通过 robot_command 依次启动 camera 和 web_video_server",
        }

    # ── 2. 查找摄像头 topic ──
    if not topic:
        all_topics = _get_ros_topics()
        topic = _find_color_camera_topic(all_topics)
        if not topic:
            return {
                "success": False,
                "error": "未找到彩色摄像头 topic",
                "hint": "请先通过 robot_command 启动 camera",
            }

    # ── 3. 下载截图 ──
    host = _get_robot_host()
    try:
        image_data = _download_snapshot(host, topic)
    except urllib.error.URLError as e:
        return {
            "success": False,
            "error": f"下载摄像头截图失败: {e}",
            "hint": "确认 web_video_server 正在运行且摄像头已启动",
        }
    except Exception as e:
        return {"success": False, "error": f"获取图片失败: {e}"}

    if len(image_data) < 1000:
        return {
            "success": False,
            "error": "截图数据过小，可能摄像头未正常出图",
            "hint": "检查摄像头是否已启动并正在发布图像",
        }

    # ── 4. 编码为 base64 ──
    base64_image = base64.b64encode(image_data).decode("utf-8")
    image_size_kb = round(len(image_data) / 1024, 1)

    # ── 5. 调用多模态模型 ──
    user_prompt = prompt.strip() if prompt.strip() else PLANT_USER_PROMPT_DEFAULT

    try:
        result_text = _call_vl_model(base64_image, user_prompt)
    except Exception as e:
        error_msg = str(e)
        # 常见错误提示
        if "not support" in error_msg.lower() or "image" in error_msg.lower():
            return {
                "success": False,
                "error": f"当前模型不支持图片输入: {error_msg}",
                "hint": (
                    "请检查 .env 中的视觉模型配置。"
                    "推荐：VL_API_BASE=https://ark.cn-beijing.volces.com/api/v3  "
                    "VL_MODEL=doubao-1.5-vision-pro-250328  "
                    "VL_API_KEY=你的火山引擎Key"
                ),
            }
        return {"success": False, "error": f"模型调用失败: {error_msg}"}

    # ── 6. 构建 snapshot URL 供前端展示 ──
    snapshot_url = f"http://{host}:{WEB_VIDEO_PORT}/snapshot?topic={topic}"

    return {
        "success": True,
        "topic": topic,
        "image_size_kb": image_size_kb,
        "model": VL_MODEL,
        "snapshot_url": snapshot_url,
        "recognition": result_text,
        "summary_cn": f"已识别摄像头画面中的植物（图片 {image_size_kb}KB，模型 {VL_MODEL}）",
    }


# ═══════════════════ 工具 2：识别指定图片中的植物 ═══════════════════


@ToolRegistry.register(
    name="recognize_plant_from_url",
    display_name="🌿 图片植物识别",
    description=(
        "通过图片 URL 识别植物。支持任意可访问的图片链接（HTTP/HTTPS）。"
        "当用户提供了一个图片链接并要求识别植物时使用。"
    ),
    parameters={
        "image_url": {
            "type": "string",
            "description": "图片的 HTTP/HTTPS 地址",
        },
        "prompt": {
            "type": "string",
            "description": "附加提问，留空使用默认植物识别提示",
            "default": "",
        },
    },
    required=["image_url"],
)
def recognize_plant_from_url(image_url: str, prompt: str = "") -> dict:
    if not VL_API_KEY:
        return {
            "success": False,
            "error": "未配置视觉模型 API Key",
            "hint": (
                "请在 .env 中配置 VL_API_KEY（火山方舟 https://console.volcengine.com/ark）"
            ),
        }

    # ── 1. 下载图片 ──
    try:
        req = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/*",
        })
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as resp:
            image_data = resp.read()
    except Exception as e:
        return {"success": False, "error": f"下载图片失败: {e}"}

    if len(image_data) < 500:
        return {"success": False, "error": "图片数据过小或无效"}

    # ── 2. 判断图片格式 ──
    mime = "image/jpeg"
    if image_url.lower().endswith(".png"):
        mime = "image/png"
    elif image_url.lower().endswith(".webp"):
        mime = "image/webp"

    # ── 3. 编码并调用模型 ──
    base64_image = base64.b64encode(image_data).decode("utf-8")
    image_size_kb = round(len(image_data) / 1024, 1)
    user_prompt = prompt.strip() if prompt.strip() else PLANT_USER_PROMPT_DEFAULT

    try:
        client = OpenAI(api_key=VL_API_KEY, base_url=VL_API_BASE)
        messages = [
            {"role": "system", "content": PLANT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{base64_image}",
                        },
                    },
                    {
                        "type": "text",
                        "text": user_prompt,
                    },
                ],
            },
        ]
        response = client.chat.completions.create(
            model=VL_MODEL,
            messages=messages,
            max_tokens=2048,
        )
        result_text = response.choices[0].message.content
    except Exception as e:
        error_msg = str(e)
        if "not support" in error_msg.lower() or "image" in error_msg.lower():
            return {
                "success": False,
                "error": f"当前模型不支持图片输入: {error_msg}",
                "hint": "请检查 VL_API_BASE 和 VL_MODEL 配置，确认使用豆包视觉模型",
            }
        return {"success": False, "error": f"模型调用失败: {error_msg}"}

    return {
        "success": True,
        "image_url": image_url,
        "image_size_kb": image_size_kb,
        "model": VL_MODEL,
        "recognition": result_text,
        "summary_cn": f"已识别图片中的植物（{image_size_kb}KB，模型 {VL_MODEL}）",
    }
