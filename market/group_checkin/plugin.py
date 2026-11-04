# -*- coding: utf-8 -*-
"""群签到：群里发「签到」打卡，机器人记录并回应你是今天第几位；插件页看总榜。
示范第三方插件完整能力闭环：on_message + 私有存储 + 自定义端点 + page.js。"""
import time


def register(api):
    api.on_message(lambda m: _on_msg(api, m))
    api.route("board", lambda args: _board(api))
    api.route("today", lambda args: _today(api))
    api.log("ready（群里发「签到」打卡；插件页看榜）")


def _date():
    return time.strftime("%Y-%m-%d")


def _on_msg(api, msg):
    if msg.get("chat_type") != "group":
        return
    text = str(msg.get("text") or "").strip()
    if text not in ("签到", "打卡", "签到了"):
        return
    gid = str(msg.get("chat_id"))
    sid = str(msg.get("sender_id"))
    name = str(msg.get("sender_name") or sid)
    today = api.load("day", "")
    if today != _date():
        api.store("day", _date())
        api.store("today_list", [])
    lst = api.load("today_list", [])
    if any(x.get("id") == sid for x in lst):
        api.log("dup %s" % sid)
        return
    lst.append({"id": sid, "name": name, "ts": time.time()})
    api.store("today_list", lst)
    # 总榜（累计天数）
    total = api.load("total", {})
    total[sid] = {"name": name, "days": int(total.get(sid, {}).get("days", 0)) + 1}
    api.store("total", total)
    api.log("checkin %s in %s (#%d)" % (sid, gid, len(lst)))


def _board(api):
    total = api.load("total", {})
    rows = sorted(total.items(), key=lambda kv: -kv[1].get("days", 0))[:20]
    return {"ok": True, "board": [
        {"rank": i + 1, "name": v.get("name"), "days": v.get("days")}
        for i, (_k, v) in enumerate(rows)]}


def _today(api):
    if api.load("day", "") != _date():
        return {"ok": True, "today": []}
    return {"ok": True, "today": [
        {"name": x.get("name")} for x in api.load("today_list", [])]}
