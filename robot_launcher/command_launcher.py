#!/usr/bin/env python3
"""
机器人端指令启动器 —— 通过 ROS2 服务接收指令，执行 ros2 launch

适配 WHEELTEC R550A：
  - SLAM launch 已包含底盘+雷达，不能重复启动
  - 互斥组：同时只能运行一种 SLAM / 底盘驱动
  - 依赖关系：RRT 需要先有 SLAM，web_video_server 需要先有 camera

用法（在机器人 Docker 容器内运行）:
  source /opt/ros/humble/setup.bash
  python3 command_launcher.py

依赖: rclpy std_srvs (ROS2 环境已有)
"""

import os
import signal
import subprocess
import threading
from datetime import datetime

import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger

# ═══════════════════ 日志目录 ═══════════════════
LOG_DIR = "/tmp"


# ═══════════════════ 命令定义 ═══════════════════

COMMANDS = {
    # ── 底盘、相机、雷达（独立启动，仅在不用 SLAM 时使用） ──
    "chassis": ["ros2", "launch", "turn_on_wheeltec_robot", "turn_on_wheeltec_robot.launch.py"],
    "camera":  ["ros2", "launch", "turn_on_wheeltec_robot", "wheeltec_camera.launch.py"],
    "lidar":   ["ros2", "launch", "turn_on_wheeltec_robot", "wheeltec_lidar.launch.py"],
    # ── 跟随 ──
    "laser_follower":  ["ros2", "launch", "simple_follower_ros2", "laser_follower.launch.py"],
    "line_follower":   ["ros2", "launch", "simple_follower_ros2", "line_follower.launch.py"],
    "visual_follower": ["ros2", "launch", "simple_follower_ros2", "visual_follower.launch.py"],
    # ── 建图（每种 SLAM 内部都包含底盘+雷达） ──
    "slam_gmapping":     ["ros2", "launch", "slam_gmapping", "slam_gmapping.launch.py"],
    "slam_toolbox":      ["ros2", "launch", "wheeltec_slam_toolbox", "online_sync.launch.py"],
    "slam_cartographer": ["ros2", "launch", "wheeltec_cartographer", "cartographer.launch.py"],
    "save_map":          ["ros2", "launch", "wheeltec_nav2", "save_map.launch.py"],
    # ── 导航 ──
    "nav2": ["ros2", "launch", "wheeltec_nav2", "wheeltec_nav2.launch.py"],
    # ── RRT 探索（需先启动 SLAM） ──
    "rrt_slam": ["ros2", "launch", "wheeltec_robot_rrt", "wheeltec_rrt_slam.launch.py"],
    # ── RTAB ──
    "rtab_slam": ["ros2", "launch", "wheeltec_robot_rtab", "wheeltec_slam_rtab.launch.py"],
    "rtab_nav":  ["ros2", "launch", "wheeltec_robot_rtab", "wheeltec_nav2_rtab.launch.py"],
    # ── 语音模块（M2 麦克风阵列） ──
    "mic_init": ["ros2", "launch", "wheeltec_mic_ros2", "mic_init.launch.py"],
    "mic_base": ["ros2", "launch", "wheeltec_mic_ros2", "base.launch.py"],
    # ── 辅助服务 ──
    "web_video_server": ["ros2", "run", "web_video_server", "web_video_server"],
    "joy":              ["ros2", "launch", "wheeltec_joy", "wheeltec_joy.launch.py"],
}

# ═══════════════════ 互斥与依赖规则（R550A 适配） ═══════════════════

# 互斥组：同一组内只能有一个在运行
# SLAM 类都包含底盘+雷达，互相冲突；与单独的 chassis/lidar 也冲突
MUTEX_GROUPS = {
    "hardware": ["chassis", "lidar", "slam_gmapping", "slam_toolbox", "slam_cartographer", "rtab_slam"],
}

# 启动某命令前，哪些命令必须已在运行
REQUIRES = {
    "rrt_slam": ["slam_toolbox"],
    "web_video_server": ["camera"],
    "mic_base": ["mic_init"],
}

# 启动 SLAM 时，自动先停掉这些冲突进程
AUTO_STOP_BEFORE = {
    "slam_gmapping":     ["chassis", "lidar"],
    "slam_toolbox":      ["chassis", "lidar"],
    "slam_cartographer": ["chassis", "lidar"],
    "rtab_slam":         ["chassis", "lidar"],
}


