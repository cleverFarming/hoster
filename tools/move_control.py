"""工具：通过 /cmd_vel 控制小车移动（等效键盘遥控）

原理：wheeltec_keyboard 节点本质是往 /cmd_vel 发 geometry_msgs/msg/Twist，
本工具通过 rosbridge publish 实现同样效果，让大模型能直接控制小车。

Twist 消息结构:
  linear:  {x: 前进/后退, y: 横移(麦轮), z: 0}
  angular: {x: 0, y: 0, z: 左转/右转}
"""

import threading
import time

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

CMD_VEL_TOPIC = "/cmd_vel"
CMD_VEL_TYPE = "geometry_msgs/msg/Twist"

# 默认速度（与 wheeltec_keyboard 默认一致）
DEFAULT_LINEAR = 0.2    # m/s
DEFAULT_ANGULAR = 0.5   # rad/s

# 方向 → (linear.x, linear.y, angular.z) 乘数
# 对应键盘: u i o / j k l / m , .
DIRECTION_MAP = {
    "forward":       ( 1,  0,  0),   # i
    "backward":      (-1,  0,  0),   # ,
    "left":          ( 0,  0,  1),   # j  (原地左转)
    "right":         ( 0,  0, -1),   # l  (原地右转)
    "forward_left":  ( 1,  0,  1),   # u
    "forward_right": ( 1,  0, -1),   # o
    "backward_left": (-1,  0,  1),   # m
    "backward_right":(-1,  0, -1),   # .
    "stop":          ( 0,  0,  0),   # k / space
    # 麦轮横移（如果你的车支持）
    "strafe_left":   ( 0,  1,  0),
    "strafe_right":  ( 0, -1,  0),
}

# 用于定时停车的全局锁和定时器
_stop_timer: threading.Timer = None
_timer_lock = threading.Lock()


def _make_twist(lx: float = 0, ly: float = 0, az: float = 0) -> dict:
    """构造 Twist 消息"""
    return {
        "linear":  {"x": lx, "y": ly, "z": 0.0},
        "angular": {"x": 0.0, "y": 0.0, "z": az},
    }


def _publish_stop():
    """发送停车指令"""
    try:
        rosbridge.publish(CMD_VEL_TOPIC, _make_twist(0, 0, 0))
    except Exception:
        pass


def _move(direction: str, linear_speed: float, angular_speed: float,
          duration: float) -> dict:
    """执行移动并在 duration 秒后自动停车"""
    global _stop_timer

    multipliers = DIRECTION_MAP.get(direction)
    if not multipliers:
        return {
            "success": False,
            "error": f"未知方向: {direction}",
            "available": list(DIRECTION_MAP.keys()),
        }

    lx_mul, ly_mul, az_mul = multipliers
    lx = lx_mul * linear_speed
    ly = ly_mul * linear_speed
    az = az_mul * angular_speed

    twist = _make_twist(lx, ly, az)

    # 发送运动指令
    rosbridge.publish(CMD_VEL_TOPIC, twist)

    # 如果是 stop 指令，不需要定时器
    if direction == "stop":
        with _timer_lock:
            if _stop_timer:
                _stop_timer.cancel()
                _stop_timer = None
        return {
            "success": True,
            "action": "stop",
            "summary_cn": "已停车",
        }

    # 设置定时停车（取消之前的定时器）
    with _timer_lock:
        if _stop_timer:
            _stop_timer.cancel()
        _stop_timer = threading.Timer(duration, _publish_stop)
        _stop_timer.daemon = True
        _stop_timer.start()

    return {
        "success": True,
        "direction": direction,
        "linear_speed": linear_speed,
        "angular_speed": angular_speed,
        "duration": duration,
        "twist": twist,
        "summary_cn": f"{'前进' if lx > 0 else '后退' if lx < 0 else ''}{'左转' if az > 0 else '右转' if az < 0 else ''} "
                      f"线速度 {abs(lx):.2f}m/s 角速度 {abs(az):.2f}rad/s，持续 {duration} 秒后自动停车",
    }


@ToolRegistry.register(
    name="move_robot",
    display_name="🚗 控制小车移动",
    description=(
        "控制小车移动（前进、后退、左转、右转、停车等）。"
        "等效于键盘遥控，通过 /cmd_vel 发送速度指令。"
        "移动会在指定秒数后自动停车，确保安全。"
        "当用户要求小车前进、后退、转弯、停下、走一段路时使用。"
    ),
    parameters={
        "direction": {
            "type": "string",
            "enum": list(DIRECTION_MAP.keys()),
            "description": (
                "移动方向: forward(前进) backward(后退) "
                "left(左转) right(右转) "
                "forward_left(左前) forward_right(右前) "
                "backward_left(左后) backward_right(右后) "
                "stop(停车) "
                "strafe_left(左横移) strafe_right(右横移/麦轮)"
            ),
        },
        "linear_speed": {
            "type": "number",
            "description": "线速度 m/s，默认 0.2，建议范围 0.05~0.5",
            "default": 0.2,
        },
        "angular_speed": {
            "type": "number",
            "description": "角速度 rad/s，默认 0.5，建议范围 0.1~1.0",
            "default": 0.5,
        },
        "duration": {
            "type": "number",
            "description": "持续秒数，到时间后自动停车，默认 2 秒，建议范围 0.5~10",
            "default": 2,
        },
    },
    required=["direction"],
)
def move_robot(direction: str, linear_speed: float = DEFAULT_LINEAR,
               angular_speed: float = DEFAULT_ANGULAR,
               duration: float = 2.0) -> dict:
    """
    通过 rosbridge 向 /cmd_vel 发布 Twist 消息控制小车移动。
    duration 秒后自动发送零速停车，防止小车跑飞。
    """
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    # 安全限速
    linear_speed = max(0.0, min(abs(linear_speed), 1.0))
    angular_speed = max(0.0, min(abs(angular_speed), 2.0))
    duration = max(0.1, min(duration, 30.0))

    try:
        return _move(direction, linear_speed, angular_speed, duration)
    except ConnectionError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        # 出错也尝试停车
        _publish_stop()
        return {"success": False, "error": f"移动失败: {e}，已发送停车指令"}
