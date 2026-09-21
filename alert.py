#!/usr/bin/env python3
"""
Bark 在线预警 — 天气 / 天气变化 / 天气预报 / 地震预警 / 地震速报 / 津波情报
iPhone 通知规格: 标题 1 行 + 正文恰好 3 行 (横幅预览不折叠)
数据源: Open-Meteo (免费无key) + Wolfx (免费无key)
"""
import os, sys, json, math, urllib.request, datetime

BARK_KEY   = os.environ.get("BARK_KEY", "")
LAT        = float(os.environ.get("LAT", "41.72"))
LON        = float(os.environ.get("LON", "125.94"))
LOCATION   = os.environ.get("LOCATION_NAME", "通化")
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

def bark(title, line1, line2, line3, level="active", sound=None, group="weather", url=None):
    """严格 iPhone 3 行通知: title + 3 行 body"""
    if not BARK_KEY:
        print("[WARN] BARK_KEY 未设置"); return
    body = f"{line1}\n{line2}\n{line3}"
    payload = {"title":title, "body":body, "level":level, "group":group}
    if sound: payload["sound"] = sound
    if url:   payload["url"] = url
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.day.app/{BARK_KEY}", data=data,
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            print(f"[BARK] {title} -> {json.loads(r.read())}")
    except Exception as e:
        print(f"[BARK ERROR] {e}")

