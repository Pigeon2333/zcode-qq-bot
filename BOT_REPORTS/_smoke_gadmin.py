# -*- coding: utf-8 -*-
"""群管模块冒烟：定时禁言tick（跨午夜/同日/状态防重）+关键词撤回禁言钩子。"""
import os
import sys
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

fails = []
calls = []          # 捕获的 OneBot 动作


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:80] if extra else ""))
    if not cond:
        fails.append(name)


d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
d.login = {"user_id": 3126940548}
d._call_action = lambda action, params, timeout=None: (
    calls.append((action, params)) or {})
# 冒烟不落盘：_group_admin_save 改内存态（_ga_sig 同步为当前内存签名语义）
d._group_admin_save = lambda data: setattr(d, "_ga_cfg", data)

# ---------- 定时禁言 ----------
_REAL_LT = time.localtime


def set_now(hh, mm):
    t = _REAL_LT()
    return time.struct_time((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0,
                             t.tm_wday, t.tm_yday, 0))

today = time.strftime("%Y-%m-%d")

# ① 跨午夜窗口 23:00→08:00，当前 23:05 → 禁言
calls.clear()
d._ga_cfg = {"groups": {"602323545": {"sched_ban": {"on": True,
        "mute_hhmm": "23:00", "unmute_hhmm": "08:00"}, "_state": {}}}}
d._ga_sig = None   # 与 file_sig(不存在文件)=None 一致 → 保留内存配置不重载
_real_lt = time.localtime
time.localtime = lambda *a, **k: set_now(23, 5)
d._sched_ban_tick()
time.localtime = _real_lt
acts = [c for c in calls if c[0] == "set_group_whole_ban"]
chk("1 跨午夜23:05→禁言", acts and acts[0][1]["enable"] is True, acts)
state = d._ga_cfg["groups"]["602323545"]["_state"]
chk("1b 状态记 muted_date", state.get("muted_date") == today, state)

# ② 同日再 tick 不重复
calls.clear()
time.localtime = lambda *a, **k: set_now(23, 6)
d._sched_ban_tick()
time.localtime = _real_lt
chk("2 已禁过不重复", not [c for c in calls if c[0] == "set_group_whole_ban"])

# ③ 次日 07:00 → 解禁（unmuted_date 未记）
calls.clear()
d._ga_cfg["groups"]["602323545"]["_state"] = {
    "muted_date": today, "unmuted_date": time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - 86400))}
time.localtime = lambda *a, **k: set_now(7, 0)
d._sched_ban_tick()
time.localtime = _real_lt
acts = [c for c in calls if c[0] == "set_group_whole_ban"]
chk("3 早上7点→解禁", acts and acts[0][1]["enable"] is False, acts)

# ④ 同日窗口 12:00→13:00，当前 12:30 → 禁
calls.clear()
d._ga_cfg["groups"]["602323545"] = {"sched_ban": {"on": True,
        "mute_hhmm": "12:00", "unmute_hhmm": "13:00"}, "_state": {}}
time.localtime = lambda *a, **k: set_now(12, 30)
d._sched_ban_tick()
time.localtime = _real_lt
acts = [c for c in calls if c[0] == "set_group_whole_ban"]
chk("4 同日12:30→禁言", acts and acts[0][1]["enable"] is True, acts)

# ⑤ 未开开关不动
calls.clear()
d._ga_cfg["groups"]["602323545"]["sched_ban"]["on"] = False
time.localtime = lambda *a, **k: set_now(12, 30)
d._sched_ban_tick()
time.localtime = _real_lt
chk("5 开关关不动", not calls)

# ---------- 关键词撤回/禁言 ----------
# ⑥ 命中撤回词 → delete_msg（异步线程，等一拍）
calls.clear()
d._ga_cfg["groups"]["602323545"] = {
    "kw_recall_on": True, "kw_recall": ["博彩", "刷单"],
    "kw_mute_on": True, "kw_mute_words": ["傻逼"], "kw_mute_min": 10, "_state": {}}
info = {"sender_id": 123456, "text": "来玩博彩网址xxx"}
d._gadmin_keyword_check("group", "602323545", info, "mid99")
time.sleep(0.6)
acts = dict((c[0], c[1]) for c in calls)
chk("6 撤回命中", calls and any(c[0] == "delete_msg" and c[1]["message_id"] == "mid99"
                                for c in calls), calls[:2])

# ⑦ 命中禁言词 → set_group_ban duration=10min
calls.clear()
info2 = {"sender_id": 999, "text": "你是傻逼吗"}
d._gadmin_keyword_check("group", "602323545", info2, "mid100")
time.sleep(0.6)
acts = [c for c in calls if c[0] == "set_group_ban"]
chk("7 禁言命中600s", acts and acts[0][1]["duration"] == 600
    and acts[0][1]["user_id"] == 999, acts)

# ⑧ 机器人自己不查
calls.clear()
d._gadmin_keyword_check("group", "602323545",
                        {"sender_id": 3126940548, "text": "博彩"}, "mid101")
time.sleep(0.4)
chk("8 bot自身不查", not calls)

# ⑨ 开关关不动作
calls.clear()
d._ga_cfg["groups"]["602323545"]["kw_recall_on"] = False
d._gadmin_keyword_check("group", "602323545",
                        {"sender_id": 111, "text": "博彩"}, "mid102")
time.sleep(0.4)
chk("9 撤回开关关不撤", not calls)

# ⑩ 私聊不查
calls.clear()
d._gadmin_keyword_check("private", "602323545",
                        {"sender_id": 111, "text": "博彩"}, "mid103")
time.sleep(0.4)
chk("10 私聊不查", not calls)

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
