# -*- coding: utf-8 -*-
"""QQ 在线状态管理插件：上下线自动设置 / 定时状态 / 手动切换——三个能力
各自独立开关。取代原内置「QQ 在线状态」卡（daemon 的登录→在线、退出→
离开钩子保留，由 app_config.qq_status_auto 驱动，本插件的「上下线自动」
开关直接管那个键）。"""

import json
import os
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
APP_CFG_PATH = os.path.join(DATA_DIR, "app_config.json")

# 状态清单（id/label/是否扩展状态）——与 daemon QQ_STATUS_KINDS 对应
STATUSES = [
    ("online",    "在线"), ("away", "离开"), ("busy", "忙碌"),
    ("dnd", "请勿打扰"), ("qme", "Q我吧"), ("invisible", "隐身"),
    ("sleep", "睡觉中"), ("study", "学习中"), ("work", "搬砖中"),
    ("fish", "摸鱼中"), ("song", "听歌中"), ("drama", "追剧中"),
    ("stayup", "熬夜中"), ("emo", "emo中"), ("love", "恋爱中"),
    ("boring", "无聊中"), ("energy", "元气满满"), ("lucky", "好运锦鲤"),
]
_KINDS = {k for k, _ in STATUSES}


def register(api):
    api.route("load_cfg", lambda args: _load_cfg(api))
    api.route("save_cfg", lambda args: _save_cfg(api, args or {}))
    api.route("set_now", lambda args: _set_now(api, (args or {}).get("kind")))
    api.route("statuses", lambda args: {"ok": True,
                                        "statuses": [{"id": k, "label": v}
                                                     for k, v in STATUSES]})
    api.on_tick(api.every(1, lambda d: _tick(api)))
    api.log("ready")


def _cfg(api):
    c = api.load("cfg", None)
    if not isinstance(c, dict):
        c = {}
    c.setdefault("auto_enabled", False)     # 上下线自动（登录→在线 停止→离开）
    c.setdefault("schedule_enabled", False)  # 定时状态
    c.setdefault("rules", [])               # [{"t":"23:30","kind":"sleep"}]
    c.setdefault("manual_enabled", True)    # 手动切换
    return c


def _read_app_cfg():
    try:
        with open(APP_CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_app_cfg_key(key, val):
    d = _read_app_cfg()
    d[key] = val
    import pathlib
    p = pathlib.Path(APP_CFG_PATH + ".tmp")
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(p), APP_CFG_PATH)


def _load_cfg(api):
    c = _cfg(api)
    # 上下线自动的真值在 app_config.qq_status_auto（daemon 直读它）
    c["auto_enabled"] = bool(_read_app_cfg().get("qq_status_auto", False))
    host_daemon = getattr(api._host, "daemon", None)
    return {"ok": True, "cfg": c,
            "statuses": [{"id": k, "label": v} for k, v in STATUSES],
            "host_ok": bool(host_daemon and hasattr(host_daemon, "set_qq_status"))}


def _save_cfg(api, args):
    c = _cfg(api)
    c["auto_enabled"] = bool(args.get("auto_enabled"))
    c["schedule_enabled"] = bool(args.get("schedule_enabled"))
    c["manual_enabled"] = bool(args.get("manual_enabled", True))
    rules = []
    for r in (args.get("rules") or [])[:20]:
        if not isinstance(r, dict):
            continue
        t = str(r.get("t") or "").strip()
        kind = str(r.get("kind") or "").strip()
        ok_t = (len(t) == 5 and t[:2].isdigit() and t[3:].isdigit()
                and 0 <= int(t[:2]) <= 23 and 0 <= int(t[3:]) <= 59)
        if ok_t and kind in _KINDS:
            rules.append({"t": t, "kind": kind})
    c["rules"] = rules
    api.store("cfg", c)
    # 上下线自动同步给 daemon（qq_status_auto）
    try:
        _write_app_cfg_key("qq_status_auto", c["auto_enabled"])
    except OSError as e:
        return {"ok": False, "error": "同步 app_config 失败 %r" % e}
    return {"ok": True, "cfg": c}


def _set_now(api, kind):
    """手动立即切换状态。"""
    c = _cfg(api)
    if not c.get("manual_enabled"):
        return {"ok": False, "error": "「手动切换」开关未开启"}
    kind = str(kind or "").strip()
    if kind not in _KINDS:
        return {"ok": False, "error": "状态类型非法"}
    r = api.set_qq_status(kind)
    if r.get("ok"):
        api.store("last_manual", {"kind": kind, "ts": time.time()})
    return r


def _tick(api):
    """每分钟：定时状态到点执行（每条规则每天最多一次）。"""
    c = _cfg(api)
    if not c.get("schedule_enabled"):
        return
    lt = time.localtime()
    now_hm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
    today = time.strftime("%Y-%m-%d")
    fired = api.load("fired", {})
    if not isinstance(fired, dict) or fired.get("date") != today:
        fired = {"date": today, "times": []}
    for r in c.get("rules", []):
        t = str(r.get("t") or "")
        if t != now_hm or t in fired.get("times", []):
            continue
        rr = api.set_qq_status(str(r.get("kind") or ""))
        fired.setdefault("times", []).append(t)
        api.log("schedule fired %s -> %s ok=%s" % (t, r.get("kind"), rr.get("ok")))
        break   # 每分钟最多执行一条
    api.store("fired", fired)