class CommandLauncherNode(Node):
    def __init__(self):
        super().__init__("command_launcher")
        self._procs = {}
        self._log_files = {}
        self._lock = threading.Lock()

        for name in COMMANDS:
            self.create_service(
                Trigger,
                f"/robot/start_{name}",
                lambda req, res, n=name: self._start_callback(req, res, n),
            )
            self.create_service(
                Trigger,
                f"/robot/stop_{name}",
                lambda req, res, n=name: self._stop_callback(req, res, n),
            )

        self.get_logger().info("command_launcher 已启动（R550A 适配版），可用服务:")
        for name in COMMANDS:
            self.get_logger().info(f"  /robot/start_{name}, /robot/stop_{name}")

    # ──────────── 辅助方法 ────────────

    def _is_running(self, name):
        proc = self._procs.get(name)
        return proc is not None and proc.poll() is None

    def _stop_process(self, name):
        # 级联停止：先停掉依赖此命令的其他命令
        for dependent, deps in REQUIRES.items():
            if name in deps and self._is_running(dependent):
                self.get_logger().info(f"级联停止 {dependent}（依赖 {name}）")
                self._stop_process(dependent)

        proc = self._procs.pop(name, None)
        if not proc or proc.poll() is not None:
            return f"{name} 未在运行"
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        # 关闭日志文件
        log_file = self._log_files.pop(name, None)
        if log_file:
            try:
                log_file.write(f"\n[{datetime.now().isoformat()}] 已停止 {name}\n")
                log_file.close()
            except Exception:
                pass

        self.get_logger().info(f"停止 {name}")
        return f"已停止 {name}"

    def _check_mutex(self, name):
        for group_name, members in MUTEX_GROUPS.items():
            if name in members:
                for other in members:
                    if other != name and self._is_running(other):
                        return (
                            f"无法启动 {name}：{other} 正在运行，两者互斥"
                            f"（{group_name} 组）。请先 stop_{other}"
                        )
        return None

    def _check_requires(self, name):
        for dep in REQUIRES.get(name, []):
            if not self._is_running(dep):
                return f"无法启动 {name}：依赖 {dep} 未在运行，请先 start_{dep}"
        return None

    def _auto_stop_conflicts(self, name):
        stopped = []
        for s in AUTO_STOP_BEFORE.get(name, []):
            if self._is_running(s):
                stopped.append(self._stop_process(s))
        return stopped

    # ──────────── 服务回调 ────────────

    def _start_callback(self, request, response, name: str):
        with self._lock:
            if self._is_running(name):
                response.success = False
                response.message = f"{name} 已在运行"
                return response

            cmd = COMMANDS.get(name)
            if not cmd:
                response.success = False
                response.message = f"未知指令: {name}"
                return response

            # 互斥检查
            err = self._check_mutex(name)
            if err:
                response.success = False
                response.message = err
                return response

            # 依赖检查
            err = self._check_requires(name)
            if err:
                response.success = False
                response.message = err
                return response

            # 自动停止冲突进程
            auto_stopped = self._auto_stop_conflicts(name)

            try:
                # 每个命令的日志输出到 /tmp/robot_logs/{name}.log
                log_path = os.path.join(LOG_DIR, f"{name}.log")
                log_file = open(log_path, "a")
                log_file.write(f"\n{'='*60}\n")
                log_file.write(f"[{datetime.now().isoformat()}] 启动 {name}\n")
                log_file.write(f"命令: {' '.join(cmd)}\n")
                log_file.write(f"{'='*60}\n")
                log_file.flush()

                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._procs[name] = proc
                self._log_files[name] = log_file
                msg = f"已启动 {name} (日志: {log_path})"
                if auto_stopped:
                    msg += f"（自动停止了: {'; '.join(auto_stopped)}）"
                response.success = True
                response.message = msg
                self.get_logger().info(f"启动 {name} (PID {proc.pid}, 日志 {log_path})")
            except Exception as e:
                response.success = False
                response.message = str(e)
        return response

    def _stop_callback(self, request, response, name: str):
        with self._lock:
            response.success = True
            response.message = self._stop_process(name)
        return response


def main():
    rclpy.init()
    node = CommandLauncherNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
