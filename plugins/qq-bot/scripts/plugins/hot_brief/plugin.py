# -*- coding: utf-8 -*-
"""每日热点速览插件：定时（可关，默认开）/手动立刻总结当日热点；历史分天存档，
插件页可查看与删除，支持自动清理 N 天前的旧记录。"""

import json
import os
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
BRIEF_PATH = os.path.join(DATA_DIR, "hot_brief.json")
HIST_DIR = os.path.join(DATA_DIR, "hot_history")
_MAX = 400


def register(api):
    api.on_tick(api.every(1, lambda d: _auto_check(api)))   # 每分钟检查到点
    api.route("refresh_now", lambda args: _refresh(api))
    api.route("history", lambda args: _history_list())
    api.route("get_day", lambda args: _get_day(str((args or {}).get("date") or "")))
    api.route("delete_day", lambda args: _delete_day(str((args or {}).get("date") or "")))
    api.route("set_auto", lambda args: _set_auto(api, bool((args or {}).get("enabled"))))
    api.route("set_auto_time", lambda args: _set_auto_time(
        api, (args or {}).get("hh"), (args or {}).get("mm")))
    api.route("set_clean_days", lambda args: _set_clean_days(
        api, (args or {}).get("days")))
    api.route("set_query", lambda args: _set_query(api, (args or {}).get("query")))
    api.route("load_auto", lambda args: {"ok": True,
                                         "auto_enabled": bool(api.load("auto_enabled", True)),
                                         "hh": int(api.load("auto_hh", 3)),
                                         "mm": int(api.load("auto_mm", 0)),
                                         "clean_days": _clean_days(api),
                                         "query": str(api.load("query", "") or "")})
    api.inject_context(_line)
    _clean_history(api)          # 启动即清一次（配合自动清理天数）
    api.log("ready")


def _clean_days(api):
    try:
        return max(0, min(3650, int(api.load("clean_days", 0))))
    except (TypeError, ValueError):
        return 0


