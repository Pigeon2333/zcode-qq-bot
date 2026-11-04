# -*- coding: utf-8 -*-
"""每日热点速览插件：定时（可关，默认开）/手动立刻总结当日热点；历史分天存档，
插件页可查看与删除。"""

import json
import os
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
BRIEF_PATH = os.path.join(DATA_DIR, "hot_brief.json")
HIST_DIR = os.path.join(DATA_DIR, "hot_history")
_MAX = 400


def register(api):
    if api.load("auto_enabled", True):
        api.on_tick(api.every(360, lambda d: _refresh(api)))
    api.route("refresh_now", lambda args: _refresh(api))
    api.route("history", lambda args: _history_list())
    api.route("get_day", lambda args: _get_day(str((args or {}).get("date") or "")))
    api.route("delete_day", lambda args: _delete_day(str((args or {}).get("date") or "")))
    api.route("set_auto", lambda args: _set_auto(api, bool((args or {}).get("enabled"))))
    api.route("load_auto", lambda args: {"ok": True,
                                         "auto_enabled": bool(api.load("auto_enabled", True))})
    api.inject_context(_line)
    api.log("ready")


def _search_text():
    try:
        import qq_bot_agent as A
        res, _backend = A.search_web("今日热点新闻 头条")
        return str(res or "").strip()
    except Exception as e:
        return "搜索失败：%r" % (e,)


def _refresh(api):
    text = _search_text()
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
        return {"ok": True, "date": today, "text": rec["text"]}
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


def _set_auto(api, enabled):
    api.store("auto_enabled", bool(enabled))
    return {"ok": True, "auto_enabled": bool(enabled)}


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
