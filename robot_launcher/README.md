# 机器人指令启动器

将 `ros2 launch` / `ros2 run` 命令转为 ROS2 服务，供 hoster 通过 rosbridge WebSocket 远程调用。

## 部署到机器人（推荐：用 ros2 run）

1. 将整个 `robot_launcher` 目录复制到机器人工作空间 src 下，并改名为 `wheeltec_command_launcher`：
   ```bash
   # 在机器人上
   cd /home/wheeltec/wheeltec_ros2/src
   # 从本机拷过来或 git clone 后，确保目录结构为：
   #   wheeltec_command_launcher/
   #     command_launcher.py  setup.py  setup.cfg  package.xml  resource/...
   ```

2. 编译并 source：
   ```bash
   cd /home/wheeltec/wheeltec_ros2
   source /opt/ros/humble/setup.bash
   colcon build --packages-select wheeltec_command_launcher
   source install/setup.bash
   ```

3. 启动（与 rosbridge 并行）：
   ```bash
   nohup ros2 run wheeltec_command_launcher command_launcher >> /tmp/command_launcher.log 2>&1 &
   ```

4. 验证：`ros2 service list | grep robot`

## 若不用包、直接跑脚本

需先看报错：`cat /tmp/command_launcher.log`。常见是 `python3` 找不到 `rclpy`，需用 ROS2 环境里的 Python。可先前台跑一次：
   ```bash
   source /opt/ros/humble/setup.bash
   source /home/wheeltec/wheeltec_ros2/install/setup.bash
   python3 /home/wheeltec/command_launcher.py
   ```
   若有 `ModuleNotFoundError: No module named 'rclpy'`，请改用上面「用 ros2 run」方式。

## 暴露的服务

每个指令对应两个服务（Trigger 类型，请求为空）：

| 指令 | 服务 | 说明 |
|------|------|------|
| chassis | /robot/start_chassis, /robot/stop_chassis | 底盘 |
| camera | /robot/start_camera, /robot/stop_camera | 相机 |
| lidar | /robot/start_lidar, /robot/stop_lidar | 雷达 |
| laser_follower | /robot/start_laser_follower, ... | 雷达跟随 |
| line_follower | ... | 视觉巡线 |
| visual_follower | ... | 视觉跟踪 |
| slam_gmapping | ... | gmapping 建图 |
| slam_toolbox | ... | slam_toolbox 建图 |
| slam_cartographer | ... | cartographer 建图 |
| save_map | ... | 保存地图 |
| nav2 | ... | 2D 导航 |
| rrt_slam | ... | RRT 探索（需先 slam_toolbox） |
| rtab_slam | ... | RTAB 建图 |
| rtab_nav | ... | RTAB 导航 |
| web_video_server | ... | Web 视频 |
| joy | ... | 手柄 |

## hoster 调用

用户在对话中说「打开雷达跟随」「启动 gmapping 建图」等，AI 会调用 `robot_command` 工具，经 rosbridge 调用上述服务。
