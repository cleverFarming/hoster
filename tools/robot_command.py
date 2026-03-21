"""工具：通过 rosbridge 调用机器人指令（启动/停止 launch）"""

from ._registry import ToolRegistry
from ._ws_client import rosbridge, is_rosbridge_connected
from ._constants import ROSBRIDGE_TIMEOUT

# 与 robot_launcher 的 COMMANDS 一致，用于校验和提示
AVAILABLE_COMMANDS = {
    "chassis": "打开底盘（⚠️ 与 SLAM 互斥，SLAM 已含底盘）",
    "camera": "打开相机",
    "lidar": "打开雷达（⚠️ 与 SLAM 互斥，SLAM 已含雷达）",
    "laser_follower": "雷达跟随",
    "line_follower": "视觉巡线",
    "visual_follower": "视觉跟踪",
    "slam_gmapping": "gmapping 建图（含底盘+雷达，与其他 SLAM 互斥）",
    "slam_toolbox": "slam_toolbox 建图（含底盘+雷达，与其他 SLAM 互斥）",
    "slam_cartographer": "cartographer 建图（含底盘+雷达，与其他 SLAM 互斥）",
    "save_map": "保存地图",
    "nav2": "2D 导航",
    "rrt_slam": "RRT 自主探索建图（⚠️ R550A 有 QoS 兼容问题，需先启动 slam_toolbox）",
    "rtab_slam": "RTAB-MAP 建图（含底盘+雷达，与其他 SLAM 互斥）",
    "rtab_nav": "RTAB-MAP 导航",
    "mic_init": "语音模块初始化（M2 麦克风串口通信 + 语音识别引擎）",
    "mic_base": "语音命令控制（唤醒+命令识别，需先启动 mic_init）",
    "web_video_server": "Web 视频服务（需先启动 camera）",
    "joy": "USB 手柄控制",
    "rosbridge": "rosbridge WebSocket 服务（端口 9090，网页通信桥梁）",
}


@ToolRegistry.register(
    name="robot_command",
    display_name="🤖 机器人指令",
    description="启动或停止机器人上的功能模块（底盘、相机、雷达、跟随、建图、导航等）。当用户要求打开/关闭某项功能时使用",
    parameters={
        "command": {
            "type": "string",
            "enum": list(AVAILABLE_COMMANDS.keys()),
            "description": "指令名称",
        },
        "action": {
            "type": "string",
            "enum": ["start", "stop"],
            "description": "start=启动, stop=停止",
        },
    },
    required=["command", "action"],
)
def robot_command(command: str, action: str) -> dict:
    """
    调用 /robot/start_xxx 或 /robot/stop_xxx 服务。
    需在机器人上运行 robot_launcher/command_launcher.py。
    """
    if not is_rosbridge_connected():
        return {"success": False, "error": "rosbridge 未连接"}

    if command not in AVAILABLE_COMMANDS:
        return {
            "success": False,
            "error": f"未知指令: {command}",
            "available": list(AVAILABLE_COMMANDS.keys()),
        }

    svc = f"/robot/{action}_{command}"

    try:
        resp = rosbridge.call_service(svc, args={}, timeout=ROSBRIDGE_TIMEOUT)
    except ConnectionError as e:
        return {"success": False, "error": str(e)}
    except TimeoutError as e:
        return {"success": False, "error": str(e), "hint": "确认机器人已运行 command_launcher 节点"}
    except RuntimeError as e:
        err = str(e)
        if "does not exist" in err or "not exist" in err:
            return {
                "success": False,
                "error": err,
                "hint": "机器人上未发现该服务。请在机器人上启动 command_launcher：source 工作空间后执行 python3 command_launcher.py",
            }
        return {"success": False, "error": err}

    # std_srvs/Trigger 响应: success, message
    ok = resp.get("success", False)
    msg = resp.get("message", "")

    return {
        "success": ok,
        "message": msg,
        "command": command,
        "action": action,
        "summary_cn": msg,
    }
