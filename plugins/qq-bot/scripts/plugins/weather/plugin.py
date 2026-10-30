# -*- coding: utf-8 -*-
"""所在地天气感知插件：城市 → Open-Meteo 实况一行中文（1h 缓存，wttr.in 兜底）。
自 agent 内联 _weather_line/_weather_context_line 迁入。
城市优先取插件设置；为空时回落 app_config.weather_city（兼容旧设置页配置）。"""

import json
import time
import urllib.parse

_HOSTS = {"api.open-meteo.com", "geocoding-api.open-meteo.com", "wttr.in"}
_CACHE = {"city": "", "geo": None, "geo_ts": 0.0, "text": "", "text_ts": 0.0}
_WMO_ZH = {
    0: "晴", 1: "大部晴", 2: "局部多云", 3: "阴", 45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
}
_API = None


def register(api):
    global _API
    _API = api
    api.inject_context(lambda ct, cid: _context_block())


def _city():
    c = (_API.setting("city", "") if _API else "") or ""
    c = str(c).strip()
    if c:
        return c
    # 兼容：插件设置没填时回落旧设置页的城市配置
    try:
        import qq_common as C
        return str(C.load_app_config().get("weather_city") or "").strip()
    except Exception:
        return ""


def _https_json(host, path, timeout=8):
    if host not in _HOSTS:
        raise ValueError("host not allowed: %s" % host)
    import http.client
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    conn.request("GET", path, headers={"User-Agent": "qq-bot/0.1"})
    raw = conn.getresponse().read().decode("utf-8", "replace")
    conn.close()
    return json.loads(raw)


def _line(api=None):
    """所在地当前天气一行中文摘要（1 小时缓存；失败返回空串）。"""
    city = _city()
    if not city:
        return ""
    now = time.time()
    if _CACHE["text"] and _CACHE["city"] == city and now - _CACHE["text_ts"] < 3600:
        return _CACHE["text"]
    try:
        geo = _CACHE["geo"]
        if _CACHE["city"] != city or now - _CACHE["geo_ts"] > 86400:
            g = _https_json("geocoding-api.open-meteo.com",
                            "/v1/search?name=%s&count=1&language=zh"
                            % urllib.parse.quote(city, safe=""))
            hits = g.get("results") or []
            if not hits:
                raise LookupError("no geo result")
            geo = (hits[0]["latitude"], hits[0]["longitude"])
            _CACHE["geo"] = geo
            _CACHE["geo_ts"] = now
            _CACHE["city"] = city
        w = _https_json("api.open-meteo.com",
                        "/v1/forecast?latitude=%s&longitude=%s"
                        "&current_weather=true&timezone=auto" % geo)
        cur = w.get("current_weather") or {}
        code = int(cur.get("weathercode") or 0)
        desc = _WMO_ZH.get(code, "天气代码%s" % code)
        temp = cur.get("temperature")
        wind = cur.get("windspeed")
        text = "%s %s°C，风 %skm/h" % (
            desc, ("%.0f" % temp) if isinstance(temp, (int, float)) else "?",
            ("%.0f" % wind) if isinstance(wind, (int, float)) else "?")
    except Exception as e:
        if _API:
            _API.log("OM_FAIL %r" % e)
        try:  # 兜底：wttr.in（描述为英文，模型能理解）
            j = _https_json("wttr.in", "/%s?format=j1"
                            % urllib.parse.quote(city, safe=""))
            cur = (j.get("current_condition") or [{}])[0]
            text = "%s %s°C（体感 %s°C）" % (
                str(((cur.get("weatherDesc") or [{}])[0]).get("value") or "?"),
                cur.get("temp_C", "?"), cur.get("FeelsLikeC", "?"))
        except Exception as e2:
            if _API:
                _API.log("FAIL %r" % e2)
            return ""
    _CACHE["text"] = text
    _CACHE["text_ts"] = time.time()
    _CACHE["city"] = city
    return text


def _context_block():
    """普通聊天的天气注入块。"""
    t = _line()
    if not t:
        return ""
    return ("\n\n## 当前天气（所在地实况，可自然引用，不必每次都提）\n- %s" % t)


def context_line():
    """主动消息素材行（agent 的 _proactive_compose 调用；AI 自行决定是否提及）。"""
    t = _line()
    if not t:
        return ""
    return ("（你所在地【%s】现在的天气：%s——只是素材，想聊可以自然带一句"
            "（冷热/下雨收伞/这种天适合干嘛），不想提就当没看见；"
            "千万别每次主动说话都扯天气，那样很死板。）"
            % (_city(), t))
