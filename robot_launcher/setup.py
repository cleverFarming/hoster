# 放入机器人 wheeltec_ros2/src 后：colcon build --packages-select wheeltec_command_launcher
# 然后：source install/setup.bash && ros2 run wheeltec_command_launcher command_launcher
from setuptools import setup

package_name = "wheeltec_command_launcher"

setup(
    name=package_name,
    version="0.0.1",
    packages=[],
    py_modules=["command_launcher"],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "command_launcher = command_launcher:main",
        ],
    },
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
    ],
)
