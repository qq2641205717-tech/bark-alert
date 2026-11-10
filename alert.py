#!/usr/bin/env python3
"""
Bark 双地点在线预警
地点: 通化东昌区 + 梧州长洲区
推送规格: 标题 1 行 + 正文 3 行 (Bark 规格不变, 丰富内容只在 3 行里塞, 不加行)
地震分级: 见 _eq_level, 本次改动未放宽任何阈值, 未升任何 timeSensitive 为 critical
天气:
  - 每30分钟: 温度突变>=5°C / 降水概率突变 <30->60%
  - 早上7点:  各地点当天天气
  - 傍晚18点: 各地点今夜到明早
可靠性 (本次新增):
  - http_get_json / bark() 均带重试 (外部请求 3 次, Bark 推送每发 2 次),
    失败只打日志, 不让单个源拖垮整轮
  - 每次成功跑完写 heartbeat.last_ok (UTC), 供 watchdog 模式判断存活
  - watchdog 模式: state 超 15 分钟无成功运行才记一次 stale,
    连续 2 次 stale 且距上次提醒 >1 小时才发一条 timeSensitive 提醒,
    绝不用 critical, 避免单次排队延迟造成误报
"""
import os, sys, json, math, time, urllib.request, datetime

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

def http_get_json(url, timeout=12, retries=3, headers=None):
    """外部请求带重试: 3 次, 间隔 2s / 5s, 3 次都失败才抛, 调用方按源分别捕获."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers=headers or {"User-Agent": "bark-alert/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_exc = e
            print(f"[HTTP] attempt {attempt}/{retries} failed {url[:80]}: {e}")
            if attempt < retries:
                time.sleep(2 if attempt == 1 else 5)
    raise last_exc

def bark(title, l1, l2, l3, level="timeSensitive", sound=None, group="wx", url=None):
    if not BARK_KEY:
        print("[WARN] BARK_KEY 未设置"); return
    # critical 级别: 连推3声 (原有行为不变)
    if level == "critical":
        times = 3
    else:
        times = 1
    payload = {"title": title, "body": f"{l1}\n{l2}\n{l3}", "level": level, "group": group}
    if sound: payload["sound"] = sound
    if url:   payload["url"] = url
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for n in range(times):
        # 每发一次最多试 2 遍, 避免一次网络抖动就丢一条提醒; 不补发已成功的遍
        sent = False
        for attempt in range(1, 3):
            try:
                req = urllib.request.Request(
                    f"https://api.day.app/{BARK_KEY}", data=data,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=12) as r:
                    print(f"[BARK] {title} ({n+1}/{times}) -> {json.loads(r.read())}")
                sent = True
                break
            except Exception as e:
                print(f"[BARK ERROR] {title} ({n+1}/{times}) attempt {attempt}: {e}")
                if attempt < 2:
                    time.sleep(2)
        if not sent:
            print(f"[BARK FAIL] {title} ({n+1}/{times}) 2 次都未发出, 丢弃不补发")

def fmt_time(t):
    """把 '2026-11-10 02:15:00' / ISO 时间统一成 '11-10 02:15', 解析失败原样返回."""
    s = str(t or "").strip()
    if len(s) >= 16 and s[4:5] == "-" and s[7:8] == "-":
        return s[5:16].replace("T", " ")
    return s

def fmt_bj_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)

def eq_lines(place, time_str, nearest_name, dist, other_name, other_dist,
             depth, intensity, source):
    """地震 3 行文案 (标题已带震级和分级, 这里只补其余字段, 不编数):
    L1 地点 · 发震时间 / L2 距最近点距离 · 深度 · 烈度 / L3 距另一地点距离 · 来源.
    depth/intensity 为 '?' 时原样写 '?', 不猜."""
    return (
        f"{str(place)[:24]} · {fmt_time(time_str)}",
        f"距{nearest_name} {dist:.0f}km · 深{depth}km · 烈度{intensity}",
        f"距{other_name} {other_dist:.0f}km · {source}",
    )

def other_loc(nearest_name):
    """返回另一个监控点的名字和到它的距离计算用地点, 供 eq_lines 用."""
    for loc in LOCATIONS:
        if loc["name"] != nearest_name:
            return loc
    return LOCATIONS[0]

def aqi_text(aqi, pm25):
    """AQI/PM2.5 一行后缀, 拿不到就返回空串, 不编数."""
    if aqi is None:
        return ""
    return f"AQI {aqi} · PM2.5 {pm25}"

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

def fetch_aqi(loc):
    url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={loc['lat']}&longitude={loc['lon']}"
           "&current=us_aqi,pm2_5,pm10&timezone=Asia%2FShanghai")
    return http_get_json(url)

def aqi_level(aqi):
    if aqi <= 50:  return "🌿", "空气很清新", "fresh"
    if aqi <= 100: return "🙂", "空气一般", None
    if aqi <= 150: return "😷", "轻度污染", None
    if aqi <= 200: return "😷", "空气被污染", "polluted"
    return "🚨", "空气严重污染", "severe"

def check_weather(s):
    wx = s.setdefault("wx", {})
    # 清理已从监控列表移除的旧地点残留状态(如已删除的海参崴)
    valid_names = {loc["name"] for loc in LOCATIONS}
    for k in list(wx.keys()):
        if k not in valid_names:
            wx.pop(k, None)
    # --- 早晚报: 按北京时间窗口判断, 每天只发一次 ---
    bj_now = fmt_bj_now()
    bj_date = bj_now.strftime("%Y-%m-%d")
    bj_hour = bj_now.hour
    last_morning = s.get("last_morning", "")
    last_evening = s.get("last_evening", "")
    if 7 <= bj_hour < 10 and last_morning != bj_date:
        print(f"[FORECAST] 发送早报 {bj_now.strftime('%H:%M')}")
        morning_forecast()
        s["last_morning"] = bj_date
    if 17 <= bj_hour < 22 and last_evening != bj_date:
        print(f"[FORECAST] 发送晚报 {bj_now.strftime('%H:%M')}")
        evening_forecast()
        s["last_evening"] = bj_date
    for loc in LOCATIONS:
        name = loc["name"]
        temp = None; wind = "?"; code = 0; apparent = "?"; humidity = "?"
        # AQI 先取一次, 供本轮天气文案复用 (取不到为 None, 文案里不编数);
        # 去重逻辑仍在后面的 AQI 段, 只用这里取到的值, 不重复请求
        cur_aqi, cur_pm25 = None, None
        try:
            _aq = fetch_aqi(loc)
            cur_aqi = int(_aq["current"]["us_aqi"])
            cur_pm25 = _aq["current"].get("pm2_5", "?")
        except Exception as e:
            print(f"[AQI {name} PREFETCH ERR] {e}")
        try:
            d = fetch_wx(loc); cur = d["current"]
            temp = cur["temperature_2m"]; code = int(cur.get("weather_code",0))
            wind = cur.get("wind_speed_10m","?")
            apparent = cur.get("apparent_temperature", "?")
            humidity = cur.get("relative_humidity_2m", "?")
            st = wx.setdefault(name, {"last_temp":None, "last_rain_prob":0, "last_aqi":None})
            lt = st.get("last_temp")
            if lt is not None and abs(temp-lt) >= 5:
                diff = temp-lt; arrow = "升温" if diff>0 else "降温"
                bark(f"{name} 🌡️{arrow}{abs(diff):.1f}°C",
                     f"现在{temp}° 体感{apparent}° {wmo(code)}",
                     f"上次{lt}° · 风{wind}km/h · 湿度{humidity}%",
                     f"{aqi_text(cur_aqi, cur_pm25) + ' · ' if cur_aqi is not None else ''}注意增减衣物",
                     level="timeSensitive", group="wx")
            h = d["hourly"]; times=h["time"]; probs=h["precipitation_probability"]
            now = cur["time"]; idx = next((i for i,t in enumerate(times) if t>=now), 0)
            nxt = max((int(probs[j] or 0) for j in range(idx, min(idx+4, len(probs)))), default=0)
            _t3 = [float(x) for x in h.get("temperature_2m", [])[idx:idx+4]]
            t3_txt = f"{min(_t3):.0f}~{max(_t3):.0f}°" if _t3 else "?"
            st["last_temp"] = temp; st["last_rain_prob"] = nxt

            # --- 雨/雪变化检测(基于降水概率状态机, 防抖动) ---
            # 状态: 1=在下雨/雪, 0=不在, None=模糊带(30-50%不触发变化)
            def rain_state(p):
                return 1 if p >= 50 else (0 if p < 30 else None)
            def precip_type(c):
                if c in (51,53,55,56,57,61,63,65,66,67,80,81,82,95,96,99): return "雨"
                if c in (71,73,75,77,85,86): return "雪"
                return ""
            now_ts = time.time()
            st.setdefault("last_rain_state", None)
            st.setdefault("last_rain_change", 0)
            st.setdefault("last_nxt", 0)
            old_st = st.get("last_rain_state")
            new_st = rain_state(nxt)
            pt = precip_type(code)
            cooldown = 2 * 3600  # 同一地点同类提醒冷却2小时
            # 首采只建基线, 不推
            if old_st is None:
                if new_st is not None:
                    st["last_rain_state"] = new_st
                    st["last_nxt"] = nxt
            # 开始下雨/雪
            elif new_st == 1 and old_st == 0 and (now_ts - st.get("last_rain_change",0)) >= cooldown:
                bark(f"{name} 🌧️开始{pt or '降雨'}",
                     f"未来3小时降水{nxt}% · {t3_txt}",
                     f"现在{wmo(code)} {temp}° 体感{apparent}°",
                     f"出门记得带{pt=='雪' and '防滑' or '伞'} · 风{wind}km/h",
                     level="timeSensitive", sound="rain", group="wx")
                st["last_rain_state"] = 1; st["last_rain_change"] = now_ts
            # 雨/雪停了
            elif new_st == 0 and old_st == 1 and (now_ts - st.get("last_rain_change",0)) >= cooldown:
                bark(f"{name} 🌤️{pt or '降雨'}趋停",
                     f"未来3小时降水{nxt}% · {t3_txt}",
                     f"现在{wmo(code)} {temp}° 体感{apparent}°",
                     f"{pt or '雨'}渐止放晴 · 风{wind}km/h",
                     level="timeSensitive", group="wx")
                st["last_rain_state"] = 0; st["last_rain_change"] = now_ts
            # 雨越下越大: 降水概率比上次涨>=30且已在下雨
            elif new_st == 1 and old_st == 1 and nxt - st.get("last_nxt",0) >= 30 and (now_ts - st.get("last_rain_change",0)) >= cooldown:
                bark(f"{name} ⛈️{pt or '雨'}越下越大",
                     f"未来3小时降水{nxt}% · {t3_txt}",
                     f"现在{wmo(code)} {temp}° 体感{apparent}°",
                     f"注意防范 · 风{wind}km/h",
                     level="timeSensitive", sound="rain", group="wx")
                st["last_rain_change"] = now_ts
            if new_st is not None:
                st["last_rain_state"] = new_st
                st["last_nxt"] = nxt
            print(f"[WX] {name} {temp}° {wmo(code)} 近3h降水{nxt}%")
        except Exception as e:
            print(f"[WX {name} ERR] {e}")

        # AQI 变化检测 (复用本轮开头已取到的 AQI, 不再重复请求;
        # 阈值/去重逻辑不变: 只有 tag 跨档才推)
        try:
            if cur_aqi is None:
                raise ValueError("AQI prefetch failed")
            aqi = cur_aqi
            pm25 = cur_pm25
            st2 = wx.get(name, st)
            la = st2.get("last_aqi")
            emoji, desc, tag = aqi_level(aqi)
            print(f"[AQI] {name} AQI={aqi} (上次{la})")
            if la is not None:
                _, _, old_tag = aqi_level(la)
                if tag and tag != old_tag:
                    _wx_txt = (f"现在{temp}° {wmo(code)}"
                               if 'temp' in locals() and temp is not None else "天气数据未取到")
                    bark(f"{name} {emoji}{desc}",
                         f"AQI {aqi} (上次{la})",
                         f"PM2.5 {pm25}μg/m³ · {_wx_txt}",
                         f"{desc} · 风{wind}km/h",
                         level="timeSensitive", group="wx")
                    print(f"[AQI] {name} {la}->{aqi} {tag}")
            st2["last_aqi"] = aqi
        except Exception as e:
            print(f"[AQI {name} ERR] {e}")

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
            apparent = cur.get("apparent_temperature", "?")
            humidity = cur.get("relative_humidity_2m", "?")
            # AQI 只做文案补充, 取不到不编数, 失败不影响早报
            try:
                _aq = fetch_aqi(loc)
                _aqi = int(_aq["current"]["us_aqi"]); _pm25 = _aq["current"].get("pm2_5", "?")
                _aqi_txt = f"AQI {_aqi} · PM2.5 {_pm25}"
            except Exception:
                _aqi_txt = "AQI 未取到"
            bark(f"{name} 今日天气",
                 f"白天 {wmo(today_code)} {tmax}° · 体感{apparent}°",
                 f"夜间 {tmin}° · 降水{pmax}% · 风{wind}km/h",
                 f"{_aqi_txt} · 湿度{humidity}%",
                 level="timeSensitive", group="wx")
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
            morn_code = codes[morning_idx] if morning_idx < len(codes) else night_code
            wind = d["current"].get("wind_speed_10m", "?")
            try:
                _aq = fetch_aqi(loc)
                _aqi = int(_aq["current"]["us_aqi"]); _pm25 = _aq["current"].get("pm2_5", "?")
                _aqi_txt = f"AQI {_aqi} · PM2.5 {_pm25}"
            except Exception:
                _aqi_txt = "AQI 未取到"
            bark(f"{name} 今夜到明早",
                 f"夜间 {wmo(night_code)} 最低{night_low:.0f}° · 降水{max_rain}%",
                 f"明早 {wmo(morn_code)} {morn_temp:.0f}° · 风{wind}km/h",
                 f"{_aqi_txt}",
                 level="timeSensitive", group="wx")
        except Exception as e:
            print(f"[EVENING {name} ERR] {e}")

# ---------- 中央气象台官方预警 ----------
def check_nmc_alerts(s):
    """去重逻辑:
    - 首次运行只建基线不轰炸
    - 同key的issuetime变了(气象台新发布/更新) -> 重推
    - 持续预警每3小时再推一次提醒(防止用户遗忘)
    - 预警解除后自动清除, 再发会重推
    """
    try:
        # 走带重试的 http_get_json (3 次), 不再裸 urlopen 一次失败就丢整轮
        d = http_get_json(
            "http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=80&signaltype=&signallevel=&province=",
            headers={"User-Agent": "Mozilla/5.0"})
        alerts = d.get("data",{}).get("page",{}).get("list",[])
        print(f"[NMC] 全国预警{len(alerts)}条, 匹配通化/梧州")

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
        now = datetime.datetime.now().timestamp()
        REMIND_INTERVAL = 3 * 3600  # 持续预警3小时提醒一次

        for key, info in current.items():
            prev = pushed.get(key, {})
            # 兼容旧格式 (值为True)
            if not isinstance(prev, dict):
                prev = {}
            need_push = False
            reason = ""

            if first_run:
                # 首次运行: 如果预警是最近12小时内发的, 也推一次(不漏报新预警)
                try:
                    t = info["time"].replace("/", "-")
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            itime = datetime.datetime.strptime(t[:19], fmt)
                            break
                        except:
                            continue
                    age_hours = (datetime.datetime.now() - itime).total_seconds() / 3600
                    if age_hours < 12:
                        need_push = True
                        reason = "当前活跃预警"
                except:
                    pass
            elif prev.get("issuetime") != info["time"]:
                need_push = True
                reason = "新发布/更新"
            elif now - prev.get("last_push", 0) > REMIND_INTERVAL:
                need_push = True
                reason = "持续提醒"

            if need_push:
                matched, wtype, color = key.split("|")
                # 天气预警全部只通知, 不响铃 (级别不变)
                level = "timeSensitive"
                sound = None
                ns = len(info["stations"])
                station_txt = info["stations"][0][:16] + (f"等{ns}地" if ns>1 else "")
                tag = "持续中" if reason == "持续提醒" else "新预警"
                bark(f"⚠️{matched} {color}色{wtype}预警",
                     f"{station_txt} · 共{ns}站发布",
                     f"发布 {info['time'][5:16].replace('/','-')} · {color}色{wtype}",
                     f"{tag} · 中央气象台",
                     level=level, sound=sound, group="alert",
                     url="http://www.nmc.cn/publish/alarm.html")
                print(f"[NMC] {key} {reason} ({ns}台站)")

            pushed[key] = {"issuetime": info["time"],
                           "last_push": now if need_push else prev.get("last_push", now)}

        # 清除已解除的预警
        s["nmc_pushed"] = {k: v for k, v in pushed.items() if k in current}
    except Exception as e:
        print(f"[NMC ERR] {e}")

# ---------- 地震 ----------
def _eq_level(mag, intensity, dist):
    """分级:
    critical(3响): 威胁到安全的 - M>=9无限制 / M>=8<=800km / M>=7<=300km / M>=6<=100km / M>=5<=50km / M>=4<=50km
    timeSensitive(1响): 大地震无论多远至少通知 - M>=7无上限 / 见下
    """
    # critical: 强响
    # 超强震(灾难级)无距离限制, 必须提醒
    if mag >= 9.0:
        return "critical", "alarm", "超级强震"
    if mag >= 8.0 and dist <= 800:
        return "critical", "alarm", "强震M"
    if mag >= 7.0 and dist <= 300:
        return "critical", "alarm", "强震M"
    if mag >= 6.0 and dist <= 100:
        return "critical", "alarm", "强震M"
    if mag >= 5.0 and dist <= 50:
        return "critical", "alarm", "当地强震"
    if mag >= 4.0 and dist <= 50:
        return "critical", "alarm", "当地地震"
    # timeSensitive: 轻响
    # 7级以上破坏性大地震, 无论多远都至少通知
    if mag >= 8.0 and dist > 800:
        return "timeSensitive", "update", "远强震"
    if mag >= 7.0 and dist > 300:
        return "timeSensitive", "update", "大地震"
    if mag >= 6.0 and dist <= 800:
        return "timeSensitive", "update", "远强震"
    if mag >= 5.0 and dist <= 500:
        return "timeSensitive", "update", "中强震"
    if mag >= 4.0 and dist <= 500:
        return "timeSensitive", "update", "有感地震"
    if mag >= 3.0 and dist <= 500:
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
                _other = other_loc(name)
                other_dist = hav(_other["lat"], _other["lon"], elat, elon)
                intensity = float(e.get("MaxIntensity") or 0)
                lvl = _eq_level(mag, intensity, dist)
                if lvl:
                    level, sound, tag = lvl
                    t = str(e.get("OriginTime",""))
                    l1, l2, l3 = eq_lines(
                        e.get('HypoCenter','?'), t, name, dist,
                        _other["name"], other_dist, e.get('Depth','?'),
                        intensity, "CENC EEW")
                    bark(f"🚨{tag} M{mag}", l1, l2, l3,
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
                _other = other_loc(name)
                other_dist = hav(_other["lat"], _other["lon"], elat, elon)
                intensity = float(latest.get("intensity") or 0)
                lvl = _eq_level(mag, intensity, dist)
                if lvl:
                    level, sound, tag = lvl
                    t = str(latest.get("time",""))
                    _src = ('烈度'+str(latest.get('intensity'))
                            if latest.get('intensity') else 'CENC速报')
                    l1, l2, l3 = eq_lines(
                        latest.get('location','?'), t, name, dist,
                        _other["name"], other_dist, latest.get('depth','?'),
                        latest.get('intensity') if latest.get('intensity') else '?',
                        _src)
                    bark(f"📢{tag} M{mag}", l1, l2, l3,
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
            # 触发条件不变: 只有 JMA 文本里含警报/注意报/预报字样才推, 仍为 critical
            bark("🌊海啸预警",
                 f"日本气象厅发布 · {latest.get('location','?')}",
                 f"M{latest.get('magnitude','?')} · 震度{latest.get('shindo','?')}",
                 f"{info[:24]}",
                 level="critical", sound="alarm", group="tsunami",
                 url="https://www.jma.go.jp/jma/index.html")
        elif info:
            s["last_tsunami_info"] = info
    except Exception as e: print(f"[TSUNAMI ERR] {e}")

    # USGS 全球地震(官方, 覆盖全球)
    try:
        d = http_get_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson")
        feats = d.get("features", [])
        last = s.get("last_usgs","")
        for f in feats:  # 按时间新→旧
            fid = f.get("id","")
            if not fid or fid == last:
                continue
            p = f.get("properties",{})
            mag = float(p.get("mag") or 0)
            if mag < 3.0:
                continue
            coords = f.get("geometry",{}).get("coordinates",[])
            if len(coords) < 2:
                continue
            elon, elat = coords[0], coords[1]
            dist = min(hav(loc["lat"], loc["lon"], elat, elon) for loc in LOCATIONS)
            lvl = _eq_level(mag, 0, dist)
            if not lvl:
                continue
            level, sound, tag = lvl
            name, dmin = nearest_loc(elat, elon)
            _other = other_loc(name)
            other_dist = hav(_other["lat"], _other["lon"], elat, elon)
            t = p.get("time") or 0
            # 原代码把毫秒时间戳当秒算 UTC 小时, 是错的; 改为毫秒转 UTC 日期时间
            try:
                hh = datetime.datetime.fromtimestamp(
                    t / 1000, tz=datetime.timezone.utc).strftime("%m-%d %H:%M UTC")
            except Exception:
                hh = "?"
            l1, l2, l3 = eq_lines(
                str(p.get('place','?')), hh, name, dmin,
                _other["name"], other_dist,
                coords[2] if len(coords) >= 3 else '?',
                '?', "USGS")
            bark(f"📡{tag} M{mag}", l1, l2, l3,
                 level=level, sound=sound, group="eq",
                 url="https://earthquake.usgs.gov/")
            s["last_usgs"] = fid
            break  # 每轮只推最新一条
    except Exception as e: print(f"[USGS ERR] {e}")

# ---------- 存活/心跳 ----------
def check_watchdog(s):
    """存活检查, 由 watchdog workflow 每 5 分钟跑一次.
    判断依据: state.json 里 heartbeat.last_ok (每次 earthquake/weather 成功跑完才写).
    防误报:
      - 年龄 <=15 分钟: 正常, stale 计数清零, 不推
      - 年龄 >15 分钟: 只记一次 stale, 不立刻推
      - 连续 2 次 stale 且距上次提醒 >1 小时: 才发 1 条 timeSensitive,
        绝不用 critical; 之后 1 小时内不再重复提醒
    watchdog 本身也可能因 GitHub 故障跑不起来, 所以它只能发现"调用方停了 /
    状态回写断了"这类静默停摆, 不能替代 GitHub 状态页.
    """
    hb = s.get("heartbeat", {}) or {}
    last_ok = hb.get("last_ok", "")
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        last_dt = datetime.datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
        age_min = (now - last_dt).total_seconds() / 60
    except Exception:
        age_min = 9999  # 从没有成功过: 按 stale 处理, 但仍要过 2 次确认
        last_dt = None
    wd = s.setdefault("watchdog", {"stale_checks": 0, "last_alert": 0})
    now_ts = now.timestamp()
    if age_min <= 15:
        if wd.get("stale_checks"):
            print(f"[WATCHDOG] 恢复正常, 上次成功 {last_ok} ({age_min:.1f}分钟前)")
        wd["stale_checks"] = 0
        return
    wd["stale_checks"] = int(wd.get("stale_checks", 0)) + 1
    print(f"[WATCHDOG] stale {wd['stale_checks']} 次, 上次成功 {last_ok or '无'} ({age_min:.0f}分钟前)")
    if wd["stale_checks"] >= 2 and now_ts - float(wd.get("last_alert", 0)) > 3600:
        wd["last_alert"] = now_ts
        bark("⚠️预警系统存活异常",
             f"上次成功 {last_ok[5:16].replace('T', ' ') if len(last_ok) >= 16 else (last_ok or '无')}",
             f"已 {age_min:.0f} 分钟无成功运行 · {hb.get('mode', '?')}",
             "可能是调用方停了/GitHub故障 · 请检查",
             level="timeSensitive", group="sys",
             url="https://github.com/qq2641205717-tech/bark-alert/actions")

# ---------- 入口 ----------
if __name__=="__main__":
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    s=load_state()
    if mode == "watchdog":
        check_watchdog(s)
    else:
        if mode in ("earthquake","all"): check_earthquake(s)
        if mode in ("weather","all"):
            check_weather(s)
            check_nmc_alerts(s)
        if mode=="morning":  morning_forecast()
        if mode=="evening":   evening_forecast()
        # 每次成功跑完 (各源内部失败已分别捕获, 不影响整轮) 写心跳, 供 watchdog 用
        s["heartbeat"] = {
            "last_ok": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": mode,
        }
    save_state(s)
    print(f"[OK] {mode} @ {datetime.datetime.now()}")
