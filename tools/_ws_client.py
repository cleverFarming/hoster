"""rosbridge WebSocket 客户端 —— 与 ROS2 机器人通信

rosbridge 协议参考:
  https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/ROSBRIDGE_PROTOCOL.md

核心操作:
  call_service  → service_response   (请求-响应，用于工具调用)
  publish       → topic              (单向推送)
  subscribe     → 持续接收 topic 消息 (传感器订阅等)
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import websocket


# ═══════════════════ RosbridgeClient ═══════════════════

class RosbridgeClient:
    """
    线程安全的 rosbridge v2.0 WebSocket 客户端。

    - call_service():  同步调用 ROS2 服务（阻塞等待响应）
    - publish():       向 topic 发布消息
    - subscribe():     订阅 topic 并注册回调
    - 内置自动重连 & 心跳
    """

    def __init__(self, url: str = "ws://localhost:9090"):
        self.url = url
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 服务调用: id → {"event": Event, "result": dict}
        self._pending: Dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # topic 订阅: topic → callback(msg)
        self._subscribers: Dict[str, Callable] = {}

    # ──────────── 生命周期 ────────────

    def connect(self, timeout: float = 5.0):
        """启动后台线程连接 rosbridge（阻塞直到连接成功或超时）"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="RosbridgeClient")
        self._thread.start()
        if not self._connected.wait(timeout=timeout):
            print(f"[rosbridge] ⚠ 连接超时 ({timeout}s)，将在后台持续重试: {self.url}")

    def disconnect(self):
        """断开连接并停止重连"""
        self._stop.set()
        if self._ws:
            self._ws.close()
        self._connected.clear()
        # 唤醒所有等待中的 service call
        with self._pending_lock:
            for p in self._pending.values():
                p["event"].set()
            self._pending.clear()
        print("[rosbridge] 已断开")

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ──────────── 核心: 调用 ROS2 服务 ────────────

    def call_service(self, service: str, args: Optional[dict] = None,
                     timeout: float = 10.0) -> dict:
        """
        同步调用 ROS2 服务，阻塞等待响应。

        Args:
            service:  服务名，如 "/farm/get_sensor"
            args:     服务参数字典
            timeout:  超时秒数

        Returns:
            服务响应中的 values 字典

        Raises:
            ConnectionError: 未连接
            TimeoutError:    超时
            RuntimeError:    服务返回失败
        """
        if not self._connected.is_set():
            raise ConnectionError(f"未连接到 rosbridge ({self.url})")

        msg_id = str(uuid.uuid4())
        event = threading.Event()

        with self._pending_lock:
            self._pending[msg_id] = {"event": event, "result": None}

        request = {
            "op": "call_service",
            "id": msg_id,
            "service": service,
        }
        if args:
            request["args"] = args

        try:
            self._ws.send(json.dumps(request, ensure_ascii=False))
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise ConnectionError(f"发送失败: {e}")

        # 阻塞等待响应
        if not event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise TimeoutError(f"调用服务 {service} 超时 ({timeout}s)")

        with self._pending_lock:
            resp = self._pending.pop(msg_id, {}).get("result", {})

        if not resp:
            raise RuntimeError(f"服务 {service} 无响应")

        if not resp.get("result", False):
            raise RuntimeError(
                f"服务 {service} 返回错误: "
                f"{json.dumps(resp.get('values', {}), ensure_ascii=False)}"
            )

        return resp.get("values", {})

    # ──────────── 发布 topic ────────────

    def publish(self, topic: str, msg: dict):
        """向 ROS2 topic 发布消息"""
        if not self._connected.is_set():
            raise ConnectionError(f"未连接到 rosbridge ({self.url})")
        self._ws.send(json.dumps({
            "op": "publish",
            "topic": topic,
            "msg": msg,
        }, ensure_ascii=False))

    # ──────────── 订阅 topic ────────────

    def subscribe(self, topic: str, msg_type: str, callback: Callable,
                  throttle_rate: int = 0, queue_length: int = 1):
        """
        订阅 ROS2 topic，收到消息时调用 callback(msg_dict)。

        Args:
            topic:         topic 名称
            msg_type:      消息类型，如 "std_msgs/String"
            callback:      回调函数 callback(msg: dict)
            throttle_rate: 节流（ms），0 = 不限
            queue_length:  队列长度
        """
        self._subscribers[topic] = callback
        sub_msg: dict = {
            "op": "subscribe",
            "topic": topic,
            "type": msg_type,
        }
        if throttle_rate > 0:
            sub_msg["throttle_rate"] = throttle_rate
        if queue_length > 0:
            sub_msg["queue_length"] = queue_length

        if self._connected.is_set():
            self._ws.send(json.dumps(sub_msg, ensure_ascii=False))

    def unsubscribe(self, topic: str):
        """取消订阅"""
        self._subscribers.pop(topic, None)
        if self._connected.is_set():
            self._ws.send(json.dumps({
                "op": "unsubscribe",
                "topic": topic,
            }))

    def subscribe_once(self, topic: str, msg_type: str,
                       timeout: float = 5.0) -> Optional[dict]:
        """
        订阅 topic，等待第一条消息后取消订阅并返回。

        Args:
            topic:     topic 名称
            msg_type:  消息类型，如 "nav_msgs/msg/Odometry"
            timeout:   等待秒数

        Returns:
            消息字典，超时则返回 None
        """
        if not self._connected.is_set():
            raise ConnectionError(f"未连接到 rosbridge ({self.url})")

        result: list = [None]
        event = threading.Event()

        def _on_msg(msg: dict):
            result[0] = msg
            event.set()

        self.subscribe(topic, msg_type, _on_msg, queue_length=1)
        try:
            event.wait(timeout=timeout)
            return result[0]
        finally:
            self.unsubscribe(topic)

    # ──────────── 内部: WebSocket 回调 ────────────

    def _on_open(self, ws):
        self._connected.set()
        print(f"[rosbridge] ✓ 已连接 {self.url}")
        # 重连后重新订阅
        for topic, cb in self._subscribers.items():
            ws.send(json.dumps({
                "op": "subscribe",
                "topic": topic,
            }))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        op = data.get("op")

        # 服务响应
        if op == "service_response":
            msg_id = data.get("id")
            if msg_id:
                with self._pending_lock:
                    if msg_id in self._pending:
                        self._pending[msg_id]["result"] = data
                        self._pending[msg_id]["event"].set()

        # topic 消息（订阅推送）
        elif op == "publish":
            topic = data.get("topic")
            cb = self._subscribers.get(topic)
            if cb:
                try:
                    cb(data.get("msg", {}))
                except Exception as e:
                    print(f"[rosbridge] 订阅回调异常 ({topic}): {e}")

    def _on_error(self, ws, error):
        print(f"[rosbridge] ✗ 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected.clear()
        print(f"[rosbridge] 连接关闭 (code={close_status_code})")

    # ──────────── 内部: 运行循环（含自动重连） ────────────

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(
                    ping_interval=10,
                    ping_timeout=5,
                )
            except Exception as e:
                print(f"[rosbridge] 运行异常: {e}")

            self._connected.clear()

            if self._stop.is_set():
                break

            print("[rosbridge] 3 秒后重连...")
            self._stop.wait(timeout=3.0)


# ═══════════════════ 模块级单例 ═══════════════════

_ROSBRIDGE_URL = os.environ.get("ROSBRIDGE_URL", "ws://192.168.1.21:9090")
_ROSBRIDGE_TIMEOUT = float(os.environ.get("ROSBRIDGE_TIMEOUT", "10"))

# 全局客户端实例（各工具模块直接引用）
rosbridge = RosbridgeClient(url=_ROSBRIDGE_URL)


def connect_rosbridge(url: str = None, timeout: float = 5.0):
    """启动 rosbridge 连接（app.py 启动时调用）"""
    global rosbridge
    if url and url != rosbridge.url:
        rosbridge = RosbridgeClient(url=url)
    rosbridge.connect(timeout=timeout)


def disconnect_rosbridge():
    """断开 rosbridge 连接"""
    rosbridge.disconnect()


def is_rosbridge_connected() -> bool:
    return rosbridge.connected


def get_rosbridge_timeout() -> float:
    return _ROSBRIDGE_TIMEOUT
