"""工具：SLAM 自主探索建图（一键启动 + 保存地图为图片）

流程:
  1. 检测 slam_toolbox 是否已在运行，没有则自动启动
  2. 检测 rrt_slam 是否已在运行，没有则自动启动
  3. 向 /clicked_point 发布 5 个点（4 个边界 + 1 个起始点）触发 RRT 探索
  4. 提供保存地图为图片的功能
"""

import base64
import io
import json
import os
import struct
import time
import threading
import zlib
from datetime import datetime

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# ═══════════════════ 内部辅助 ═══════════════════


def _get_ros_nodes() -> list:
    """获取当前 ROS2 节点列表"""
    try:
        resp = rosbridge.call_service("/rosapi/nodes", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("nodes", [])
    except Exception:
        return []


def _get_ros_topics() -> list:
    """获取当前 ROS2 话题列表"""
    try:
        resp = rosbridge.call_service("/rosapi/topics", args={}, timeout=ROSBRIDGE_TIMEOUT)
        return resp.get("topics", [])
    except Exception:
        return []


def _is_slam_toolbox_running(nodes: list = None, topics: list = None) -> bool:
    """检测 slam_toolbox 是否在运行"""
    if nodes is None:
        nodes = _get_ros_nodes()
    if topics is None:
        topics = _get_ros_topics()

    slam_nodes = ["/slam_toolbox", "/async_slam_toolbox_node", "/sync_slam_toolbox_node"]
    slam_topics = ["/slam_toolbox/graph_visualization", "/slam_toolbox/scan_visualization"]

    node_match = any(key in n for n in nodes for key in slam_nodes)
    topic_match = any(t in topics for t in slam_topics)
    return node_match or topic_match


def _is_rrt_running(nodes: list = None) -> bool:
    """检测 RRT 探索节点是否在运行"""
    if nodes is None:
        nodes = _get_ros_nodes()

    rrt_keywords = ["global_rrt", "local_rrt", "filter", "assigner", "robot_picker"]
    return any(kw in n for n in nodes for kw in rrt_keywords)


def _start_command(command: str, timeout: float = 15.0) -> dict:
    """通过 command_launcher 启动指令"""
    svc = f"/robot/start_{command}"
    try:
        resp = rosbridge.call_service(svc, args={}, timeout=timeout)
        return {"success": resp.get("success", False), "message": resp.get("message", "")}
    except TimeoutError:
        return {"success": False, "error": f"启动 {command} 超时", "hint": "确认 command_launcher 在运行"}
    except RuntimeError as e:
        err = str(e)
        if "已在运行" in err:
            return {"success": True, "message": f"{command} 已在运行", "already_running": True}
        return {"success": False, "error": err}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _publish_clicked_point(x: float, y: float, z: float = 0.0):
    """向 /clicked_point 发布一个 PointStamped 消息"""
    now_sec = int(time.time())
    msg = {
        "header": {
            "stamp": {"sec": now_sec, "nanosec": 0},
            "frame_id": "map",
        },
        "point": {
            "x": x,
            "y": y,
            "z": z,
        },
    }
    rosbridge.publish("/clicked_point", msg)


def _write_rgba_png(filepath: str, pixels: bytes, width: int, height: int):
    """纯 Python 写 RGBA PNG，仅用标准库 struct + zlib，无需 PIL
    pixels: bytes, 长度 = width * height * 4 (RGBA)
    """

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    # color_type=6 表示 RGBA
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    # IDAT: 每行前加 filter byte 0x00
    raw_rows = []
    stride = width * 4
    for row in range(height):
        raw_rows.append(b"\x00" + pixels[row * stride:(row + 1) * stride])
    idat = _chunk(b"IDAT", zlib.compress(b"".join(raw_rows), 9))
    iend = _chunk(b"IEND", b"")

    with open(filepath, "wb") as f:
        f.write(sig + ihdr + idat + iend)


def _scale_up_map(pixels_rgba: bytearray, src_w: int, src_h: int, scale: int) -> tuple:
    """将 RGBA 像素数据放大 scale 倍（最近邻）
    返回 (new_pixels, new_w, new_h)
    """
    dst_w = src_w * scale
    dst_h = src_h * scale
    dst = bytearray(dst_w * dst_h * 4)
    for sy in range(src_h):
        for sx in range(src_w):
            src_idx = (sy * src_w + sx) * 4
            r, g, b, a = pixels_rgba[src_idx], pixels_rgba[src_idx+1], pixels_rgba[src_idx+2], pixels_rgba[src_idx+3]
            for dy in range(scale):
                for dx in range(scale):
                    dst_idx = ((sy * scale + dy) * dst_w + (sx * scale + dx)) * 4
                    dst[dst_idx] = r
                    dst[dst_idx+1] = g
                    dst[dst_idx+2] = b
                    dst[dst_idx+3] = a
    return bytes(dst), dst_w, dst_h


def _render_map_rgba(data: list, width: int, height: int) -> bytearray:
    """将 OccupancyGrid data 渲染为 RGBA 像素（含翻转），带彩色增强
    颜色方案：
      -1/255 未知 → 浅灰 (220,220,220)
      0      空闲 → 白色 (255,255,255)
      100    障碍 → 深蓝黑 (20,20,40)
      1-99   半占据 → 蓝绿渐变
    """
    rgba = bytearray(width * height * 4)
    for i, val in enumerate(data):
        idx = i * 4
        if val == -1 or val == 255:  # 未知
            rgba[idx] = 220; rgba[idx+1] = 220; rgba[idx+2] = 225; rgba[idx+3] = 255
        elif val == 0:  # 空闲
            rgba[idx] = 255; rgba[idx+1] = 255; rgba[idx+2] = 255; rgba[idx+3] = 255
        elif val == 100:  # 完全占据 → 深色
            rgba[idx] = 20; rgba[idx+1] = 20; rgba[idx+2] = 40; rgba[idx+3] = 255
        else:
            # 1-99 半占据：浅蓝到深蓝渐变
            t = val / 100.0
            r = int(255 * (1 - t) + 40 * t)
            g = int(255 * (1 - t) + 60 * t)
            b = int(255 * (1 - t * 0.6))
            rgba[idx] = r; rgba[idx+1] = g; rgba[idx+2] = b; rgba[idx+3] = 255

    # OccupancyGrid 从左下角开始，图片从左上角 → 翻转行
    flipped = bytearray(width * height * 4)
    stride = width * 4
    for row in range(height):
        src_start = row * stride
        dst_start = (height - 1 - row) * stride
        flipped[dst_start:dst_start + stride] = rgba[src_start:src_start + stride]

    return flipped


def _draw_grid_lines(pixels_rgba: bytearray, width: int, height: int,
                     resolution: float, origin_x: float, origin_y: float,
                     grid_meters: float = 1.0):
    """在 RGBA 像素上画网格线（每 grid_meters 米一条），用于辅助查看尺度
    线条颜色：半透明淡蓝 (180, 200, 220, 80)
    """
    cells_per_line = max(1, int(grid_meters / resolution))
    # 计算 origin 在像素中的偏移
    ox_cells = int(-origin_x / resolution) if resolution > 0 else 0
    oy_cells = int(-origin_y / resolution) if resolution > 0 else 0

    line_r, line_g, line_b, line_a = 180, 200, 220, 80

    # 竖线
    x = ox_cells % cells_per_line
    while x < width:
        for y in range(height):
            idx = (y * width + x) * 4
            # 仅在非障碍区域画线（不遮挡墙壁）
            if pixels_rgba[idx] > 100:
                pixels_rgba[idx] = line_r
                pixels_rgba[idx+1] = line_g
                pixels_rgba[idx+2] = line_b
                pixels_rgba[idx+3] = line_a + pixels_rgba[idx+3] * (255 - line_a) // 255
        x += cells_per_line

    # 横线（注意图已翻转，从顶部开始）
    y = (height - oy_cells % cells_per_line) % cells_per_line
    while y < height:
        for x_pos in range(width):
            idx = (y * width + x_pos) * 4
            if pixels_rgba[idx] > 100:
                pixels_rgba[idx] = line_r
                pixels_rgba[idx+1] = line_g
                pixels_rgba[idx+2] = line_b
                pixels_rgba[idx+3] = line_a + pixels_rgba[idx+3] * (255 - line_a) // 255
        y += cells_per_line


def _get_robot_position() -> dict:
    """获取机器人当前位置"""
    try:
        msg = rosbridge.subscribe_once("/odom", "nav_msgs/msg/Odometry", timeout=5.0)
        if msg:
            pos = msg.get("pose", {}).get("pose", {}).get("position", {})
            return {"x": pos.get("x", 0.0), "y": pos.get("y", 0.0)}
    except Exception:
        pass
    return {"x": 0.0, "y": 0.0}


# ═══════════════════ 工具 1：一键自主建图 ═══════════════════


@ToolRegistry.register(
    name="start_slam_exploration",
    display_name="🗺️ 一键自主建图",
    description=(
        "一键启动 SLAM 自主探索建图。自动完成: 启动 slam_toolbox → 启动 RRT 探索 → 发布探索区域。"
        "当用户要求'自主建图'、'自动建图'、'SLAM 建图'、'开始探索'时使用。"
        "默认探索范围 20m×20m，机器人会在雷达可达范围内自动避障探索。"
        "⚠️ 已知问题：R550A 上 RRT 与 slam_toolbox 存在 QoS 不兼容"
        "（TRANSIENT_LOCAL vs VOLATILE），filter 可能卡在 Waiting for the global map。"
        "如遇此问题，建议改用手动遥控建图（启动 slam_toolbox + 键盘控制）。"
    ),
    parameters={
        "range_size": {
            "type": "number",
            "description": "探索区域的半边长（米），默认 10 表示 20m×20m 范围",
            "default": 10,
        },
        "reset_map": {
            "type": "boolean",
            "description": "启动前是否重置已有地图，默认 true",
            "default": True,
        },
    },
)
def start_slam_exploration(range_size: float = 10, reset_map: bool = True) -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    steps_log = []

    # ── 第1步：检测并启动 slam_toolbox ──
    nodes = _get_ros_nodes()
    topics = _get_ros_topics()

    slam_running = _is_slam_toolbox_running(nodes, topics)

    if slam_running and not reset_map:
        steps_log.append("slam_toolbox 已在运行，跳过启动")
    elif slam_running and reset_map:
        # 需要重置：先停止再重启 slam_toolbox，确保全新地图
        steps_log.append("slam_toolbox 已在运行，执行 stop→start 重置地图")
        try:
            rosbridge.call_service("/robot/stop_slam_toolbox", args={}, timeout=ROSBRIDGE_TIMEOUT)
            time.sleep(2)
        except Exception:
            pass
        # 同时也停掉 RRT（它依赖 slam_toolbox）
        try:
            rosbridge.call_service("/robot/stop_rrt_slam", args={}, timeout=ROSBRIDGE_TIMEOUT)
            time.sleep(2)
        except Exception:
            pass
        result = _start_command("slam_toolbox")
        if result.get("success"):
            steps_log.append(f"slam_toolbox 已重新启动: {result.get('message', '')}")
            time.sleep(5)
        else:
            return {
                "success": False,
                "error": f"重启 slam_toolbox 失败: {result.get('error', '')}",
                "steps": steps_log,
            }
    else:
        result = _start_command("slam_toolbox")
        if result.get("success"):
            steps_log.append(f"slam_toolbox 已启动: {result.get('message', '')}")
            time.sleep(5)
        else:
            return {
                "success": False,
                "error": f"启动 slam_toolbox 失败: {result.get('error', '')}",
                "hint": result.get("hint", "确认 command_launcher 在运行"),
                "steps": steps_log,
            }

    # ── 第2步：检测并启动 RRT 探索 ──
    nodes = _get_ros_nodes()  # 刷新节点列表
    rrt_running = _is_rrt_running(nodes)

    if rrt_running and not reset_map:
        steps_log.append("RRT 探索节点已在运行，跳过启动")
    else:
        # 如果 RRT 还在跑但需要重置，先停掉
        if rrt_running:
            try:
                rosbridge.call_service("/robot/stop_rrt_slam", args={}, timeout=ROSBRIDGE_TIMEOUT)
                steps_log.append("已停止旧的 RRT 探索")
                time.sleep(2)
            except Exception:
                pass
        result = _start_command("rrt_slam", timeout=20.0)
        if result.get("success"):
            steps_log.append(f"RRT 探索已启动: {result.get('message', '')}")
            # 等待 RRT 各节点就绪（Nav2 lifecycle 激活 + TF 建立需要较长时间）
            # 原 8 秒不够，日志显示 global_costmap 激活经常超过 10 秒
            time.sleep(15)
        else:
            return {
                "success": False,
                "error": f"启动 RRT 探索失败: {result.get('error', '')}",
                "hint": result.get("hint", ""),
                "steps": steps_log,
            }

    # ── 第3步：获取机器人位置 ──
    robot_pos = _get_robot_position()
    cx, cy = robot_pos["x"], robot_pos["y"]
    steps_log.append(f"机器人当前位置: ({cx:.2f}, {cy:.2f})")

    # ── 第4步：发布 5 个点（4 个边界 + 1 个起始点）──
    r = range_size
    # 顺时针发布 4 个边界点（以机器人位置为中心）
    boundary_points = [
        (cx - r, cy + r),   # 左上
        (cx + r, cy + r),   # 右上
        (cx + r, cy - r),   # 右下
        (cx - r, cy - r),   # 左下
    ]

    for i, (bx, by) in enumerate(boundary_points):
        _publish_clicked_point(bx, by)
        steps_log.append(f"边界点 {i+1}: ({bx:.1f}, {by:.1f})")
        time.sleep(1.0)  # 间隔 1 秒确保节点接收

    # 第5个点：随机树起始点（机器人当前位置）
    _publish_clicked_point(cx, cy)
    steps_log.append(f"起始点: ({cx:.2f}, {cy:.2f})")

    return {
        "success": True,
        "range_m": f"{r*2}m × {r*2}m",
        "center": {"x": round(cx, 2), "y": round(cy, 2)},
        "steps": steps_log,
        "summary_cn": (
            f"自主探索建图已启动！探索范围 {r*2}m×{r*2}m，以机器人位置为中心。"
            f"机器人将自动导航探索并建图，完成后会自动停止。"
            f"使用 save_slam_map 保存地图。"
        ),
    }


# ═══════════════════ 工具 2：停止自主建图 ═══════════════════


@ToolRegistry.register(
    name="stop_slam_exploration",
    display_name="🛑 停止自主建图",
    description=(
        "停止当前正在进行的自主探索建图。会依次停止 RRT 探索和 SLAM 建图。"
        "当用户要求'停止建图'、'结束探索'、'停下来'时使用。"
    ),
    parameters={
        "stop_slam": {
            "type": "boolean",
            "description": "是否同时停止 SLAM 建图节点，默认 false（仅停止探索，保留建图）",
            "default": False,
        },
    },
)
def stop_slam_exploration(stop_slam: bool = False) -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    steps_log = []

    # 停止 RRT
    result = _start_command.__wrapped__ if hasattr(_start_command, '__wrapped__') else None
    svc = "/robot/stop_rrt_slam"
    try:
        resp = rosbridge.call_service(svc, args={}, timeout=ROSBRIDGE_TIMEOUT)
        ok = resp.get("success", False)
        msg = resp.get("message", "")
        steps_log.append(f"停止 RRT: {'成功' if ok else '失败'} - {msg}")
    except Exception as e:
        steps_log.append(f"停止 RRT 失败: {e}")

    # 可选：停止 SLAM
    if stop_slam:
        svc = "/robot/stop_slam_toolbox"
        try:
            resp = rosbridge.call_service(svc, args={}, timeout=ROSBRIDGE_TIMEOUT)
            ok = resp.get("success", False)
            msg = resp.get("message", "")
            steps_log.append(f"停止 SLAM: {'成功' if ok else '失败'} - {msg}")
        except Exception as e:
            steps_log.append(f"停止 SLAM 失败: {e}")

    return {
        "success": True,
        "steps": steps_log,
        "summary_cn": "已停止自主探索" + ("和 SLAM 建图" if stop_slam else "，SLAM 建图仍在运行"),
    }


# ═══════════════════ 工具 3：保存地图为图片 ═══════════════════


@ToolRegistry.register(
    name="save_slam_map",
    display_name="💾 保存 SLAM 地图",
    description=(
        "保存当前 SLAM 建图结果为高清 PNG 图片。"
        "默认 4 倍放大（每个栅格 4×4 像素），可调节 scale 参数提高清晰度。"
        "支持彩色渲染（障碍物深蓝、空闲白色、未知浅灰）和网格线。"
        "当用户要求'保存地图'、'导出地图'、'地图截图'、'高清地图'时使用。"
    ),
    parameters={
        "save_ros_map": {
            "type": "boolean",
            "description": "是否调用 ROS save_map 保存标准地图文件（.pgm+.yaml），默认 true",
            "default": True,
        },
        "get_image": {
            "type": "boolean",
            "description": "是否从 /map 话题获取地图并转为图片返回，默认 true",
            "default": True,
        },
        "scale": {
            "type": "integer",
            "description": "放大倍数，默认 4（每栅格 4×4 像素）。可设 1-8，越大越清晰但文件越大",
            "default": 4,
        },
        "show_grid": {
            "type": "boolean",
            "description": "是否显示 1 米网格线，默认 true",
            "default": True,
        },
    },
)
def save_slam_map(save_ros_map: bool = True, get_image: bool = True,
                  scale: int = 4, show_grid: bool = True) -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 限制 scale 范围
    scale = max(1, min(8, scale))

    result = {"success": True}
    steps_log = []

    # ── 方式1：调用厂商 save_map ──
    if save_ros_map:
        try:
            resp = rosbridge.call_service(
                "/robot/start_save_map", args={}, timeout=15.0
            )
            ok = resp.get("success", False)
            msg = resp.get("message", "")
            steps_log.append(f"ROS save_map: {'成功' if ok else '失败'} - {msg}")
            result["ros_save"] = {"success": ok, "message": msg}
        except Exception as e:
            steps_log.append(f"ROS save_map 失败: {e}")
            result["ros_save"] = {"success": False, "error": str(e)}

    # ── 方式2：从 /map 话题获取地图数据，转为高清 PNG ──
    if get_image:
        try:
            map_msg = rosbridge.subscribe_once(
                "/map", "nav_msgs/msg/OccupancyGrid", timeout=10.0
            )
            if map_msg:
                info = map_msg.get("info", {})
                width = info.get("width", 0)
                height = info.get("height", 0)
                resolution = info.get("resolution", 0.05)
                origin = info.get("origin", {}).get("position", {})
                origin_x = origin.get("x", 0.0)
                origin_y = origin.get("y", 0.0)
                data = map_msg.get("data", [])

                if width > 0 and height > 0 and data:
                    # 1) 渲染为彩色 RGBA（含翻转）
                    rgba = _render_map_rgba(data, width, height)

                    # 2) 画网格线（在原始分辨率上画，放大后线条也清晰）
                    if show_grid:
                        _draw_grid_lines(rgba, width, height,
                                         resolution, origin_x, origin_y,
                                         grid_meters=1.0)

                    # 3) 放大
                    if scale > 1:
                        final_pixels, final_w, final_h = _scale_up_map(
                            rgba, width, height, scale
                        )
                    else:
                        final_pixels = bytes(rgba)
                        final_w, final_h = width, height

                    # 4) 保存
                    static_dir = os.path.join(
                        os.path.dirname(os.path.dirname(__file__)),
                        "static", "maps"
                    )
                    os.makedirs(static_dir, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"map_{ts}.png"
                    filepath = os.path.join(static_dir, filename)
                    url_path = f"/maps/{filename}"

                    _write_rgba_png(filepath, final_pixels, final_w, final_h)

                    file_size = os.path.getsize(filepath)
                    result["map_image"] = {
                        "format": "png",
                        "original_size": f"{width}×{height}",
                        "output_size": f"{final_w}×{final_h}",
                        "scale": scale,
                        "resolution_m": resolution,
                        "origin": {
                            "x": round(origin_x, 2),
                            "y": round(origin_y, 2),
                        },
                        "real_area": f"{width*resolution:.1f}m × {height*resolution:.1f}m",
                        "file": filepath,
                        "url": url_path,
                        "size_kb": round(file_size / 1024, 1),
                    }
                    steps_log.append(
                        f"高清地图已保存: {filename} "
                        f"({final_w}×{final_h}px, {scale}x放大, "
                        f"原始 {width}×{height} @ {resolution}m/格, "
                        f"实际 {width*resolution:.1f}m×{height*resolution:.1f}m, "
                        f"{file_size/1024:.1f}KB)"
                    )
                else:
                    steps_log.append("收到地图消息但数据为空")
                    result["map_image"] = None
            else:
                steps_log.append("未收到 /map 数据（超时）")
                result["map_image"] = None
        except Exception as e:
            steps_log.append(f"获取地图图片失败: {e}")
            result["map_image"] = None

    result["steps"] = steps_log
    result["summary_cn"] = "；".join(steps_log) if steps_log else "无操作"
    return result


# ═══════════════════ 工具 4：查询自主建图进度 ═══════════════════


def _get_frontier_count(timeout: float = 5.0) -> dict:
    """
    监听 /filtered_goal_points (PointArray) 获取剩余前沿点数量。
    这是判断 RRT 建图是否完成的核心信号：
      - points 非空 → 还有未探索区域
      - points 为空 → 所有区域已探索，建图完成
      - 超时无消息 → RRT 未运行或 topic 不存在
    """
    result = {"received": False, "count": -1}
    event = threading.Event()

    def _on_frontiers(msg):
        points = msg.get("points", [])
        result["received"] = True
        result["count"] = len(points)
        event.set()

    try:
        rosbridge.subscribe(
            "/filtered_goal_points",
            "wheeltec_rrt_msg/msg/PointArray",
            _on_frontiers,
            queue_length=1,
        )
        event.wait(timeout=timeout)
    except Exception:
        pass
    finally:
        try:
            rosbridge.unsubscribe("/filtered_goal_points")
        except Exception:
            pass

    return result


@ToolRegistry.register(
    name="check_exploration_progress",
    display_name="📊 建图进度",
    description=(
        "查询当前自主探索建图的进度。核心通过 /filtered_goal_points 判断："
        "该 topic 发布剩余待探索的前沿点，当 points 为空时表示建图完成。"
        "同时检测 SLAM、RRT 运行状态、地图更新、机器人移动等信息。"
        "当用户询问'建图进度'、'探索到哪了'、'地图多大了'、'建图完成了吗'时使用。"
    ),
    parameters={},
)
def check_exploration_progress() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    nodes = _get_ros_nodes()
    topics = _get_ros_topics()

    slam_running = _is_slam_toolbox_running(nodes, topics)
    rrt_running = _is_rrt_running(nodes)
    has_map = "/map" in topics

    # ── 核心：监听 /filtered_goal_points 判断前沿点 ──
    frontier = _get_frontier_count(timeout=5.0)
    frontier_received = frontier["received"]
    frontier_count = frontier["count"]

    # 检查地图是否在活跃更新
    map_active = False
    map_updates = 0
    if has_map:
        count = [0]
        event = threading.Event()

        def _on_map(msg):
            count[0] += 1
            event.set()

        try:
            rosbridge.subscribe("/map", "nav_msgs/msg/OccupancyGrid", _on_map, queue_length=1)
            event.wait(timeout=3.0)
        except Exception:
            pass
        finally:
            try:
                rosbridge.unsubscribe("/map")
            except Exception:
                pass

        map_active = count[0] > 0
        map_updates = count[0]

    # 获取地图元数据
    map_info = {}
    if has_map:
        try:
            meta = rosbridge.subscribe_once("/map_metadata", "nav_msgs/msg/MapMetaData", timeout=5.0)
            if meta:
                w = meta.get("width", 0)
                h = meta.get("height", 0)
                res = meta.get("resolution", 0.05)
                map_info = {
                    "width_cells": w,
                    "height_cells": h,
                    "resolution_m": res,
                    "width_m": round(w * res, 1),
                    "height_m": round(h * res, 1),
                }
        except Exception:
            pass

    # 检查机器人是否在移动
    moving = False
    try:
        cmd_vel = rosbridge.subscribe_once("/cmd_vel", "geometry_msgs/msg/Twist", timeout=2.0)
        if cmd_vel:
            lx = abs(cmd_vel.get("linear", {}).get("x", 0))
            az = abs(cmd_vel.get("angular", {}).get("z", 0))
            moving = (lx > 0.01) or (az > 0.01)
    except Exception:
        pass

    # ── 组装状态描述 ──
    parts = []
    if slam_running:
        parts.append("SLAM Toolbox 运行中")
    else:
        parts.append("SLAM 未运行")

    if rrt_running:
        parts.append("RRT 探索运行中")
    else:
        parts.append("RRT 探索未运行")

    # 前沿点信息
    if frontier_received:
        if frontier_count == 0:
            parts.append("前沿点为 0，所有区域已探索完毕，建图完成")
        else:
            parts.append(f"剩余 {frontier_count} 个待探索前沿点")
    elif rrt_running:
        parts.append("未收到前沿点数据（/filtered_goal_points 无响应）")

    if map_active:
        parts.append(f"地图正在更新（3秒内 {map_updates} 次）")
    elif has_map:
        parts.append("地图存在但无新更新")
    else:
        parts.append("无地图数据")

    if map_info:
        parts.append(f"地图尺寸 {map_info.get('width_m', '?')}m×{map_info.get('height_m', '?')}m")

    if moving:
        parts.append("机器人正在移动")
    else:
        parts.append("机器人静止中")

    # ── 判断整体阶段（前沿点是最核心的判据） ──
    exploration_complete = frontier_received and frontier_count == 0

    if exploration_complete:
        phase = "complete"
        phase_cn = "建图已完成，所有前沿点已探索完毕，可保存地图"
    elif slam_running and rrt_running and frontier_received and frontier_count > 0 and moving:
        phase = "exploring"
        phase_cn = f"正在探索建图中，还有 {frontier_count} 个前沿点待探索"
    elif slam_running and rrt_running and not moving:
        phase = "paused"
        phase_cn = "RRT 运行中但机器人静止，可能在规划路径或等待目标"
    elif slam_running and not rrt_running:
        phase = "slam_only"
        phase_cn = "仅 SLAM 在运行，RRT 探索未启动"
    elif not slam_running:
        phase = "idle"
        phase_cn = "建图未启动"
    else:
        phase = "unknown"
        phase_cn = "状态不确定"

    return {
        "success": True,
        "slam_running": slam_running,
        "rrt_running": rrt_running,
        "exploration_complete": exploration_complete,
        "frontier_count": frontier_count if frontier_received else None,
        "frontier_received": frontier_received,
        "map_active": map_active,
        "map_updates_3s": map_updates,
        "robot_moving": moving,
        "map_info": map_info,
        "phase": phase,
        "phase_cn": phase_cn,
        "summary_cn": "；".join(parts),
    }
