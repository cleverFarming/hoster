#!/usr/bin/env python3
"""
rosbridge 连通性测试脚本
用法: python3 test_rosbridge.py [ws://localhost:9090]

功能:
  1. 连接 rosbridge WebSocket
  2. 简要列出 topics 和 services
  3. 自动发现位置/传感器候选
  4. 订阅所有 topics，持续输出收到的消息（一行一条）
"""

import signal
import websocket
import json
import sys
import threading
import time
import uuid
from datetime import datetime

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://192.168.1.11:9090"

# ═══════════════════ 通信基础设施 ═══════════════════

_results = {}
_events = {}
_topic_msgs = {}
_connected = threading.Event()
_run = True


def _compact_msg(msg, max_len=150):
    """将消息压缩为单行，超长截断"""
    if isinstance(msg, dict):
        # 大数组/二进制字段仅显示长度
        out = {}
        for k, v in list(msg.items())[:8]:
            if isinstance(v, (list, str)) and len(v) > 50:
                out[k] = f"<len={len(v)}>"
            else:
                out[k] = v
        msg = out
    s = json.dumps(msg, ensure_ascii=False)
    return (s[:max_len] + "...") if len(s) > max_len else s


def _on_open(ws):
    _connected.set()
    print(f"✅ rosbridge 连接成功: {URL}\n")


def _on_message(ws, message):
    data = json.loads(message)
    op = data.get("op")
    msg_id = data.get("id")

    if op == "service_response" and msg_id and msg_id in _events:
        _results[msg_id] = data
        _events[msg_id].set()
    elif op == "publish":
        topic = data.get("topic", "")
        _topic_msgs[topic] = data.get("msg", {})
        ts = datetime.now().strftime("%H:%M:%S")
        compact = _compact_msg(data.get("msg", {}))
        print(f"[{ts}] {topic} | {compact}")


def _on_error(ws, error):
    print(f"❌ 连接错误: {error}")


def _on_close(ws, code, msg):
    _connected.clear()
    print(f"连接关闭 (code={code})")


ws = websocket.WebSocketApp(
    URL,
    on_open=_on_open,
    on_message=_on_message,
    on_error=_on_error,
    on_close=_on_close,
)
threading.Thread(target=ws.run_forever, daemon=True).start()

if not _connected.wait(timeout=5):
    print(f"❌ 无法连接到 rosbridge: {URL}")
    print("   请确认:")
    print("   1) rosbridge_server 正在运行")
    print("   2) 端口 9090 没有被防火墙阻止")
    print("   3) URL 地址正确")
    sys.exit(1)


def call_service(service, args=None, timeout=5):
    """同步调用 rosbridge 服务"""
    msg_id = str(uuid.uuid4())
    ev = threading.Event()
    _events[msg_id] = ev
    msg = {"op": "call_service", "id": msg_id, "service": service}
    if args:
        msg["args"] = args
    ws.send(json.dumps(msg, ensure_ascii=False))
    if ev.wait(timeout=timeout):
        return _results.pop(msg_id, None)
    return None


def subscribe_and_wait(topic, msg_type, wait_sec=3):
    """订阅 topic 并等待一条消息"""
    ws.send(json.dumps({
        "op": "subscribe",
        "topic": topic,
        "type": msg_type,
        "queue_length": 1,
    }))
    time.sleep(wait_sec)
    ws.send(json.dumps({"op": "unsubscribe", "topic": topic}))
    return _topic_msgs.get(topic)


# ═══════════════════ 1. 列出 Topics ═══════════════════

print("=" * 60)
print("📡 ROS2 Topics")
print("=" * 60)

all_topics = []
all_types = []

resp = call_service("/rosapi/topics")
if resp and resp.get("values"):
    all_topics = resp["values"].get("topics", [])
    all_types = resp["values"].get("types", [])
    for topic, tp in sorted(zip(all_topics, all_types)):
        print(f"  {topic:<45} [{tp}]")
    print(f"\n  共 {len(all_topics)} 个 topics")
else:
    print("  ⚠ 无法获取 topics")
    print("  尝试: ros2 launch rosbridge_server rosbridge_websocket_launch.xml")
    print("  确保同时启动了 rosapi 节点")

# ═══════════════════ 2. 列出 Services ═══════════════════

print("\n" + "=" * 60)
print("🔧 ROS2 Services")
print("=" * 60)

all_services = []

resp = call_service("/rosapi/services")
if resp and resp.get("values"):
    all_services = resp["values"].get("services", [])
    for svc in sorted(all_services):
        print(f"  {svc}")
    print(f"\n  共 {len(all_services)} 个 services")
else:
    print("  ⚠ 无法获取 services")

# ═══════════════════ 3. 自动发现位置与传感器 ═══════════════════

print("\n" + "=" * 60)
print("🔍 自动发现位置与传感器 Topics")
print("=" * 60)

topic_to_type = dict(zip(all_topics, all_types)) if all_topics and all_types else {}

POSITION_KEYWORDS = ["pose", "odom", "tf", "position", "localization", "loc", "amcl"]
SENSOR_KEYWORDS = ["scan", "imu", "camera", "image", "laser", "battery", "sensor", "depth", "lidar"]

position_candidates = [(t, topic_to_type.get(t, "?"))
                       for t in all_topics
                       if any(kw in t.lower() for kw in POSITION_KEYWORDS)]
sensor_candidates = [(t, topic_to_type.get(t, "?"))
                     for t in all_topics
                     if any(kw in t.lower() for kw in SENSOR_KEYWORDS)]

print("  位置相关候选:")
if position_candidates:
    for topic, tp in sorted(position_candidates):
        print(f"    {topic:<40} [{tp}]")
else:
    print("    (无)")

print("\n  传感器相关候选:")
if sensor_candidates:
    for topic, tp in sorted(sensor_candidates):
        print(f"    {topic:<40} [{tp}]")
else:
    print("    (无)")

# ═══════════════════ 4. 订阅所有 topics，持续监听 ═══════════════════

print("\n" + "=" * 60)
print("📡 订阅所有 topics，持续输出（Ctrl+C 退出）")
print("=" * 60)

topic_to_type = dict(zip(all_topics, all_types)) if all_topics and all_types else {}
for topic in sorted(all_topics):
    msg_type = topic_to_type.get(topic, "std_msgs/msg/String")
    ws.send(json.dumps({
        "op": "subscribe",
        "topic": topic,
        "type": msg_type,
        "queue_length": 1,
    }))

def _stop(sig, frame):
    global _run
    _run = False

signal.signal(signal.SIGINT, _stop)
print(f"  已订阅 {len(all_topics)} 个 topics，等待消息...\n")

while _run and _connected.is_set():
    time.sleep(0.1)

ws.close()
