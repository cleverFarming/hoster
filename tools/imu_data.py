"""工具：通过 rosbridge 订阅 /imu/data_raw 获取 IMU 姿态数据"""

import math

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

IMU_TOPICS = [
    ("/imu/data_raw", "sensor_msgs/msg/Imu"),
    ("/imu", "sensor_msgs/msg/Imu"),
]


def _quat_to_rpy_deg(ox: float, oy: float, oz: float, ow: float) -> tuple:
    """四元数转 roll/pitch/yaw 角度（度）"""
    # roll (x), pitch (y), yaw (z)
    sinr_cosp = 2 * (ow * ox + oy * oz)
    cosr_cosp = 1 - 2 * (ox * ox + oy * oy)
    roll = math.degrees(math.atan2(sinr_cosp, cosr_cosp))

    sinp = 2 * (ow * oy - oz * ox)
    pitch = math.degrees(math.asin(max(-1, min(1, sinp))))

    siny_cosp = 2 * (ow * oz + ox * oy)
    cosy_cosp = 1 - 2 * (oy * oy + oz * oz)
    yaw = math.degrees(math.atan2(siny_cosp, cosy_cosp))

    return round(roll, 1), round(pitch, 1), round(yaw, 1)


def _parse_imu(msg: dict) -> dict:
    """解析 sensor_msgs/Imu"""
    ori = msg.get("orientation", {}) or {}
    ang = msg.get("angular_velocity", {}) or {}
    lin = msg.get("linear_acceleration", {}) or {}

    ox = ori.get("x", 0) or 0
    oy = ori.get("y", 0) or 0
    oz = ori.get("z", 0) or 0
    ow = ori.get("w", 1) or 1

    roll, pitch, yaw = _quat_to_rpy_deg(ox, oy, oz, ow)

    return {
        "frame_id": msg.get("header", {}).get("frame_id", "imu"),
        "orientation_deg": {"roll": roll, "pitch": pitch, "yaw": yaw},
        "angular_velocity": {
            "x": round(ang.get("x", 0) or 0, 4),
            "y": round(ang.get("y", 0) or 0, 4),
            "z": round(ang.get("z", 0) or 0, 4),
        },
        "linear_acceleration": {
            "x": round(lin.get("x", 0) or 0, 3),
            "y": round(lin.get("y", 0) or 0, 3),
            "z": round(lin.get("z", 0) or 0, 3),
        },
        "tilt_cn": f"仰俯 {pitch}°，侧倾 {roll}°，航向 {yaw}°",
    }


@ToolRegistry.register(
    name="get_imu_data",
    display_name="📐 IMU 姿态",
    description="获取 IMU 姿态数据（姿态角、角速度、线加速度）。当用户询问机器人倾斜、姿态、IMU 时使用",
    parameters={},
    required=[],
)
def get_imu_data() -> dict:
    """订阅 /imu/data_raw 获取 Imu 消息"""
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    for topic, msg_type in IMU_TOPICS:
        try:
            msg = rosbridge.subscribe_once(topic, msg_type, timeout=ROSBRIDGE_TIMEOUT)
            if msg:
                parsed = _parse_imu(msg)
                return {
                    "success": True,
                    "topic": topic,
                    **parsed,
                    "summary_cn": parsed["tilt_cn"],
                }
        except ConnectionError as e:
            return {"success": False, "error": str(e)}
        except Exception:
            continue

    return {"success": False, "error": "未收到 IMU 数据", "hint": "请确认 /imu 或 /imu/data_raw 存在"}
