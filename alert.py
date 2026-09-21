#!/usr/bin/env python3
"""
Bark 在线预警 — 天气 / 天气变化 / 天气预报 / 地震预警 / 地震速报 / 津波情报
数据源: Open-Meteo (免费无key) + Wolfx (免费无key)
推送:   Bark HTTP API
状态:   state.json (由 GitHub Actions 自动 commit 回仓库持久化)
"""
import os, sys, json, time, math, urllib.request, urllib.parse, datetime

# ---------- 配置 ----------
BARK_KEY   = os.environ.get("BARK_KEY", "")
LAT        = float(os.environ.get("LAT", "41.72"))
LON        = float(os.environ.get("LON", "125.94"))
LOCATION   = os.environ.get("LOCATION_NAME", "通化")
STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

# WMO weather code -> 中文
WMO = {
    0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}

def wmo_desc(code):
    return WMO.get(int(code), f"天气代码{code}")

# ---------- 工具 ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "bark-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def bark(title, body, level="active", sound=None, group="default", url=None):
    if not BARK_KEY:
        print("[WARN] BARK_KEY 未设置, 跳过推送")
        return
    payload = {"title": title, "body": body, "level": level, "group": group}
    if sound:
        payload["sound"] = sound
    if url:
        payload["url"] = url
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.day.app/{BARK_KEY}",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "bark-alert/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            resp = json.loads(r.read().decode("utf-8"))
            print(f"[BARK] {title} -> {resp}")
    except Exception as e:
        print(f"[BARK ERROR] {e}")

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

# ---------- 地震 / 津波 ----------
def check_earthquake(state):
    # 1) CENC 地震预警 (EEW, 秒级)
    try:
        eew = http_get_json("https://api.wolfx.jp/cenc_eew.json")
        eid = eew.get("EventID", "")
        last = state.get("last_eew_id", "")
        if eid and eid != last:
            state["last_eew_id"] = eid
            if last:  # 首次只建基线不推
                mag = float(eew.get("Magnitude") or 0)
                depth = eew.get("Depth", "?")
                hypo = eew.get("HypoCenter", "未知")
                elat = float(eew.get("Latitude") or 0)
                elon = float(eew.get("Longitude") or 0)
                dist = haversine(LAT, LON, elat, elon) if elat and elon else 9999
                maxi = eew.get("MaxIntensity", "?")
                # 阈值: 距本地 300km 内 M4.0+, 或全国 M6.0+
                if (dist <= 300 and mag >= 4.0) or mag >= 6.0:
                    bark(
                        f"🚨 地震预警 M{mag}",
                        f"震中：{hypo}\n深度：{depth}km\n距{LOCATION}约 {dist:.0f} km\n"
                        f"最大烈度：{maxi}\n发震：{eew.get('OriginTime','')}",
                        level="critical", sound="alarm", group="earthquake",
                        url="https://news.ceic.ac.cn/",
                    )
                else:
                    print(f"[EQ] EEW M{mag} @{hypo} {dist:.0f}km, 低于阈值不推")
    except Exception as e:
        print(f"[EQ EEW ERROR] {e}")

    # 2) CENC 正式速报
    try:
        eq = http_get_json("https://api.wolfx.jp/cenc_eqlist.json")
        latest = eq.get("No1", {})
        eid = latest.get("EventID", "")
        last = state.get("last_eq_id", "")
        if eid and eid != last:
            state["last_eq_id"] = eid
            if last:
                mag = float(latest.get("magnitude") or 0)
                loc = latest.get("location", "未知")
                depth = latest.get("depth", "?")
                elat = float(latest.get("latitude") or 0)
                elon = float(latest.get("longitude") or 0)
                dist = haversine(LAT, LON, elat, elon) if elat and elon else 9999
                # 阈值: 距本地 200km 内 M3.0+, 或全国 M5.0+
                if (dist <= 200 and mag >= 3.0) or mag >= 5.0:
                    bark(
                        f"📢 地震速报 M{mag}",
                        f"震中：{loc}\n深度：{depth}km\n距{LOCATION}约 {dist:.0f} km\n"
                        f"时间：{latest.get('time','')}",
                        level="timeSensitive", sound="update", group="earthquake",
                        url="https://news.ceic.ac.cn/",
                    )
                else:
                    print(f"[EQ] 速报 M{mag} @{loc} {dist:.0f}km, 低于阈值")
    except Exception as e:
        print(f"[EQ LIST ERROR] {e}")

    # 3) 日本气象厅 津波情报 (海啸预警)
    try:
        jma = http_get_json("https://api.wolfx.jp/jma_eqlist.json")
        latest = jma.get("No1", {})
        info = (latest.get("info") or "").strip()
        last = state.get("last_tsunami_info", "")
        # 只有真正发布海啸警报/注意报才推; "津波の心配はありません"=无海啸担忧, 不推
        is_warning = bool(
            info and (
                "大津波警報" in info or "津波警報" in info
                or "津波注意報" in info or "津波予報" in info
            )
        )
        if is_warning and info != last:
            state["last_tsunami_info"] = info
            bark(
                "🌊 海啸/津波情报",
                f"日本气象厅发布：{info}\n震中：{latest.get('location','')}\n"
                f"M{latest.get('magnitude','')} 最大震度{latest.get('shindo','')}",
                level="critical", sound="alarm", group="tsunami",
                url="https://www.jma.go.jp/jma/index.html",
            )
        elif info:
            state["last_tsunami_info"] = info
    except Exception as e:
        print(f"[TSUNAMI ERROR] {e}")

