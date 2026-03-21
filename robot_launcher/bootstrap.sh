#!/bin/bash
# ============================================================
# R550A 最小引导脚本 —— 只启动 rosbridge + command_launcher
# 其他所有服务（SLAM、相机、建图等）通过网页 AI 控制
#
# 用法（在机器人 Docker 容器内）:
#   bash bootstrap.sh
# ============================================================

source /opt/ros/humble/setup.bash

echo "===== R550A 最小引导 ====="
echo ""

# ----- 清理已有进程 -----
if pgrep -f "command_launcher" > /dev/null 2>&1; then
    echo "[清理] 发现已有 command_launcher，正在清理..."
    pkill -f command_launcher 2>/dev/null
    sleep 1
fi

if pgrep -f "rosbridge" > /dev/null 2>&1; then
    echo "[清理] 发现已有 rosbridge，正在清理..."
    pkill -f rosbridge 2>/dev/null
    sleep 1
fi

# ----- 1. rosbridge WebSocket -----
echo "[1/2] 启动 rosbridge WebSocket（端口 9090）..."
ros2 launch rosbridge_server rosbridge_websocket_launch.xml > /tmp/rosbridge.log 2>&1 &
sleep 3

if pgrep -f "rosbridge" > /dev/null 2>&1; then
    echo "  ✓ rosbridge 已启动"
else
    echo "  ✗ rosbridge 启动失败，查看日志: tail -20 /tmp/rosbridge.log"
    exit 1
fi

# ----- 2. command_launcher -----
echo "[2/2] 启动 command_launcher（服务管理器）..."

# 自动查找 command_launcher.py 的位置
LAUNCHER_PATH=""
for p in \
    "$(dirname "$0")/command_launcher.py" \
    "/home/wheeltec/command_launcher.py" \
    "/home/wheeltec/wheeltec_ros2/command_launcher.py" \
; do
    if [ -f "$p" ]; then
        LAUNCHER_PATH="$p"
        break
    fi
done

if [ -z "$LAUNCHER_PATH" ]; then
    echo "  ✗ 找不到 command_launcher.py"
    echo "  请将 command_launcher.py 放到以下任一位置:"
    echo "    - $(dirname "$0")/command_launcher.py"
    echo "    - /home/wheeltec/command_launcher.py"
    exit 1
fi

python3 "$LAUNCHER_PATH" > /tmp/command_launcher.log 2>&1 &
sleep 2

if pgrep -f "command_launcher" > /dev/null 2>&1; then
    echo "  ✓ command_launcher 已启动"
else
    echo "  ✗ command_launcher 启动失败，查看日志: tail -20 /tmp/command_launcher.log"
    exit 1
fi

# ----- 输出汇总 -----
IP=$(hostname -I | awk '{print $1}')

echo ""
echo "============================================"
echo "  引导完成！"
echo "============================================"
echo ""
echo "  小车 IP:     $IP"
echo "  WebSocket:   ws://$IP:9090"
echo ""
echo "  现在可以通过网页控制以下服务："
echo "    - SLAM 建图（slam_toolbox / gmapping / cartographer）"
echo "    - 相机 / 视频服务"
echo "    - 导航 / RRT 探索"
echo "    - 保存地图"
echo "    - 键盘控制 / 手柄控制"
echo ""
echo "  日志文件："
echo "    /tmp/rosbridge.log"
echo "    /tmp/command_launcher.log"
echo ""
echo "  停止引导服务：pkill -f rosbridge; pkill -f command_launcher"
echo "============================================"