def hav(la1, lo1, la2, lo2):
    R=6371.0
    p1,p2=math.radians(la1),math.radians(la2)
    dp=math.radians(la2-la1); dl=math.radians(lo2-lo1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.asin(math.sqrt(a))

# ---------- 地震 / 津波 ----------
def check_earthquake(s):
    try:
        e = http_get_json("https://api.wolfx.jp/cenc_eew.json")
        eid = e.get("EventID",""); last = s.get("last_eew_id","")
        if eid and eid != last:
            s["last_eew_id"] = eid
            if last:
                mag=float(e.get("Magnitude") or 0); hypo=e.get("HypoCenter","?")
                elat=float(e.get("Latitude") or 0); elon=float(e.get("Longitude") or 0)
                dist=hav(LAT,LON,elat,elon) if elat and elon else 9999
                if (dist<=300 and mag>=4.0) or mag>=6.0:
                    bark(f"🚨地震预警 M{mag}",
                         f"{hypo} 距你{dist:.0f}km",
                         f"深{e.get('Depth','?')}km 烈度{e.get('MaxIntensity','?')}",
                         f"{e.get('OriginTime','')[11:16]}发震·详情",
                         level="critical", sound="alarm", group="eq",
                         url="https://news.ceic.ac.cn/")
                else:
                    print(f"[EQ] EEW M{mag} {hypo} {dist:.0f}km 低于阈值")
    except Exception as e: print(f"[EQ EEW ERR] {e}")

    try:
        eq = http_get_json("https://api.wolfx.jp/cenc_eqlist.json")
        latest = eq.get("No1",{}); eid = latest.get("EventID","")
        last = s.get("last_eq_id","")
        if eid and eid != last:
            s["last_eq_id"] = eid
            if last:
                mag=float(latest.get("magnitude") or 0)
                loc=latest.get("location","?")
                elat=float(latest.get("latitude") or 0); elon=float(latest.get("longitude") or 0)
                dist=hav(LAT,LON,elat,elon) if elat and elon else 9999
                if (dist<=200 and mag>=3.0) or mag>=5.0:
                    bark(f"📢地震速报 M{mag}",
                         f"{loc} 距你{dist:.0f}km",
                         f"深{latest.get('depth','?')}km",
                         f"{latest.get('time','')[11:16]}发震·详情",
                         level="timeSensitive", sound="update", group="eq",
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

# ---------- 天气 ----------
def fetch_weather():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability,weather_code"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%2FShanghai&forecast_days=3")
    return http_get_json(url)

def check_weather(s):
    try:
        d = fetch_weather(); cur=d["current"]
        temp=cur.get("temperature_2m"); code=int(cur.get("weather_code",0))
        hum=cur.get("relative_humidity_2m","?"); wind=cur.get("wind_speed_10m","?")
        lt=s.get("last_temp")
        if lt is not None and abs(temp-lt)>=5:
            d2=temp-lt; arrow="升温" if d2>0 else "降温"
            bark(f"🌡️{arrow}{abs(d2):.1f}°C",
                 f"{LOCATION} 现在{temp}°",
                 f"上次{lt}° {wmo(code)}",
                 "注意增减衣物",
                 level="timeSensitive", group="weather")
        h=d["hourly"]; times=h["time"]; probs=h["precipitation_probability"]
        now=cur["time"]; idx=next((i for i,t in enumerate(times) if t>=now),0)
        nxt=max((int(probs[j] or 0) for j in range(idx,min(idx+4,len(probs)))),default=0)
        if nxt>=60 and s.get("last_rain_prob",0)<30:
            bark(f"🌧️将降雨{nxt}%",
                 f"未来3小时降水{nxt}%",
                 f"当前{wmo(code)} {temp}°",
                 "出门请带伞",
                 level="timeSensitive", sound="rain", group="weather")
        s["last_temp"]=temp; s["last_rain_prob"]=nxt
        s["last_check"]=datetime.datetime.now().isoformat()
        print(f"[WX] {LOCATION} {temp}° {wmo(code)} 近3h降水{nxt}%")
    except Exception as e: print(f"[WX ERR] {e}")

def daily_forecast():
    """每天 7 点: 3 天预报, 每行一天"""
    try:
        d=fetch_weather(); daily=d["daily"]
        dates=daily["time"]; tmax=daily["temperature_2m_max"]
        tmin=daily["temperature_2m_min"]; codes=daily["weather_code"]
        pmax=daily["precipitation_probability_max"]
        labels=["今天","明天","后天"]
        lines=[f"{labels[i]} {wmo(codes[i])} {tmin[i]}°~{tmax[i]}°" for i in range(min(3,len(dates)))]
        while len(lines)<3: lines.append("")
        rain = f"降水{max(pmax[:3])}%" if pmax else ""
        bark(f"{LOCATION} 今日天气",
             lines[0], lines[1], f"{lines[2]} {rain}".strip(),
             group="weather")
    except Exception as e: print(f"[DAILY ERR] {e}")

def hourly_forecast():
    """24 小时展望: 现在 / 夜间最低 / 明天最高 / 降水风"""
    try:
        d=fetch_weather(); cur=d["current"]
        temp=cur["temperature_2m"]; code=int(cur.get("weather_code",0))
        wind=cur.get("wind_speed_10m","?")
        h=d["hourly"]; times=h["time"]; temps=h["temperature_2m"]
        probs=h["precipitation_probability"]; hcodes=h["weather_code"]
        now=cur["time"]; idx=next((i for i,t in enumerate(times) if t>=now),0)
        seg=temps[idx:idx+25]
        night_low = min(seg[:14]) if len(seg)>=14 else min(seg)
        day_high  = max(seg[12:24]) if len(seg)>=24 else max(seg[12:])
        max_rain  = max((int(probs[j] or 0) for j in range(idx,min(idx+24,len(probs)))),default=0)
        tmrw_code = hcodes[idx+14] if idx+14<len(hcodes) else code
        bark(f"{LOCATION} {temp}°{wmo(code)}",
             f"今夜 {temp:.0f}°→{night_low:.0f}° {wmo(code)}",
             f"明天午后 {day_high:.0f}° {wmo(tmrw_code)}",
             f"降水{max_rain}% 风{wind}km/h",
             group="weather")
    except Exception as e: print(f"[HOURLY ERR] {e}")

if __name__=="__main__":
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    s=load_state()
    if mode in ("earthquake","all"): check_earthquake(s)
    if mode in ("weather","all"):   check_weather(s)
    if mode=="daily":   daily_forecast()
    if mode=="hourly":  hourly_forecast()
    save_state(s)
    print(f"[OK] {mode} @ {datetime.datetime.now()}")
