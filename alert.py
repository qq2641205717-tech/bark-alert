#!/usr/bin/env python3
"""
Bark 双地点在线预警
地点: 通化东昌区 + 梧州长洲区
推送规格: 标题 1 行 + 正文 3 行
地震分级:
  - 当地地震(距任一监控点<=100km) 或 强震感(M>=5 / 烈度>=5) -> critical 紧急
  - 附近小震(距任一监控点<=300km, M>=3)                    -> timeSensitive
天气:
  - 每30分钟: 温度突变>=5°C / 降水概率突变 <30->60%
  - 早上7点:  各地点当天天气
  - 傍晚18点: 各地点今夜到明早
"""
import os, sys, json, math, urllib.request, datetime

# ---------- 双地点 ----------
LOCATIONS = [
    {"name": "通化", "lat": 41.72, "lon": 125.94, "kw": ["通化"]},
    {"name": "梧州", "lat": 23.48, "lon": 111.28, "kw": ["梧州"]},
]
BARK_KEY   = os.environ.get("BARK_KEY", "")
STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

WMO = {
    0:"晴",1:"大部晴",2:"多云",3:"阴",45:"雾",48:"冻雾",
    51:"毛毛雨",53:"毛毛雨",55:"毛毛雨",56:"冻雨",57:"冻雨",
    61:"小雨",63:"中雨",65:"大雨",66:"冻雨",67:"冻雨",
    71:"小雪",73:"中雪",75:"大雪",77:"雪粒",
    80:"阵雨",81:"阵雨",82:"强阵雨",85:"阵雪",86:"阵雪",
    95:"雷雨",96:"雷雨冰雹",99:"强雷雨冰雹",
}
def wmo(c): return WMO.get(int(c), f"代码{c}")

