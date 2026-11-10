#!/usr/bin/env python3
"""
Bark 双地点在线预警
地点: 通化东昌区 + 梧州长洲区
推送规格 (2026-11-10 苹果通知适配): 标题 1 行 + 副标题 1 行 + 正文最多 2 行
 依据: Bark 官方参数页 (Finb/Bark docs/params.md): subtitle 为可选字段,
 显示在标题下方、正文上方的较小字号位置, 不传则不显示这一行; title 显示在
 通知卡片第一行, body 为通知卡片主要内容。Bark 官方未给逐字段字数上限;
 iOS 锁屏折叠态实际能看全的约标题 40-50 字符、正文 90-120 字符 (第三方实测
 口径, 非 Apple 官方数字), APNs 整条 payload 上限 4096 字节。本文件自设上限
 (留足余量, 中文按字计): 标题 <=16 字, 副标题 <=20 字, 正文每行 <=30 字、
 合计 <=60 字, 整条通知合计 <=96 字, 超出用 … 截断。
 - 标题: 最短事件名, 如 温度突变; 紧急只加一个 🚨 前缀, 预警类只加
 一个 ⚠️ 前缀, 其余不加 emoji / 箭头 / 温度
 - 副标题: 时间 + 地点, 如 通化 11-10 09:00
 - 正文: 只留最关键的数据 + 建议, 次要字段 (上次温度、湿度、PM2.5、
 距另一地点距离、来源全称等) 已删掉, 缺数据写 未取到, 不编数
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
状态文件分流 (2026-11-10 晚报刷屏修复):
  - 天气/地震/watchdog 各写各的文件 (wx_state.json / eq_state.json /
    wd_state.json), 不再共用同一个 state.json。之前共用一个文件时,
    两个 workflow 每分钟同时回写, git pull --rebase 必然冲突,
    而回写步骤用 `|| true` 把失败吞掉、表面仍显示成功, 导致
    last_evening=2026-11-10 始终推不回 main, 晚报在 17:00-17:13
    每分钟重发一次 (14 次运行, 28 条)。分流后不同文件 rebase
    不再冲突; 回写失败时 workflow 必须真正报错 (见各 yml)。
  - 首次分流运行时, 各模式从旧 state.json 里只迁移自己那几项
    (见 migrate_legacy_state), 旧文件留作迁移源, 不要删。
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

def save_state(s, path=None):
    target = path or STATE_FILE
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)

# --- 状态分流: 每个模式只写自己的文件, 互不覆盖 ---
_STATE_KEYS = {
    "weather": {"wx", "last_morning", "last_evening", "nmc_pushed", "heartbeat"},
    "earthquake": {"last_eew_id", "last_eq_id", "last_tsunami_info", "last_usgs", "heartbeat"},
    "watchdog": {"watchdog", "heartbeat"},
}
_MODE_STATE_FILE = {
    "weather": "wx_state.json", "morning": "wx_state.json", "evening": "wx_state.json",
    "earthquake": "eq_state.json", "watchdog": "wd_state.json",
}

def state_file_for(mode):
    """该模式的状态文件. 显式设置了 STATE_FILE 环境变量时优先用它
    (workflow 里也会显式传, 双保险); 否则按模式分流; 未知模式用旧 state.json."""
    env = os.environ.get("STATE_FILE")
    if env:
        return env
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, _MODE_STATE_FILE.get(mode, "state.json"))

def load_state_file(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def migrate_legacy_state(legacy, mode):
    """首次分流: 从旧 state.json 里只拿本模式那几项, 别模式的键不带走,
    否则分流后又会把对方的旧值写回自己的文件, 再次互相覆盖."""
    keys = _STATE_KEYS.get(mode, set())
    return {k: v for k, v in (legacy or {}).items() if k in keys}

def load_state_for_mode(mode):
    """加载某模式的状态: 自己的文件在就用它; 不在且旧 state.json 在,
    就从旧文件迁移本模式的键 (下次保存时落盘到自己的文件)."""
    path = state_file_for(mode)
    if os.path.isfile(path):
        return load_state_file(path)
    base = os.path.dirname(os.path.abspath(__file__))
    legacy = os.path.join(base, "state.json")
    if path != legacy and os.path.isfile(legacy):
        print(f"[STATE] {mode}: 自己的 {os.path.basename(path)} 还没有, 从旧 state.json 迁移")
        return migrate_legacy_state(load_state_file(legacy), mode)
    return {}

def freshest_heartbeat(*heartbeats):
    """取最新的一条 heartbeat (按 last_ok 解析, 解析失败的忽略).
    watchdog 要看天气和地震两个模式的存活, 哪个新就信哪个."""
    best, best_dt = {}, None
    for hb in heartbeats:
        if not isinstance(hb, dict):
            continue
        try:
            dt = datetime.datetime.fromisoformat(str(hb.get("last_ok", "")).replace("Z", "+00:00"))
        except Exception:
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = hb, dt
    return best

def load_watchdog_state():
    """watchdog 自己的状态 (wd_state.json) + 天气/地震两个心跳里最新的一条.
    三个文件各写各的, watchdog 只读天气/地震的心跳, 不写它们的文件."""
    s = load_state_for_mode("watchdog")
    candidates = [s.get("heartbeat")]
    for mode in ("weather", "earthquake"):
        p = state_file_for(mode)
        if os.path.isfile(p):
            candidates.append(load_state_file(p).get("heartbeat"))
    s["heartbeat"] = freshest_heartbeat(*candidates)
    return s

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

# 自设上限: 标题<=16字, 副标题<=20字, 正文每行<=30字、合计<=60字, 整条<=96字
TITLE_MAX, SUBTITLE_MAX, BODY_LINE_MAX, BODY_MAX = 16, 20, 30, 60

# 通知图标: Bark 官方 icon 字段 = 图片 URL, 替换通知里默认的 Bark 图标,
# 图标缓存在手机上, 同一 URL 只下载一次, 首次下载超 10 秒退回默认图标,
# 需要 iOS 15 及以上 (Finb/Bark docs/params.md)。官方没给格式/尺寸要求,
# 也没有现成的分类型图标, 所以图标文件放本仓库 icons/ 目录, 用
# raw.githubusercontent.com 直链; 文件随代码一起推上去后链接才生效,
# 推上去之前图标 URL 是 404, 那条推送会显示默认 Bark 图标, 不会失败。
ICON_BASE = "https://raw.githubusercontent.com/qq2641205717-tech/bark-alert/main/icons"
ICONS = {
    "temp_up": "temp_up.png", "temp_down": "temp_down.png",
    "rain_start": "rain_start.png", "rain_stop": "rain_stop.png",
    "rain_heavy": "rain_heavy.png", "snow": "snow.png",
    "sunny": "sunny.png", "air_bad": "air_bad.png",
    "morning": "morning.png", "evening": "evening.png",
    "earthquake": "earthquake.png", "tsunami": "tsunami.png",
    "warning": "warning.png", "system": "system.png",
}

def _clip(text, n):
    """超长截断, 保留前 n-1 字 + …, 保证苹果通知上能看完."""
    t = "" if text is None else str(text)
    return t if len(t) <= n else t[:n - 1] + "…"

def bark(title, subtitle, l1, l2=None, level="timeSensitive", sound=None, group="wx", url=None, icon=None):
    """苹果通知三段式: title 标题 / subtitle 副标题 / body 正文(1-2 行).
    subtitle 为 Bark 官方可选字段, 不传则 iOS 不显示这一行, 本文件总是传.
    icon 为 ICONS 里的 key (如 "temp_up"), 按提醒类型选图标, 传错 key 则不发 icon."""
    if not BARK_KEY:
        print("[WARN] BARK_KEY 未设置"); return
    if level == "critical":
        times = 3
    else:
        times = 1
    title = _clip(title, TITLE_MAX)
    subtitle = _clip(subtitle, SUBTITLE_MAX)
    lines = [_clip(l1, BODY_LINE_MAX)] + ([_clip(l2, BODY_LINE_MAX)] if l2 else [])
    body = "\n".join(lines)
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX - 1] + "…"
    payload = {"title": title, "subtitle": subtitle, "body": body,
               "level": level, "group": group}
    if sound: payload["sound"] = sound
    if url:   payload["url"] = url
    if icon and icon in ICONS:
        payload["icon"] = ICON_BASE + "/" + ICONS[icon]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for n in range(times):
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

def fmt_bj_now_str():
    """当前北京时间, 统一为 '11-10 09:00'."""
    return fmt_bj_now().strftime("%m-%d %H:%M")

def fmt_iso_bj(s):
    """ISO UTC 时间 (如 '2026-11-10T03:45:26Z') 转北京时间 '11-10 11:45', 解析失败为 '未取到'."""
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (dt + datetime.timedelta(hours=8)).strftime("%m-%d %H:%M")
    except Exception:
        return "未取到"

def fmt_title(loc, event, prefix=""):
    """统一标题: 【地点】事件名, prefix 只允许 '' / '🚨' / '⚠️' 其中一个."""
    return f"{prefix}【{loc}】{event}" if prefix else f"【{loc}】{event}"

def _txt(x):
    """文本字段: 空 / '?' / None 一律写 '未取到', 不编数."""
    s = "" if x is None else str(x).strip()
    return s if s and s != "?" else "未取到"

def _temp(x):
    """温度字段: 数值写 'X°C', 拿不到写 '未取到'."""
    if x is None or str(x).strip() in ("", "?"):
        return "未取到"
    return f"{x}°C"

def _km(x):
    """距离字段: 数值写 'Xkm', 拿不到写 '未取到'."""
    try:
        return f"{float(x):.0f}km"
    except Exception:
        return "未取到"

def _deg(x):
    """温度简写: 数值取整写 '5°', 拿不到写 '未取到' (省字数)."""
    try:
        return f"{float(x):.0f}°"
    except Exception:
        return "未取到"

def _sub(loc, time_str):
    """副标题统一格式: '地点 MM-DD HH:MM', 如 '通化 11-10 09:00'."""
    return f"{loc} {fmt_time(time_str)}"

def _aqi_advice(tag):
    if tag == "fresh":
        return "适宜户外活动"
    if tag in ("polluted", "severe"):
        return "请减少户外活动"
    return "请注意防护"

def eq_short(nearest_name, time_str, dist, depth, intensity, source):
    """地震三段式文案 (副标题 + 正文 2 行, 不含地点全称):
    subtitle '地点 MM-DD HH:MM' / L1 '距最近点: …; 深度: …; 烈度: …'
    / L2 '来源: …'。距另一地点距离为次要字段, 已删掉, 不编数。"""
    depth_txt = _km(depth)
    intensity_txt = str(intensity).strip() if str(intensity).strip() not in ("", "?") else "未取到"
    return (
        _sub(nearest_name, time_str),
        f"距{nearest_name}: {_km(dist)}; 深度: {depth_txt}; 烈度: {intensity_txt}",
        f"来源: {_txt(source)}",
    )

def other_loc(nearest_name):
    """返回另一个监控点的名字和到它的距离计算用地点, 供 eq_short 用."""
    for loc in LOCATIONS:
        if loc["name"] != nearest_name:
            return loc
    return LOCATIONS[0]

def aqi_text(aqi, pm25):
    """AQI/PM2.5 一段, 统一为 'AQI 70, PM2.5 20', 拿不到写 '未取到', 不编数."""
    if aqi is None:
        return "未取到"
    return f"AQI {aqi}, PM2.5 {pm25}"

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

def send_morning_if_due(s, persist=None):
    """早报: 7-10 点窗口内、今天还没发过才发, 发完立刻把标记写下来.
    persist 是保存回调 (主入口传 save_state): 发完立刻落盘, 即使后面
    的天气检查出异常、进程提前退出, 这条已发标记也不会丢, 下一分钟
    不会再发一次。没有 persist 时只改内存里的 s (供离线测试用)."""
    bj_now = fmt_bj_now()
    bj_date = bj_now.strftime("%Y-%m-%d")
    if 7 <= bj_now.hour < 10 and s.get("last_morning") != bj_date:
        print(f"[FORECAST] 发送早报 {bj_now.strftime('%H:%M')}")
        morning_forecast()
        s["last_morning"] = bj_date
        if persist:
            persist(s)
        return True
    return False

def send_evening_if_due(s, persist=None):
    """晚报: 17-22 点窗口内、今天还没发过才发, 发完立刻落盘 (同上).
    这就是 2026-11-10 刷屏的直接防线: 标记一旦落盘, 下一分钟
    读到的就是今天已发, 不会再推."""
    bj_now = fmt_bj_now()
    bj_date = bj_now.strftime("%Y-%m-%d")
    if 17 <= bj_now.hour < 22 and s.get("last_evening") != bj_date:
        print(f"[FORECAST] 发送晚报 {bj_now.strftime('%H:%M')}")
        evening_forecast()
        s["last_evening"] = bj_date
        if persist:
            persist(s)
        return True
    return False

def check_weather(s, persist=None):
    wx = s.setdefault("wx", {})
    # 清理已从监控列表移除的旧地点残留状态(如已删除的海参崴)
    valid_names = {loc["name"] for loc in LOCATIONS}
    for k in list(wx.keys()):
        if k not in valid_names:
            wx.pop(k, None)
    # --- 早晚报: 按北京时间窗口判断, 每天只发一次 ---
    send_morning_if_due(s, persist=persist)
    send_evening_if_due(s, persist=persist)
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
            now_txt = fmt_time(cur.get("time", "")) or fmt_bj_now_str()
            if lt is not None and abs(temp-lt) >= 5:
                diff = temp-lt; arrow = "升温" if diff>0 else "降温"
                bark(fmt_title(name, "温度突变"), _sub(name, now_txt),
                    f"现在{_deg(temp)}, 体感{_deg(apparent)}, {arrow}{abs(diff):.1f}°",
                        f"风{wind}km/h, AQI{cur_aqi if cur_aqi is not None else '未取到'}, 注意增减衣物",
                            icon="temp_up" if diff > 0 else "temp_down", level="timeSensitive", group="wx")
            h = d["hourly"]; times=h["time"]; probs=h["precipitation_probability"]
            now = cur["time"]; idx = next((i for i,t in enumerate(times) if t>=now), 0)
            nxt = max((int(probs[j] or 0) for j in range(idx, min(idx+4, len(probs)))), default=0)
            _t3 = [float(x) for x in h.get("temperature_2m", [])[idx:idx+4]]
            t3_txt = f"{min(_t3):.0f}~{max(_t3):.0f}°C" if _t3 else "未取到"
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
                bark(fmt_title(name, f"{pt or '降雨'}开始"), _sub(name, now_txt),
                    f"未来3小时降水{nxt}%",
                        f"{'注意防滑' if pt == '雪' else '出门请带伞'}",
                            icon="snow" if pt == "雪" else "rain_start", level="timeSensitive", sound="rain", group="wx")
                st["last_rain_state"] = 1; st["last_rain_change"] = now_ts
            # 雨/雪停了
            elif new_st == 0 and old_st == 1 and (now_ts - st.get("last_rain_change",0)) >= cooldown:
                bark(fmt_title(name, f"{pt or '降雨'}趋停"), _sub(name, now_txt),
                    f"未来3小时降水{nxt}%",
                        "降水渐止, 可择机出行",
                            icon="rain_stop", level="timeSensitive", group="wx")
                st["last_rain_state"] = 0; st["last_rain_change"] = now_ts
            # 雨越下越大: 降水概率比上次涨>=30且已在下雨
            elif new_st == 1 and old_st == 1 and nxt - st.get("last_nxt",0) >= 30 and (now_ts - st.get("last_rain_change",0)) >= cooldown:
                bark(fmt_title(name, f"{pt or '降雨'}越下越大"), _sub(name, now_txt),
                    f"未来3小时降水{nxt}%",
                        "注意防范强降水",
                            icon="rain_heavy", level="timeSensitive", sound="rain", group="wx")
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
                    _wx_txt = (f"{wmo(code)}"
                               if 'temp' in locals() and temp is not None else "未取到")
                    bark(fmt_title(name, "空气质量变化"), _sub(name, fmt_bj_now_str()),
                        f"AQI{aqi}, PM2.5 {pm25}",
                            _aqi_advice(tag),
                                icon="air_bad", level="timeSensitive", group="wx")
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
                _aqi_txt = f"AQI {_aqi}, PM2.5 {_pm25}"
            except Exception:
                _aqi_txt = "未取到"
            bark(fmt_title(name, "今日天气"), f"{name} {fmt_bj_now().strftime('%m-%d')}",
                f"{wmo(today_code)}, 最高{_deg(tmax)}, 最低{_deg(tmin)}",
                    f"降水{pmax}%, {_aqi_txt}",
                        icon="sunny" if today_code in (0, 1) else "morning", level="timeSensitive", group="wx")
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
                _aqi_txt = f"AQI {_aqi}, PM2.5 {_pm25}"
            except Exception:
                _aqi_txt = "未取到"
            bark(fmt_title(name, "今夜到明早"), f"{name} {fmt_bj_now().strftime('%m-%d')}",
                f"夜间{wmo(night_code)}, 最低{night_low:.0f}°",
                    f"明早{wmo(morn_code)}{morn_temp:.0f}°, 降水{max_rain}%",
                        icon="evening", level="timeSensitive", group="wx")
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
                _pub = info['time'][5:16].replace('/','-') if len(info['time']) >= 16 else _txt(info['time'])
                bark(fmt_title(matched, f"{wtype}{color}色预警", prefix="⚠️"),
                    f"{matched} {_pub}",
                        f"{wtype}{color}色预警, 共{ns}站",
                            f"{tag}, 中央气象台",
                                icon="warning", level=level, sound=sound, group="alert",
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
                    sub, l1, l2 = eq_short(name, t, dist, e.get('Depth','?'),
                                           intensity, "CENC EEW")
                    bark(fmt_title(name, f"{tag} M{mag}", prefix="🚨" if level == "critical" else ""),
                         sub, l1, l2,
                         icon="earthquake", level=level, sound=sound, group="eq",
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
                    _src = 'CENC速报'
                    sub, l1, l2 = eq_short(name, t, dist, latest.get('depth','?'),
                                           latest.get('intensity') if latest.get('intensity') else '?',
                                               _src)
                    bark(fmt_title(name, f"{tag} M{mag}", prefix="🚨" if level == "critical" else ""),
                         sub, l1, l2,
                         icon="earthquake", level=level, sound=sound, group="eq",
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
            bark(fmt_title("海啸", "海啸预警", prefix="🚨"),
                _txt(latest.get('location','?')),
                    f"M{_txt(latest.get('magnitude','?'))}, 震度{_txt(latest.get('shindo','?'))}, {_txt(info)}",
                        "来源: 日本气象厅",
                            icon="tsunami", level="critical", sound="alarm", group="tsunami",
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
            sub, l1, l2 = eq_short(name, hh, dmin,
                                           coords[2] if len(coords) >= 3 else '?',
                                               '?', "USGS")
            bark(fmt_title(name, f"{tag} M{mag}", prefix="🚨" if level == "critical" else ""),
                 sub, l1, l2,
                 icon="earthquake", level=level, sound=sound, group="eq",
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
        bark(fmt_title("系统", "预警系统存活异常", prefix="⚠️"),
            f"{(now + datetime.timedelta(hours=8)).strftime('%m-%d %H:%M')}",
                f"上次成功{fmt_iso_bj(last_ok)}, 已停{age_min:.0f}分钟",
                    "请检查调用方",
                        icon="system", level="timeSensitive", group="sys",
                            url="https://github.com/qq2641205717-tech/bark-alert/actions")

# ---------- 入口 ----------
if __name__=="__main__":
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    if mode == "watchdog":
        # watchdog 只写 wd_state.json, 心跳从 wx/eq 两个文件里取最新的
        STATE_FILE = state_file_for("watchdog")
        s = load_watchdog_state()
        check_watchdog(s)
        save_state(s)
    elif mode in ("morning", "evening"):
        # 以前这里是直接调 morning_forecast()/evening_forecast(), 不经过
        # 去重, 外部每分钟调一次就会每分钟发一次。现在走同一套去重:
        # 只在当天没发过时发, 发完立刻落盘到 wx_state.json。
        STATE_FILE = state_file_for("weather")
        s = load_state_for_mode("weather")
        if mode == "morning":
            send_morning_if_due(s, persist=save_state)
        else:
            send_evening_if_due(s, persist=save_state)
        s["heartbeat"] = {
            "last_ok": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": mode,
        }
        save_state(s)
    else:
        # weather / earthquake / all: 各模式只读、只写自己的状态文件
        STATE_FILE = state_file_for(mode)
        s = load_state_for_mode(mode) if mode in _MODE_STATE_FILE else load_state()
        if mode in ("earthquake","all"): check_earthquake(s)
        if mode in ("weather","all"):
            check_weather(s, persist=save_state)
            check_nmc_alerts(s)
        # 每次成功跑完 (各源内部失败已分别捕获, 不影响整轮) 写心跳, 供 watchdog 用
        s["heartbeat"] = {
            "last_ok": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": mode,
        }
        save_state(s)
    print(f"[OK] {mode} @ {datetime.datetime.now()}")
