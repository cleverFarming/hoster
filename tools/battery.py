"""工具：通过 rosbridge 订阅电池状态 topic 获取机器人电量"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# 常见的电池 topic（按优先级）
BATTERY_TOPICS = [
    # 当前机器人使用的电量 topic
    ("/PowerVoltage",         "std_msgs/msg/Float32"),
    ("/battery",              "sensor_msgs/msg/BatteryState"),
    ("/battery_state",        "sensor_msgs/msg/BatteryState"),
    ("/mobile_base/sensors/battery", "sensor_msgs/msg/BatteryState"),
    ("/power_supply",         "sensor_msgs/msg/BatteryState"),
    # wheeltec 可能通过自定义 topic 发布电压
    ("/voltage",              "std_msgs/msg/Float32"),
    ("/wheeltec/battery",     "sensor_msgs/msg/BatteryState"),
]

# BatteryState.power_supply_status 枚举
_STATUS_MAP = {
    0: "未知",
    1: "充电中",
    2: "放电中",
    3: "未充电",
    4: "已充满",
}


def _parse_battery_state(msg: dict) -> dict:
    """解析 sensor_msgs/BatteryState"""
    voltage = msg.get("voltage", 0) or 0
    current = msg.get("current", 0) or 0
    percentage = msg.get("percentage", -1)
    # percentage 有时是 0~1，有时是 0~100
    if percentage is not None and 0 < percentage <= 1.0:
        percentage = percentage * 100
    charge = msg.get("charge", 0) or 0
    capacity = msg.get("capacity", 0) or 0
    status_code = msg.get("power_supply_status", 0) or 0
    status_text = _STATUS_MAP.get(status_code, "未知")

    result = {
        "voltage": round(voltage, 2),
        "current": round(current, 2),
        "status": status_text,
        "status_code": status_code,
    }

    if percentage is not None and percentage >= 0:
        result["percentage"] = round(percentage, 1)

    if capacity > 0:
        result["charge"] = round(charge, 2)
        result["capacity"] = round(capacity, 2)

    return result


def _parse_float_voltage(msg: dict) -> dict:
    """解析 std_msgs/Float32（仅电压）"""
    voltage = msg.get("data", 0) or 0
    return {"voltage": round(voltage, 2)}


def _estimate_percentage(voltage: float) -> float:
    """根据电压粗估电量百分比（12V 铅酸/锂电通用）"""
    if voltage <= 0:
        return -1
    # 常见的机器人电池电压范围
    if voltage > 20:
        # 24V 系统: 21V=0%, 29.4V=100%
        pct = (voltage - 21.0) / (29.4 - 21.0) * 100
    elif voltage > 10:
        # 12V 系统: 10.5V=0%, 12.6V=100%
        pct = (voltage - 10.5) / (12.6 - 10.5) * 100
    else:
        return -1
    return round(max(0, min(100, pct)), 1)


@ToolRegistry.register(
    name="get_battery_status",
    display_name="🔋 电池电量",
    description=(
        "获取机器人电池状态（电量百分比、电压、电流、充电状态）。"
        "当用户询问'电量多少'、'还有多少电'、'电池状态'、'需要充电吗'时使用。"
    ),
    parameters={},
    required=[],
)
def get_battery_status() -> dict:
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    for topic, msg_type in BATTERY_TOPICS:
        try:
            msg = rosbridge.subscribe_once(topic, msg_type, timeout=ROSBRIDGE_TIMEOUT)
            if not msg:
                continue

            # 根据消息类型解析
            if "BatteryState" in msg_type:
                parsed = _parse_battery_state(msg)
            else:
                parsed = _parse_float_voltage(msg)

            # 如果没有百分比但有电压，粗估
            if "percentage" not in parsed and parsed.get("voltage", 0) > 0:
                est = _estimate_percentage(parsed["voltage"])
                if est >= 0:
                    parsed["percentage"] = est
                    parsed["percentage_estimated"] = True

            # 生成中文摘要
            parts = []
            if "percentage" in parsed:
                pct = parsed["percentage"]
                parts.append(f"电量 {pct}%")
                if pct < 20:
                    parts.append("⚠️ 电量过低，建议尽快充电")
                elif pct < 50:
                    parts.append("电量偏低")
            if parsed.get("voltage"):
                parts.append(f"电压 {parsed['voltage']}V")
            if parsed.get("current"):
                parts.append(f"电流 {parsed['current']}A")
            if parsed.get("status") and parsed["status"] != "未知":
                parts.append(parsed["status"])

            return {
                "success": True,
                "topic": topic,
                **parsed,
                "summary_cn": "，".join(parts) if parts else f"电压 {parsed.get('voltage', '?')}V",
            }

        except ConnectionError as e:
            return {"success": False, "error": str(e)}
        except Exception:
            continue

    return {
        "success": False,
        "error": "未找到电池状态 topic",
        "hint": "请确认机器人发布了电池相关的 topic（/battery, /battery_state, /voltage 等）",
    }