# ---------- 天气 ----------
def fetch_weather():
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,precipitation_probability_max"
        "&timezone=Asia%2FShanghai&forecast_days=3"
    )
    return http_get_json(url)

def check_weather(state):
    try:
        data = fetch_weather()
        cur = data.get("current", {})
        temp = cur.get("temperature_2m")
        code = int(cur.get("weather_code", 0))
        hum = cur.get("relative_humidity_2m", "?")
        wind = cur.get("wind_speed_10m", "?")

        # 温度突变 (>=5°C)
        last_temp = state.get("last_temp")
        if last_temp is not None and temp is not None:
            diff = temp - last_temp
            if abs(diff) >= 5:
                direction = "升温" if diff > 0 else "降温"
                bark(
                    f"🌡️ {direction} {abs(diff):.1f}°C",
                    f"{LOCATION} 当前 {temp}°C（上次 {last_temp}°C）\n"
                    f"{wmo_desc(code)}，湿度 {hum}%，风速 {wind} km/h",
                    level="timeSensitive", group="weather",
                )

        # 降水概率突变: 未来 3h 降水概率 >60% 且上次 <30%
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        probs = hourly.get("precipitation_probability", [])
        # 找当前时间索引
        now_str = cur.get("time", "")
        idx = 0
        for i, t in enumerate(times):
            if t >= now_str:
                idx = i
                break
        near_prob = 0
        for j in range(idx, min(idx + 4, len(probs))):
            near_prob = max(near_prob, int(probs[j] or 0))
        last_rain = state.get("last_rain_prob", 0)
        if near_prob >= 60 and last_rain < 30:
            bark(
                f"🌧️ 即将降雨 概率{near_prob}%",
                f"{LOCATION} 未来几小时降水概率 {near_prob}%\n"
                f"当前 {wmo_desc(code)} {temp}°C\n出门请带伞",
                level="timeSensitive", sound="rain", group="weather",
            )

        state["last_temp"] = temp
        state["last_rain_prob"] = near_prob
        state["last_weather_code"] = code
        state["last_check"] = datetime.datetime.now().isoformat()
        print(f"[WEATHER] {LOCATION} {temp}°C {wmo_desc(code)} 近3h降水{near_prob}%")
    except Exception as e:
        print(f"[WEATHER ERROR] {e}")

def daily_forecast():
    try:
        data = fetch_weather()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        pmax = daily.get("precipitation_probability_max", [])
        cur = data.get("current", {})
        lines = []
        for i in range(min(3, len(dates))):
            label = "今天" if i == 0 else ("明天" if i == 1 else "后天")
            lines.append(
                f"{label}（{dates[i][5:]}）：{wmo_desc(codes[i])}  "
                f"{tmin[i]}°~{tmax[i]}°  降水{pmax[i]}%"
            )
        body = "\n".join(lines)
        body += f"\n\n当前 {cur.get('temperature_2m','?')}°C {wmo_desc(cur.get('weather_code',0))}"
        bark(f"📅 {LOCATION}未来3天预报", body, group="weather")
    except Exception as e:
        print(f"[DAILY ERROR] {e}")

# ---------- 入口 ----------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    state = load_state()
    if mode in ("earthquake", "all"):
        check_earthquake(state)
    if mode in ("weather", "all"):
        check_weather(state)
    if mode == "daily":
        daily_forecast()
    save_state(state)
    print(f"[OK] mode={mode} done at {datetime.datetime.now()}")