# ---------- 工具 ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent":"bark-alert/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def bark(title, l1, l2, l3, level="active", sound=None, group="wx", url=None):
    if not BARK_KEY:
        print("[WARN] BARK_KEY 未设置"); return
    # critical 级别连续推10条, APNs间隔几秒送达, 形成连续响铃(像闹钟)
    times = 10 if level == "critical" else 1
    payload = {"title":title, "body":f"{l1}\n{l2}\n{l3}", "level":level, "group":group}
    if sound: payload["sound"] = sound
    if url:   payload["url"] = url
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for n in range(times):
        req = urllib.request.Request(
            f"https://api.day.app/{BARK_KEY}", data=data,
            headers={"Content-Type":"application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                print(f"[BARK] {title} ({n+1}/{times}) -> {json.loads(r.read())}")
        except Exception as e:
            print(f"[BARK ERROR] {e}")

def hav(la1, lo1, la2, lo2):
    R=6371.0
    p1,p2=math.radians(la1),math.radians(la2)
    dp=math.radians(la2-la1); dl=math.radians(lo2-lo1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.asin(math.sqrt(a))

def nearest_loc(elat, elon):
    best = None
    for loc in LOCATIONS:
        d = hav(loc["lat"], loc["lon"], elat, elon)
        if best is None or d < best[1]:
            best = (loc["name"], d)
    return best

# ---------- 天气 ----------
def fetch_wx(loc):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={loc['lat']}&longitude={loc['lon']}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability,weather_code"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%2FShanghai&forecast_days=3")
    return http_get_json(url)

def check_weather(s):
    wx = s.setdefault("wx", {})
    for loc in LOCATIONS:
        name = loc["name"]
        try:
            d = fetch_wx(loc); cur = d["current"]
            temp = cur["temperature_2m"]; code = int(cur.get("weather_code",0))
            wind = cur.get("wind_speed_10m","?")
            st = wx.setdefault(name, {"last_temp":None, "last_rain_prob":0})
            lt = st.get("last_temp")
            if lt is not None and abs(temp-lt) >= 5:
                diff = temp-lt; arrow = "升温" if diff>0 else "降温"
                bark(f"{name} 🌡️{arrow}{abs(diff):.1f}°C",
                     f"现在{temp}° {wmo(code)}",
                     f"上次{lt}°",
                     "注意增减衣物",
                     level="timeSensitive", group="wx")
            h = d["hourly"]; times=h["time"]; probs=h["precipitation_probability"]
            now = cur["time"]; idx = next((i for i,t in enumerate(times) if t>=now), 0)
            nxt = max((int(probs[j] or 0) for j in range(idx, min(idx+4, len(probs)))), default=0)
            if nxt >= 60 and st.get("last_rain_prob",0) < 30:
                bark(f"{name} 🌧️将降雨{nxt}%",
                     f"未来3小时降水{nxt}%",
                     f"当前{wmo(code)} {temp}°",
                     "出门请带伞",
                     level="timeSensitive", sound="rain", group="wx")
            st["last_temp"] = temp; st["last_rain_prob"] = nxt
            print(f"[WX] {name} {temp}° {wmo(code)} 近3h降水{nxt}%")
        except Exception as e:
            print(f"[WX {name} ERR] {e}")

def morning_forecast():
    for loc in LOCATIONS:
        name = loc["name"]
        try:
            d = fetch_wx(loc); daily = d["daily"]; cur = d["current"]
            today_code = daily["weather_code"][0]
            tmax = daily["temperature_2m_max"][0]
            tmin = daily["temperature_2m_min"][0]
            pmax = daily["precipitation_probability_max"][0]
            wind = cur.get("wind_speed_10m","?")
            bark(f"{name} 今日天气",
                 f"白天 {wmo(today_code)} {tmax}°",
                 f"夜间 {tmin}°",
                 f"降水{pmax}% 风{wind}km/h",
                 group="wx")
        except Exception as e:
            print(f"[MORNING {name} ERR] {e}")

def evening_forecast():
    for loc in LOCATIONS:
        name = loc["name"]
        try:
            d = fetch_wx(loc)
            h = d["hourly"]; times=h["time"]; temps=h["temperature_2m"]
            probs=h["precipitation_probability"]; codes=h["weather_code"]
            now = d["current"]["time"]; idx = next((i for i,t in enumerate(times) if t>=now), 0)
            seg = temps[idx:idx+15]
            night_low = min(seg) if seg else d["current"]["temperature_2m"]
            morning_idx = next((i for i in range(idx, len(times))
                                if times[i][11:13] in ("06","07") and i < idx+15), idx+12)
            morn_temp = temps[morning_idx] if morning_idx < len(temps) else temps[-1]
            night_code = codes[idx+6] if idx+6 < len(codes) else codes[idx]
            max_rain = max((int(probs[j] or 0) for j in range(idx, min(idx+15,len(probs)))), default=0)
            bark(f"{name} 今夜到明早",
                 f"夜间 {wmo(night_code)} 最低{night_low:.0f}°",
                 f"明早 {morn_temp:.0f}°",
                 f"降水{max_rain}%",
                 group="wx")
        except Exception as e:
            print(f"[EVENING {name} ERR] {e}")

# ---------- 中央气象台官方预警 ----------
def check_nmc_alerts(s):
    """按 地点|类型|颜色 去重, 同key无变化只推一次; 首次只建基线不轰炸; 解除后自动清除再发会重推"""
    try:
        url = "http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=80&signaltype=&signallevel=&province="
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
        alerts = d.get("data",{}).get("page",{}).get("list",[])

        current = {}
        for a in alerts:
            title = a.get("title","")
            matched = next((loc["name"] for loc in LOCATIONS if any(k in title for k in loc["kw"])), None)
            if not matched: continue
            color = next((c for c in ("红","橙","黄","蓝") if c+"色" in title), "蓝")
            wtype = next((kw for kw in ("雷电","大雾","暴雨","寒潮","大风","高温","台风","暴雪",
                          "霜冻","道路结冰","沙尘暴","雷雨大风","强对流","冰雹","干旱","霾","海上大风")
                          if kw in title), "天气")
            key = f"{matched}|{wtype}|{color}"
            station = title.split("发布")[0]
            current.setdefault(key, {"stations":[], "time":a.get("issuetime","")})["stations"].append(station)

        pushed = s.get("nmc_pushed", {})
        first_run = not pushed

        for key, info in current.items():
            if key in pushed or first_run:
                continue
            matched, wtype, color = key.split("|")
            level = "critical" if color in ("红","橙") else ("timeSensitive" if color=="黄" else "active")
            sound = "alarm" if color in ("红","橙") else None
            ns = len(info["stations"])
            station_txt = info["stations"][0][:16] + (f"等{ns}地" if ns>1 else "")
            bark(f"⚠️{matched} {color}色{wtype}预警",
                 f"{station_txt}",
                 f"发布 {info['time'][5:16].replace('/','-')}",
                 "中央气象台权威发布",
                 level=level, sound=sound, group="alert",
                 url="http://www.nmc.cn/publish/alarm.html")
            print(f"[NMC] {key} 新预警 ({ns}台站)")

        s["nmc_pushed"] = {k: True for k in current}
    except Exception as e:
        print(f"[NMC ERR] {e}")

# ---------- 地震 ----------
def _eq_level(mag, intensity, dist):
    if dist <= 100:
        return "critical", "alarm", "当地地震"
    if mag >= 5.0 or intensity >= 5.0:
        return "critical", "alarm", "强震感"
    if dist <= 300 and mag >= 3.0:
        return "timeSensitive", "update", "附近地震"
    return None

def check_earthquake(s):
    try:
        e = http_get_json("https://api.wolfx.jp/cenc_eew.json")
        eid = e.get("EventID",""); last = s.get("last_eew_id","")
        if eid and eid != last:
            s["last_eew_id"] = eid
            if last:
                mag=float(e.get("Magnitude") or 0)
                elat=float(e.get("Latitude") or 0); elon=float(e.get("Longitude") or 0)
                name, dist = nearest_loc(elat, elon)
                intensity = float(e.get("MaxIntensity") or 0)
                lvl = _eq_level(mag, intensity, dist)
                if lvl:
                    level, sound, tag = lvl
                    bark(f"🚨{tag} M{mag}",
                         f"距{name} {dist:.0f}km",
                         f"深{e.get('Depth','?')}km 烈度{intensity}",
                         f"{e.get('HypoCenter','?')} {e.get('OriginTime','')[11:16]}",
                         level=level, sound=sound, group="eq",
                         url="https://news.ceic.ac.cn/")
    except Exception as e: print(f"[EQ EEW ERR] {e}")

    try:
        eq = http_get_json("https://api.wolfx.jp/cenc_eqlist.json")
        latest = eq.get("No1",{}); eid = latest.get("EventID","")
        last = s.get("last_eq_id","")
        if eid and eid != last:
            s["last_eq_id"] = eid
            if last:
                mag=float(latest.get("magnitude") or 0)
                elat=float(latest.get("latitude") or 0); elon=float(latest.get("longitude") or 0)
                name, dist = nearest_loc(elat, elon)
                intensity = float(latest.get("intensity") or 0)
                lvl = _eq_level(mag, intensity, dist)
                if lvl:
                    level, sound, tag = lvl
                    bark(f"📢{tag} M{mag}",
                         f"距{name} {dist:.0f}km",
                         f"深{latest.get('depth','?')}km",
                         f"{latest.get('location','?')} {latest.get('time','')[11:16]}",
                         level=level, sound=sound, group="eq",
                         url="https://news.ceic.ac.cn/")
    except Exception as e: print(f"[EQ LIST ERR] {e}")

    try:
        j = http_get_json("https://api.wolfx.jp/jma_eqlist.json")
        latest = j.get("No1",{}); info=(latest.get("info") or "").strip()
        last = s.get("last_tsunami_info","")
        is_warn = bool(info and ("大津波警報" in info or "津波警報" in info
                                or "津波注意報" in info or "津波予報" in info))
        if is_warn and info != last:
            s["last_tsunami_info"] = info
            bark("🌊海啸预警",
                 "日本气象厅发布",
                 f"{latest.get('location','?')}",
                 f"M{latest.get('magnitude','?')} 震度{latest.get('shindo','?')}",
                 level="critical", sound="alarm", group="tsunami",
                 url="https://www.jma.go.jp/jma/index.html")
        elif info:
            s["last_tsunami_info"] = info
    except Exception as e: print(f"[TSUNAMI ERR] {e}")

# ---------- 入口 ----------
if __name__=="__main__":
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    s=load_state()
    if mode in ("earthquake","all"): check_earthquake(s)
    if mode in ("weather","all"):
        check_weather(s)
        check_nmc_alerts(s)
    if mode=="morning":  morning_forecast()
    if mode=="evening":   evening_forecast()
    save_state(s)
    print(f"[OK] {mode} @ {datetime.datetime.now()}")
