"""AI智慧农业 —— 工具模块
传感器模拟（含时间趋势 + 区域偏移）、浇水控制、日志读写、时间查询
新增工具只需：① 写函数  ② 加 TOOL_DEFS  ③ 加 _FN
"""

import sqlite3, json, math, random, threading, time
from datetime import datetime, timedelta
from typing import Union, Optional

# ═══════════════════ 工具显示名 ═══════════════════

TOOL_DISPLAY_NAMES = {
    "get_sensor_data":    "📡 查询传感器数据",
    "get_zone_overview":  "📋 获取区域概览",
    "get_sensor_history": "📈 查询历史趋势",
    "water_zone":         "💧 执行浇水操作",
    "read_log":           "📖 读取操作日志",
    "write_log":          "📝 写入操作日志",
    "get_current_time":   "🕐 获取当前时间",
}

# ═══════════════════ 常量 ═══════════════════

DB = "farm.db"
ZONES = ["东北", "西北", "东南", "西南"]
SENSORS = ["temperature", "humidity", "co2", "light"]
UNITS = {"temperature": "°C", "humidity": "%", "co2": "ppm", "light": "lux"}
NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照强度"}

# ═══════════════════ 数据库 ═══════════════════

def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INTEGER PRIMARY KEY, ts TEXT, zone TEXT, type TEXT, val REAL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY, ts TEXT, op TEXT, detail TEXT, who TEXT DEFAULT 'AI'
            );
        """)

# ═══════════════════ 传感器模拟 ═══════════════════

def _sim(zone: str, s: str, t: Optional[datetime] = None) -> float:
    t = t or datetime.now()
    h = t.hour + t.minute / 60
    z = {"东北": -2, "西北": -1, "东南": 1, "西南": 2}[zone]

    if s == "temperature":
        base = 25 + 8 * math.sin((h - 8) * math.pi / 12)
        return round(base + z * 0.5 + random.gauss(0, 0.8), 1)

    if s == "humidity":
        base = 65 - 15 * math.sin((h - 8) * math.pi / 12)
        return round(max(0, min(100, base - z * 1.5 + random.gauss(0, 1.5))), 1)

    if s == "co2":
        base = 400 + 50 * math.cos((h - 2) * math.pi / 12)
        return round(base + z * 5 + random.gauss(0, 8), 1)

    raw = max(0, 50000 * math.sin((h - 6) * math.pi / 12)) if 6 <= h <= 18 else 0
    return round(max(0, raw + z * 1000 + random.gauss(0, 1500)))

# ═══════════════════ 后台数据采集线程 ═══════════════════

_collector_started = False
_collector_lock = threading.Lock()
_collector_stop_event = threading.Event()


def _collector_loop(interval: float):
    """后台线程主循环：每隔 interval 秒向数据库写入所有区域所有传感器的模拟数据"""
    while not _collector_stop_event.is_set():
        now = datetime.now()
        ts = now.isoformat()
        rows = []
        for zone in ZONES:
            for sensor in SENSORS:
                val = _sim(zone, sensor, now)
                rows.append((ts, zone, sensor, val))

        try:
            with sqlite3.connect(DB) as c:
                c.executemany(
                    "INSERT INTO sensor_data VALUES(NULL,?,?,?,?)", rows
                )
        except Exception as e:
            # 静默处理，避免后台线程崩溃
            print(f"[数据采集] 写入失败: {e}")

        # 可中断的等待，比 time.sleep 更优雅
        _collector_stop_event.wait(timeout=interval)


def start_sensor_collector(interval: float = 30.0):
    """
    启动后台传感器数据采集线程。

    参数:
        interval: 采集间隔（秒），默认 30 秒。
                  每次采集会为 4 个区域 × 4 个传感器 = 16 条记录写入数据库。

    说明:
        - 多次调用是安全的，只会启动一个线程
        - 线程设为 daemon，主进程退出时自动结束
        - 也可调用 stop_sensor_collector() 手动停止
    """
    global _collector_started

    with _collector_lock:
        if _collector_started:
            print("[数据采集] 采集线程已在运行，跳过重复启动")
            return

        _collector_stop_event.clear()

        t = threading.Thread(
            target=_collector_loop,
            args=(interval,),
            name="SensorCollector",
            daemon=True,  # 主进程退出时自动终止
        )
        t.start()
        _collector_started = True
        print(f"[数据采集] 后台线程已启动，采集间隔 {interval} 秒，"
              f"每次写入 {len(ZONES) * len(SENSORS)} 条记录")


def stop_sensor_collector():
    """停止后台传感器数据采集线程"""
    global _collector_started

    with _collector_lock:
        if not _collector_started:
            print("[数据采集] 采集线程未运行")
            return

        _collector_stop_event.set()
        _collector_started = False
        print("[数据采集] 已发送停止信号")


def is_collector_running() -> bool:
    """检查采集线程是否正在运行"""
    return _collector_started


# ═══════════════════ 工具实现 ═══════════════════

def get_current_sensor_data(zone: str, sensor_type: str) -> dict:
    """获取实时读数并存入数据库"""
    now = datetime.now()
    v = _sim(zone, sensor_type, now)
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO sensor_data VALUES(NULL,?,?,?,?)",
                  (now.isoformat(), zone, sensor_type, v))
    return {"zone": zone, "sensor": NAMES[sensor_type],
            "value": v, "unit": UNITS[sensor_type],
            "time": now.strftime("%Y-%m-%d %H:%M:%S")}


def get_historical_sensor_data(zone: str, sensor_type: str, hours: float) -> dict:
    """
    获取过去 N 小时的历史数据。
    优先从数据库读取真实采集数据，若数据不足则用模拟数据补充。
    """
    now = datetime.now()
    start = now - timedelta(hours=hours)

    # 先尝试从数据库读取
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ts, val FROM sensor_data "
            "WHERE zone=? AND type=? AND ts>=? ORDER BY ts",
            (zone, sensor_type, start.isoformat())
        ).fetchall()

    if len(rows) >= 5:
        # 数据库有足够数据，直接使用
        data = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r[0])
                data.append({"time": t.strftime("%m-%d %H:%M"), "value": r[1]})
            except Exception:
                continue
    else:
        # 数据不足，用模拟数据
        interval = 30 if hours <= 24 else (60 if hours <= 168 else 180)
        data = []
        for i in range(int(hours * 60 / interval), 0, -1):
            ts = now - timedelta(minutes=i * interval)
            data.append({"time": ts.strftime("%m-%d %H:%M"),
                          "value": _sim(zone, sensor_type, ts)})

    vals = [d["value"] for d in data]
    return {"zone": zone, "sensor": NAMES[sensor_type], "unit": UNITS[sensor_type],
            "period": f"过去{hours}小时", "count": len(data),
            "min": min(vals), "max": max(vals),
            "avg": round(sum(vals) / len(vals), 1),
            "data_source": "数据库" if len(rows) >= 5 else "模拟",
            "data": data}


def get_zone_overview(zone: str) -> dict:
    """一次性获取某区域所有传感器当前读数"""
    now = datetime.now()
    readings = {}
    with sqlite3.connect(DB) as c:
        for s in SENSORS:
            v = _sim(zone, s, now)
            c.execute("INSERT INTO sensor_data VALUES(NULL,?,?,?,?)",
                      (now.isoformat(), zone, s, v))
            readings[NAMES[s]] = {"value": v, "unit": UNITS[s]}
    return {"zone": zone,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "readings": readings}


def water_zone(zone: str, amount_liters: float) -> dict:
    """执行浇水并自动写入日志"""
    now = datetime.now()
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO logs VALUES(NULL,?,?,?,?)",
                  (now.isoformat(), "浇水",
                   f"{zone}区域 浇水 {amount_liters}L", "AI"))
    return {"status": "success",
            "message": f"已向{zone}区域浇水 {amount_liters} 升",
            "time": now.strftime("%Y-%m-%d %H:%M:%S")}


def write_log(operation_type: str, details: str) -> dict:
    """手动写入一条日志"""
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO logs VALUES(NULL,?,?,?,?)",
                  (datetime.now().isoformat(), operation_type, details, "AI"))
    return {"status": "success", "message": "日志已写入"}


def read_logs(limit: int = 10, operation_type: Optional[str] = None) -> dict:
    """查询最近的日志，可按类型筛选"""
    with sqlite3.connect(DB) as c:
        if operation_type:
            rows = c.execute(
                "SELECT ts,op,detail,who FROM logs "
                "WHERE op=? ORDER BY id DESC LIMIT ?",
                (operation_type, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT ts,op,detail,who FROM logs "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"count": len(rows),
            "logs": [{"time": r[0], "type": r[1],
                      "detail": r[2], "operator": r[3]} for r in rows]}


def get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    """获取当前日期和时间，包含星期、季节、农事建议等"""
    now = datetime.now()

    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[now.weekday()]

    hour = now.hour
    if 5 <= hour < 7:
        period, farm_hint = "清晨", "适合巡田、查看露水情况"
    elif 7 <= hour < 9:
        period, farm_hint = "早晨", "适合施肥、喷药（风小、蒸发少）"
    elif 9 <= hour < 11:
        period, farm_hint = "上午", "光照渐强，注意观察作物状态"
    elif 11 <= hour < 13:
        period, farm_hint = "中午", "高温时段，避免浇水和喷药"
    elif 13 <= hour < 15:
        period, farm_hint = "下午早段", "温度最高，注意遮阳和通风"
    elif 15 <= hour < 17:
        period, farm_hint = "下午", "温度回落，可恢复田间作业"
    elif 17 <= hour < 19:
        period, farm_hint = "傍晚", "适合浇水（蒸发少、夜间吸收好）"
    elif 19 <= hour < 21:
        period, farm_hint = "晚间", "检查灌溉设备和夜间防护"
    else:
        period, farm_hint = "夜间", "作物休息期，注意低温防护"

    month = now.month
    if month in [3, 4, 5]:
        season, season_hint = "春季", "春耕播种期，注意倒春寒"
    elif month in [6, 7, 8]:
        season, season_hint = "夏季", "生长旺季，注意防暑、防涝、病虫害"
    elif month in [9, 10, 11]:
        season, season_hint = "秋季", "收获季节，注意适时采收"
    else:
        season, season_hint = "冬季", "休耕/大棚管理期，注意防冻保温"

    return {
        "date": now.strftime("%Y年%m月%d日"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekday,
        "period": period,
        "season": season,
        "datetime_full": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(now.timestamp()),
        "farm_hint": farm_hint,
        "season_hint": season_hint,
        "timezone": timezone,
    }


# ═══════════════════ OpenAI Tool 定义 ═══════════════════

def _t(name, desc, props, req=None):
    p = {"type": "object", "properties": props}
    if req:
        p["required"] = req
    return {"type": "function",
            "function": {"name": name, "description": desc, "parameters": p}}

_Z = {"type": "string", "enum": ZONES, "description": "区域：东北/西北/东南/西南"}
_S = {"type": "string", "enum": SENSORS,
      "description": "传感器类型：temperature/humidity/co2/light"}

TOOL_DEFS = [
    _t("get_current_sensor_data",
       "获取指定区域某个传感器的实时数据",
       {"zone": _Z, "sensor_type": _S},
       ["zone", "sensor_type"]),

    _t("get_historical_sensor_data",
       "获取指定区域某个传感器过去 N 小时的历史数据（含最小/最大/平均值）",
       {"zone": _Z, "sensor_type": _S,
        "hours": {"type": "number", "description": "过去多少小时"}},
       ["zone", "sensor_type", "hours"]),

    _t("get_zone_overview",
       "一次性获取指定区域全部传感器（温度/湿度/CO₂/光照）的当前读数",
       {"zone": _Z},
       ["zone"]),

    _t("water_zone",
       "对指定区域进行浇水，需指定水量（升）",
       {"zone": _Z,
        "amount_liters": {"type": "number", "description": "浇水量（升）"}},
       ["zone", "amount_liters"]),

    _t("write_log",
       "向系统日志写入一条操作记录",
       {"operation_type": {"type": "string", "description": "操作类型，如：施肥、巡检、告警"},
        "details":        {"type": "string", "description": "操作详情"}},
       ["operation_type", "details"]),

    _t("read_logs",
       "查询系统操作日志，可按类型筛选",
       {"limit":          {"type": "integer", "description": "返回条数，默认 10"},
        "operation_type": {"type": "string",  "description": "按操作类型筛选（可选）"}}),

    _t("get_current_time",
       "获取当前日期、时间、星期、季节，以及对应的农事建议提示。当用户询问现在几点、今天几号、什么季节等时间相关问题时使用。",
       {"timezone": {"type": "string",
                     "description": "时区名称，默认 Asia/Shanghai",
                     "default": "Asia/Shanghai"}}),
]

# ═══════════════════ 统一调度 ═══════════════════

_FN = {f.__name__: f for f in [
    get_current_sensor_data, get_historical_sensor_data,
    get_zone_overview, water_zone, write_log, read_logs,
    get_current_time,
]}

def call_tool(name: str, args: dict) -> str:
    fn = _FN.get(name)
    if not fn:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        return json.dumps(fn(**args), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)