# -*- coding: utf-8 -*-
"""群管模块冒烟：定时禁言tick（跨午夜/同日/状态防重）+关键词撤回禁言钩子。"""
import datetime
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
# 时钟要同时伪造 localtime 和 strftime：被测函数两样都用，而 time.strftime 不走
# Python 层替换过的 time.localtime（只替换后者时函数读到的仍是真实时间——旧版
# 脚本因此只有深夜运行才可能通过）。群管配置只驻内存（签名恒定），不会去读
# 本机真实的 group_admin.json（旧版脚本假定该文件不存在，文件存在即全部失效）。
_REAL_STRFTIME, _REAL_LOCALTIME, _REAL_FILE_SIG = time.strftime, time.localtime, D.file_sig


class Clock(object):
    dt = datetime.datetime(2026, 11, 24, 23, 5)


def fake_strftime(fmt="", *a):
    dt = Clock.dt
    if fmt == "%Y-%m-%d":
        return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)
    if fmt == "%H:%M":
        return "%02d:%02d" % (dt.hour, dt.minute)
    # 其余格式交给真实实现（不能借 datetime.strftime——它内部会回调 time.strftime）
    return _REAL_STRFTIME(fmt, *((a or (dt.timetuple(),))))


SIG = ("smoke", "gadmin")
time.strftime = fake_strftime
time.localtime = lambda *a, **k: Clock.dt.timetuple()
D.file_sig = lambda p: SIG


def set_sched(mute, unmute, state=None, on=True):
    d._ga_cfg = {"groups": {"602323545": {
        "sched_ban": {"on": on, "mute_hhmm": mute, "unmute_hhmm": unmute},
        "_state": dict(state or {})}}}
    d._ga_sig = SIG


def tick(*a):
    """把时钟拨到指定时刻并跑一次 tick，返回这一轮的全群禁言动作（True=禁 False=解）。"""
    Clock.dt = datetime.datetime(*a)
    del calls[:]
    d._sched_ban_tick()
    return [c[1].get("enable") for c in calls if c[0] == "set_group_whole_ban"]


def state():
    return d._ga_cfg["groups"]["602323545"]["_state"]


try:
    # 跨午夜窗口 23:00 → 08:00
    set_sched("23:00", "08:00")
    chk("1 跨午夜 23:05 → 禁言", tick(2026, 11, 24, 23, 5) == [True], calls)
    chk("1b 状态记 muted_date", state().get("muted_date") == "2026-11-24"
        and "unmuted_date" not in state(), state())
    chk("2 同日 23:59 再 tick 不重复", tick(2026, 11, 24, 23, 59) == [], calls)
    chk("3a 次日 00:10 → 保持禁言（过零点不解除）", tick(2026, 11, 25, 0, 10) == [], calls)
    chk("3b 次日 07:59 → 不到点，保持禁言", tick(2026, 11, 25, 7, 59) == [], calls)
    chk("3c 次日 08:00 → 解禁", tick(2026, 11, 25, 8, 0) == [False], calls)
    chk("3d 解禁记 unmuted_date，之后不重复",
        state().get("unmuted_date") == "2026-11-25" and tick(2026, 11, 25, 12, 0) == [],
        state())
    chk("3e 次日 23:00 → 再次禁言", tick(2026, 11, 25, 23, 0) == [True], calls)

    # 同日窗口 12:00 → 13:00
    set_sched("12:00", "13:00")
    chk("4a 同日窗口 12:30 → 禁言", tick(2026, 11, 24, 12, 30) == [True], calls)
    chk("4b 同日窗口 13:00 → 解禁", tick(2026, 11, 24, 13, 0) == [False], calls)
    chk("4c 同日窗口 13:30 再 tick 不重复", tick(2026, 11, 24, 13, 30) == [], calls)

    # 开关关着不动
    set_sched("23:00", "08:00", on=True)
    tick(2026, 11, 24, 23, 5)
    set_sched("23:00", "08:00", on=False)
    chk("5 开关关不动", tick(2026, 11, 25, 8, 0) == [], calls)
finally:
    time.strftime, time.localtime = _REAL_STRFTIME, _REAL_LOCALTIME
    Clock.dt = datetime.datetime.now()

# ---------- 关键词撤回/禁言 ----------
# 群管配置仍是内存态（file_sig 保持常量，函数才会读这份配置）
set_sched("23:00", "08:00")
# ⑥ 命中撤回词 → delete_msg（异步线程，等一拍）
del calls[:]
d._ga_cfg["groups"]["602323545"].update({
    "kw_recall_on": True, "kw_recall": ["博彩", "刷单"],
    "kw_mute_on": True, "kw_mute_words": ["傻逼"], "kw_mute_min": 10})
info = {"sender_id": 123456, "text": "来玩博彩网址xxx"}
d._gadmin_keyword_check("group", "602323545", info, "mid99")
time.sleep(0.6)
acts = dict((c[0], c[1]) for c in calls)
chk("6 撤回命中", calls and any(c[0] == "delete_msg" and c[1]["message_id"] == "mid99"
                                for c in calls), calls[:2])

# ⑦ 命中禁言词 → set_group_ban duration=10min
del calls[:]
info2 = {"sender_id": 999, "text": "你是傻逼吗"}
d._gadmin_keyword_check("group", "602323545", info2, "mid100")
time.sleep(0.6)
acts = [c for c in calls if c[0] == "set_group_ban"]
chk("7 禁言命中600s", acts and acts[0][1]["duration"] == 600
    and acts[0][1]["user_id"] == 999, acts)

# ⑧ 机器人自己不查
del calls[:]
d._gadmin_keyword_check("group", "602323545",
                        {"sender_id": 3126940548, "text": "博彩"}, "mid101")
time.sleep(0.4)
chk("8 bot自身不查", not calls)

# ⑨ 开关关不动作
del calls[:]
d._ga_cfg["groups"]["602323545"]["kw_recall_on"] = False
d._gadmin_keyword_check("group", "602323545",
                        {"sender_id": 111, "text": "博彩"}, "mid102")
time.sleep(0.4)
chk("9 撤回开关关不撤", not calls)

# ⑩ 私聊不查
del calls[:]
d._gadmin_keyword_check("private", "602323545",
                        {"sender_id": 111, "text": "博彩"}, "mid103")
time.sleep(0.4)
chk("10 私聊不查", not calls)
D.file_sig = _REAL_FILE_SIG

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