def _set_clean_days(api, days):
    try:
        n = max(0, min(3650, int(days)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "天数格式非法（0-3650，0=不清理）"}
    api.store("clean_days", n)
    removed = _clean_history(api)
    return {"ok": True, "clean_days": n, "removed": removed}


def _clean_history(api):
    """删除 clean_days 天前的历史存档；返回删除的文件数。0=禁用不清。"""
    n = _clean_days(api)
    if n <= 0 or not os.path.isdir(HIST_DIR):
        return 0
    cutoff = time.time() - n * 86400
    removed = 0
    for f in os.listdir(HIST_DIR):
        if not f.endswith(".json"):
            continue
        p = os.path.join(HIST_DIR, f)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            continue
    if removed:
        api.log("auto-clean removed %d old hot records (>%dd)" % (removed, n))
    return removed


def _auto_check(api):
    """到点自动总结：每天在配置的 时:分 之后跑一次（错过开机则启动后补跑）。"""
    if not api.load("auto_enabled", True):
        return
    try:
        hh = max(0, min(23, int(api.load("auto_hh", 3))))
        mm = max(0, min(59, int(api.load("auto_mm", 0))))
    except (TypeError, ValueError):
        hh, mm = 3, 0
    lt = time.localtime()
    now_min = lt.tm_hour * 60 + lt.tm_min
    tgt_min = hh * 60 + mm
    if now_min < tgt_min:
        return
    today = time.strftime("%Y-%m-%d")
    if api.load("last_auto_day", "") == today:
        return
    api.store("last_auto_day", today)
    api.log("auto refresh @%02d:%02d" % (hh, mm))
    _refresh(api)


def _search_text(api):
    """按用户配置的来源关键词搜索（、/,/分隔，最多 3 组），合并结果。"""
    try:
        import qq_bot_agent as A
    except Exception as e:
        return "搜索失败：%r" % (e,)
    raw = str(api.load("query", "") or "").strip()
    if not raw:
        return ""          # 未配置自定义关键词时跳过搜索（默认来源=热榜直抓）
    queries = [q.strip() for q in raw.replace("，", "、").replace(",", "、").split("、")
               if q.strip()][:3]
    parts = []
    for q in queries:
        try:
            res, _backend = A.search_web(q)
            res = str(res or "").strip()
            if res:
                parts.append("【%s】\n%s" % (q, res))
        except Exception as e:
            parts.append("【%s】搜索失败：%r" % (q, e))
    return "\n\n".join(parts) if parts else ""


def _collect(api):
    """默认来源=微博热搜+百度热搜直抓；用户配置了关键词再叠加搜索结果。
    榜单全挂时回退纯搜索（未配置关键词则报错提示）。"""
    rows, errs = [], []
    for name, fn in (("微博热搜", _fetch_weibo), ("百度热搜", _fetch_baidu)):
        try:
            items = fn()
            if items:
                rows.append("【%s】\n%s" % (name, "\n".join("- " + x for x in items)))
        except Exception as e:
            errs.append("%s(%r)" % (name, e))
    search = _search_text(api)
    if search and "搜索失败" not in search[:20]:
        rows.append(search)
    if rows:
        return "\n\n".join(rows), errs
    if search:
        return search, errs
    raise RuntimeError("热榜抓取失败(%s)且未配置搜索关键词；"
                       "可在插件页「热点来源」填搜索关键词作备用来源"
                       % "、".join(errs) or "未知")


_SUM_SYS = ("你是新闻编辑。输入是热榜词条（微博热搜/百度热搜等）和网页搜索结果。"
            "网页结果里很多是聚合站/导航页/官网介绍，不是新闻本身。"
            "请整理出**今天真正的热点**："
            "- 挑 5-10 条有实质内容的，每条一行，格式「- 事件名：一句话要点」"
            "- 忽略网站导航、聚合页介绍、广告、无实义条目"
            "- 不要网址、不要来源网站名"
            "- 热榜词条只有标题没有详情时，保留词条本身即可，**不要编造细节**"
            "- 直接输出清单，不要开场白、不要附注说明。如果所有条目都没有实质内容，"
            "就输出「- 今天暂无可靠热点」")


def _UA():
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _fetch_weibo(timeout=12):
    """微博热搜（官方接口 weibo.com/ajax/side/hotSearch，公开 JSON）。"""
    import http.client
    conn = http.client.HTTPSConnection("weibo.com", 443, timeout=timeout)
    conn.request("GET", "/ajax/side/hotSearch", headers={
        "User-Agent": _UA(), "Accept": "application/json",
        "Referer": "https://weibo.com/hot/search"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("HTTP %d" % r.status)
    d = json.loads(data.decode("utf-8", "replace"))
    items = ((d.get("data") or {}).get("realtime")) or []
    out = []
    for it in items[:15]:
        w = str(it.get("word") or it.get("note") or "").strip()
        if w:
            out.append(w)
    return out


def _fetch_baidu(timeout=12):
    """百度热搜（top.baidu.com/board 页面内嵌 JSON）。"""
    import http.client
    import re as _re
    conn = http.client.HTTPSConnection("top.baidu.com", 443, timeout=timeout)
    conn.request("GET", "/board?tab=realtime", headers={"User-Agent": _UA()})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("HTTP %d" % r.status)
    html = data.decode("utf-8", "replace")
    m = _re.search(r"<!--s-data:(.*?)-->", html, _re.S)
    if not m:
        raise RuntimeError("页面结构变了（没找到内嵌数据）")
    d = json.loads(m.group(1))
    cards = ((d.get("data") or {}).get("cards")) or []
    out = []
    for card in cards:
        for it in (card.get("content") or [])[:15]:
            w = str(it.get("word") or "").strip()
            desc = _re.sub(r"\s+", " ", str(it.get("desc") or "")).strip()
            if w:
                out.append(w + ("：" + desc[:50] if desc else ""))
        if out:
            break
    return out


def _refine(api, raw_text):
    """LLM 提炼：把榜单/搜索结果洗成真热点清单；失败原样回退（绝不因提炼挂掉丢速览）。
    提炼后再过一道机械兜底：带网址/导航站/广告词的行丢弃（防模型偷懒照抄）。
    模型偶发 429/超时会静默失败——重试一次并把原因写进插件日志。"""
    import re as _re
    import time as _t
    try:
        import qq_bot_agent as A
    except Exception as e:
        if api:
            api.log("refine unavailable %r" % e)
        return raw_text
    msgs = [{"role": "system", "content": _SUM_SYS},
            {"role": "user", "content": str(raw_text)[:4000]}]
    junk = _re.compile(
        r"(?:https?://|www\.|[\w\-]+\.(?:com|cn|net|today|org)(?![\w])"
        r"|聚合|导航|热榜榜|榜单聚合|一网打尽|注册|促销|优惠|下载|"
        r"通用信息平台|致力于连接|实时榜|当前排名)")
    for attempt in (1, 2):
        lines = []
        try:
            _m, reply = A.call_glm_with_fallback(msgs, "")
            reply = str(reply or "").strip()
            lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
            lines = [ln for ln in lines if ln.startswith("-") or ln.startswith("•")]
            lines = [ln for ln in lines if not junk.search(ln)]
            if len(lines) >= 3:
                return "\n".join(lines)
            if api:
                api.log("refine attempt %d: only %d clean lines, retry" % (attempt, len(lines)))
        except Exception as e:
            if api:
                api.log("refine attempt %d fail %r" % (attempt, e))
        _t.sleep(2)
    return raw_text


def _refresh(api):
    try:
        raw, _errs = _collect(api)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    if not raw or "搜索失败" in raw[:20]:
        return {"ok": False, "error": "没有可用的热点来源"}
    text = _refine(api, raw)
    today = time.strftime("%Y-%m-%d")
    rec = {"date": today, "text": text[:_MAX + 200], "ts": time.time()}
    try:
        pathlib = __import__("pathlib")
        pathlib.Path(BRIEF_PATH).write_text(json.dumps(rec, ensure_ascii=False),
                                            encoding="utf-8")
        os.makedirs(HIST_DIR, exist_ok=True)
        pathlib.Path(os.path.join(HIST_DIR, today + ".json")).write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        api.log("brief ok %d chars" % len(text))
        _clean_history(api)      # 顺手清一次过期记录
        return {"ok": True, "date": today, "text": rec["text"],
                "refined": text is not raw}
    except OSError as e:
        return {"ok": False, "error": repr(e)}


def _history_list():
    out = []
    if os.path.isdir(HIST_DIR):
        for f in sorted(os.listdir(HIST_DIR), reverse=True):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(HIST_DIR, f), "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({"date": d.get("date") or f[:-5],
                            "preview": str(d.get("text") or "")[:60]})
            except (OSError, ValueError):
                continue
    return {"ok": True, "days": out[:60]}


def _get_day(date):
    if not date or "/" in date or "\\" in date or ".." in date:
        return {"ok": False, "error": "日期非法"}
    p = os.path.join(HIST_DIR, date + ".json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return {"ok": True, "record": json.load(f)}
    except (OSError, ValueError):
        return {"ok": False, "error": "该日无记录"}


def _delete_day(date):
    if not date or "/" in date or "\\" in date or ".." in date:
        return {"ok": False, "error": "日期非法"}
    p = os.path.join(HIST_DIR, date + ".json")
    try:
        os.remove(p)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": repr(e)}


def _set_query(api, query):
    q = str(query or "").strip()[:200]
    api.store("query", q)      # 空串=恢复默认关键词
    return {"ok": True, "query": q}


def _set_auto(api, enabled):
    api.store("auto_enabled", bool(enabled))
    return {"ok": True, "auto_enabled": bool(enabled)}


def _set_auto_time(api, hh, mm):
    try:
        hh_i = max(0, min(23, int(hh)))
        mm_i = max(0, min(59, int(mm)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "时间格式非法"}
    api.store("auto_hh", hh_i)
    api.store("auto_mm", mm_i)
    return {"ok": True, "hh": hh_i, "mm": mm_i}


def _line(chat_type, chat_id):
    try:
        with open(BRIEF_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict) or str(d.get("date") or "") != time.strftime("%Y-%m-%d"):
        return ""
    text = str(d.get("text") or "").strip()
    if not text:
        return ""
    return ("\n\n## 今日热点速览（今天 %s 的新闻/热搜摘要，聊到相关话题时可自然引用，"
            "不确定的细节不要编）\n- %s" % (d.get("date"), text[:_MAX]))
