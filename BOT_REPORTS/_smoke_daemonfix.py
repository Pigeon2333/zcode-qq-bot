# -*- coding: utf-8 -*-
"""离线冒烟：daemon 两处修复（2026-11-25 审计 S20 + L7）。

S20 群白名单：空列表不再全放行；文件坏了沿用上一份（stale）/保守闭嘴（broken）；
     /status 新增 wl_state / wl_error。
L7 WS 接收线程兜底：分发/解析异常不再带崩接收线程（WS_RX_ERROR）；
     巡检发现线程死了自动重拉（WS_RX_RESTART）。

不连 ws、不启动机器人；白名单文件打桩到系统临时目录，不碰 ~/.zcode-qq-bot。
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

fails = []
n_pass = 0
LOGS = []


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:100] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


# 日志捕获（WL_FILE_BAD / WS_RX_ERROR / WS_RX_RESTART 都走 D.out）
D.out = lambda line: LOGS.append(line)


def logs_of(msg):
    return [l for l in LOGS if msg in l]


# 白名单文件打桩到系统临时目录（安全条款：不读写真实数据目录）
WL_DIR = tempfile.mkdtemp(prefix="smoke_wl_")
WL_PATH = os.path.join(WL_DIR, "group_whitelist.json")
D.GROUP_WHITELIST_PATH = WL_PATH
_PAD = [0]   # 每次写入追加不同数量的换行，保证 mtime+size 签名必变（热加载才生效）


def write_wl(text):
    _PAD[0] += 1
    pathlib.Path(WL_PATH).write_text(text + ("\n" * _PAD[0]), encoding="utf-8")


def rm_wl():
    try:
        os.remove(WL_PATH)
    except OSError:
        pass


def fresh(cfg=None):
    """新 Daemon 实例：白名单缓存/上一份好内容/坏签名全部归零。"""
    return D.Daemon(ws_url="", token="", cfg=cfg or {}, ctl_port=0)


def st(d):
    return d.status()


# ================= S20 群白名单 =================

# 1 文件不存在，没有 --groups → 所有群放行；wl_state=default_all
rm_wl()
d = fresh()
chk("S20-1a 无文件无参数所有群放行", d._group_allowed("111") and d._group_allowed("222"))
s = st(d)
chk("S20-1b wl_state=default_all", s.get("wl_state") == "default_all", s.get("wl_state"))
chk("S20-1c wl_error 空", s.get("wl_error") == "", repr(s.get("wl_error")))

# 2 文件不存在，--groups=A,B → 只对 A、B 放行；wl_state=default_groups
d = fresh(cfg={"group_whitelist": ["A", "B"]})
chk("S20-2a 只对 A/B 放行", d._group_allowed("A") and d._group_allowed("B")
    and not d._group_allowed("C"))
s = st(d)
chk("S20-2b wl_state=default_groups", s.get("wl_state") == "default_groups", s.get("wl_state"))

# 3 文件存在 allow_all=true → 所有群放行；wl_state=file
write_wl(json.dumps({"enabled": True, "allow_all": True, "groups": []}))
d = fresh()
chk("S20-3a allow_all 放行任意群", d._group_allowed("X1") and d._group_allowed("X2"))
chk("S20-3b wl_state=file", st(d).get("wl_state") == "file", st(d).get("wl_state"))

# 4 allow_all=false，groups=[] → 任何群都不放行（旧实现空列表=全放行）
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": []}))
d = fresh()
chk("S20-4 空名单闭嘴", not d._group_allowed("anything"))

# 5 allow_all=false，groups=[A] → 只对 A 放行
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": ["A"]}))
d = fresh()
chk("S20-5 名单内放行名单外闭嘴", d._group_allowed("A") and not d._group_allowed("B"))

# 6 enabled=false → 任何群都不放行；wl_state=disabled
write_wl(json.dumps({"enabled": False, "allow_all": True, "groups": ["A"]}))
d = fresh()
chk("S20-6a 总开关关不放行", not d._group_allowed("A"))
chk("S20-6b wl_state=disabled", st(d).get("wl_state") == "disabled", st(d).get("wl_state"))

# 7 先读到合法内容，之后文件变成非法 JSON → 沿用上一份；wl_state=stale；
#   WL_FILE_BAD 每个坏签名只打一次（换一个坏签名再打一次）
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": ["A"]}))
d = fresh()
d._group_allowed("A")           # 触发首次加载（合法）
del LOGS[:]
write_wl("{这不是合法JSON")
chk("S20-7a 坏了沿用上一份", d._group_allowed("A") and not d._group_allowed("B"))
s = st(d)
chk("S20-7b wl_state=stale", s.get("wl_state") == "stale", s.get("wl_state"))
chk("S20-7c wl_error 非空", bool(s.get("wl_error")), repr(s.get("wl_error")))
n1 = len(logs_of("WL_FILE_BAD"))
d._group_allowed("A")           # 同一签名再来一条消息
d._group_allowed("A")
n2 = len(logs_of("WL_FILE_BAD"))
chk("S20-7d 同签名只报一次 (%d→%d)" % (n1, n2), n1 == 1 and n2 == 1, "%d,%d" % (n1, n2))
write_wl("{{{还是坏的")          # 换个内容 = 新签名
d._group_allowed("A")
chk("S20-7e 新签名再报一次", len(logs_of("WL_FILE_BAD")) == 2,
    len(logs_of("WL_FILE_BAD")))

# 8 启动后第一份就是非法 JSON → 不放行任何群；wl_state=broken
rm_wl()
d = fresh()                      # 先建实例再落坏文件 = 「启动后第一份就读不出」
write_wl("{{第一份就是坏的")
chk("S20-8a broken 全闭嘴", not d._group_allowed("A") and not d._group_allowed("B"))
s = st(d)
chk("S20-8b wl_state=broken", s.get("wl_state") == "broken", s.get("wl_state"))
chk("S20-8c wl_error 非空", bool(s.get("wl_error")), repr(s.get("wl_error")))
chk("S20-8d broken 也报 WL_FILE_BAD", len(logs_of("WL_FILE_BAD")) >= 1)

# 9 文件修好 → 立刻用新内容；wl_state=file（秒级热加载不变）
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": ["FIXED"]}))
chk("S20-9a 修好即生效", d._group_allowed("FIXED") and not d._group_allowed("A"))
s = st(d)
chk("S20-9b wl_state 回到 file", s.get("wl_state") == "file", s.get("wl_state"))
chk("S20-9c wl_error 清空", s.get("wl_error") == "", repr(s.get("wl_error")))

# 10 黑名单：即使 allow_all=true 也不放行（现有行为不回归）
write_wl(json.dumps({"enabled": True, "allow_all": True, "groups": []}))
d = fresh(cfg={"group_blacklist": ["BAD"]})
chk("S20-10 黑名单压过 allow_all", not d._group_allowed("BAD") and d._group_allowed("OK"))

# 11 /status 老字段一个不少
s = st(fresh())
for k in ("running", "pid", "uptime", "ws_url", "ws_connected", "ws_last_ok",
          "ws_alive", "login", "inbox_pending", "stats", "data_dir"):
    if k not in s:
        chk("S20-11 /status 保留老字段 %s" % k, False, "缺 %s" % k)
        break
else:
    chk("S20-11 /status 老字段齐全", True)

# ================= L7 WS 接收线程兜底 =================

RACY = json.dumps({"echo": "eRACE", "retcode": 0})
NORMAL = json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat"})


def race_dict():
    """回包表替身：in 检查永远通过、取值必炸——精确复刻超时清理与回包到达撞车。"""
    class _Racy(dict):
        def __contains__(self, k):
            return True

        def __getitem__(self, k):
            raise KeyError(k)
    return _Racy()


def drive(ws, frames):
    """同步驱动 run_forever：逐帧投喂，帧投完模拟连接关闭。"""
    ws.connect = lambda: None
    step = {"i": 0}

    def fake_recv():
        i = step["i"]
        step["i"] += 1
        if i < len(frames):
            return frames[i]
        ws._stop.set()
        raise ConnectionError("closed")
    ws.recv_message = fake_recv
    ws.run_forever()


# 12 回包表查询抛异常 → 只记一条 WS_RX_ERROR，循环继续吃下一帧
d = fresh()
d.pending_echoes = race_dict()
seen = []
_orig = d._dispatch_ws_event


def spy(ev):
    seen.append(ev)
    return _orig(ev)


d.ws.on_event = spy
del LOGS[:]
drive(d.ws, [RACY, NORMAL])
chk("L7-12a 循环继续吃下一帧", len(seen) == 2, len(seen))
errs = logs_of("WS_RX_ERROR")
chk("L7-12b 只记一条 WS_RX_ERROR", len(errs) == 1, errs)
chk("L7-12c detail 带 repr 且截 120 字",
    bool(errs) and "KeyError" in errs[0]
    and len(errs[0].split('"detail": ', 1)[1].rstrip("}")) <= 125, errs[:1])

# 13 连接正常关闭 → 按现有逻辑重连，不记 WS_RX_ERROR
d = fresh()
del LOGS[:]
connects = []


def con():
    connects.append(1)
    if len(connects) >= 2:       # 第二次连接时置停并断开 → 走完一轮既有重连后退出
        d.ws._stop.set()
        raise ConnectionError("closed-for-test")


def recv_closed():
    raise ConnectionError("closed")
d.ws.connect = con
d.ws.recv_message = recv_closed
d.ws.run_forever()
chk("L7-13 正常断线重连不报 WS_RX_ERROR",
    len(connects) == 2 and not logs_of("WS_RX_ERROR"),
    "%d,%s" % (len(connects), logs_of("WS_RX_ERROR")))


# 14 「该不该重拉」判定小函数（可单测）
class _FakeWS:
    """替身 ws：run_forever 只是一直等停，便于验证重拉出的线程真的活着。"""

    def __init__(self):
        self._stop = threading.Event()
        self.connected = False
        self.url = "ws://fake"

    def run_forever(self):
        self._stop.wait(10)

    def close(self):
        self._stop.set()


d = fresh()
d.ws = _FakeWS()
dead = threading.Thread(target=lambda: None)
dead.start()
dead.join()
d._ws_recv_thread = None
chk("L7-14a 未启动过不判死", d._ws_recv_needs_restart() is False)
d._ws_recv_thread = dead
chk("L7-14b 线程死了要重拉", d._ws_recv_needs_restart() is True)
alive = threading.Thread(target=d.ws.run_forever, daemon=True)
alive.start()
d._ws_recv_thread = alive
chk("L7-14c 线程活着不重拉", d._ws_recv_needs_restart() is False)
d._ws_recv_thread = dead
d.stop_flag.set()
chk("L7-14d daemon 在停不重拉", d._ws_recv_needs_restart() is False)
d.stop_flag.clear()

# 15 巡检兜底：线程死了 → 重拉并记 WS_RX_RESTART；daemon 在停 → 不动
del LOGS[:]
d._ws_recv_thread = dead
d._ws_recv_watchdog()
new_t = d._ws_recv_thread
chk("L7-15a 重拉出新线程", new_t is not dead and isinstance(new_t, threading.Thread)
    and new_t.is_alive())
chk("L7-15b 记 WS_RX_RESTART", len(logs_of("WS_RX_RESTART")) == 1, logs_of("WS_RX_RESTART"))
d.stop_flag.set()
old_t = d._ws_recv_thread
d._ws_recv_watchdog()
chk("L7-15c daemon 在停巡检不动", d._ws_recv_thread is old_t
    and len(logs_of("WS_RX_RESTART")) == 1)

# 收尾：放掉重拉出的常驻等待线程
d.ws.close()
new_t.join(5)
chk("L7-16 收尾重拉线程已退出", not new_t.is_alive())

# ================= 第 2 组：图文合并开关 / 连发差一 / 群管人名歧义 / 出站剥标日志 =================
# （2026-11-25 施工单 A：text_img_merge_enabled 开关、_is_bursting 差一、
#   _resolve_member 歧义防误伤、_strip_control_marks 只加 SEND_MARK_STRIP 日志）

write_wl(json.dumps({"enabled": True, "allow_all": True, "groups": []}))
_REAL_SLEEP = time.sleep


class _SleepGate(object):
    """time.sleep 替身：记录调用并在第一次调用时挂起，等测试放行——
    延迟入箱线程停在 2.5s 上，主线程先喂后续事件再放行，消除竞态。"""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self, s):
        self.calls.append(s)
        self.entered.set()
        self.gate.wait(5)


def fresh2(rc):
    """第 2 组专用实例：reply_config 驻内存（不读真实配置）、群管关键词检查
    与触发判定打桩（触发恒 True/keyword，绕开真实触发逻辑）、黑名单置空。"""
    d = fresh()
    d.login = {"user_id": 10000}
    d._rc = dict(rc)
    d._reply_config = lambda: d._rc
    d._gadmin_keyword_check = lambda *a, **k: None
    d._decide_trigger = lambda chat_type, info, chat_id="": (True, "keyword")
    d._user_blacklist = lambda: set()
    return d


def ingest_text(d, ts, mid, text="帮我搞定这几个人"):
    """纯文字条走 _ingest_message（图文合并延迟判定就在这个函数里）。"""
    info = D.extract_message_info([], 10000, ("group", "G2"))
    info["text"] = text
    d._ingest_message({"time": ts, "message_id": mid},
                      "group", "G2", "测试群", "u1", "用户一", info)


def ingest_img_event(d, ts, mid, url="http://example/1.jpg"):
    """图走外层入口：_nearby_img 缓存与 _fwd_merge_hold 都在外层打点。"""
    d._handle_message_event({
        "time": ts, "message_id": mid, "message_type": "group",
        "group_id": "G2", "user_id": "u1", "sender": {"card": "用户一"},
        "message": [{"type": "image", "data": {"url": url}}],
    })


def wait_inbox(d, n, timeout=5):
    dl = time.monotonic() + timeout
    while time.monotonic() < dl:
        if len(d.inbox) >= n:
            return True
        _REAL_SLEEP(0.01)
    return len(d.inbox) >= n


# ---- 2-1 开关缺键（视为 True）：触发纯文字条仍走 2.5s 延迟，随后到的图并入 ----
gate = _SleepGate()
time.sleep = gate
try:
    d = fresh2({})
    ingest_text(d, 1000000, "m1")
    gate.entered.wait(2)
    chk("2-1a 缺键仍走 2.5s 延迟且未入箱", gate.calls == [2.5]
        and len(d.inbox) == 0 and list(d._fwd_pending) == ["group_G2_u1"],
        "%s,%s" % (gate.calls, dict(d._fwd_pending)))
    ingest_img_event(d, 1000001, "m2")
    chk("2-1b 等待期到的图被压触发、先自入箱", len(d.inbox) == 1
        and d.inbox[0]["triggered"] is False and d.inbox[0]["text"] == "[图片]",
        [(i["triggered"], i["text"]) for i in d.inbox])
    gate.gate.set()
    chk("2-1c 延迟条并图入箱", wait_inbox(d, 2)
        and d.inbox[1]["triggered"] is True
        and d.inbox[1]["text"] == "帮我搞定这几个人"
        and d.inbox[1]["image_urls"] == ["http://example/1.jpg"]
        and d.inbox[1]["merged_nearby_image"] is True,
        [(i["triggered"], i["text"], i["image_urls"]) for i in d.inbox])
    chk("2-1d pending 已清空", not d._fwd_pending, dict(d._fwd_pending))
finally:
    time.sleep = _REAL_SLEEP
    gate.gate.set()

# ---- 2-2 开关 False：不延迟直接入箱，随后到的图各自处理不并入 ----
gate = _SleepGate()
time.sleep = gate
try:
    d = fresh2({"text_img_merge_enabled": False})
    ingest_text(d, 2000000, "n1")
    chk("2-2a 关时不走延迟、立即入箱", gate.calls == [] and len(d.inbox) == 1
        and d.inbox[0]["triggered"] is True
        and d.inbox[0]["text"] == "帮我搞定这几个人"
        and d.inbox[0]["image_urls"] == [],
        "%s,%s" % (gate.calls, [(i["triggered"], i["text"], i["image_urls"])
                                for i in d.inbox]))
    chk("2-2b pending 不登记", not d._fwd_pending, dict(d._fwd_pending))
    ingest_img_event(d, 2000001, "n2")
    chk("2-2c 随后到的图照常触发、不并入文字条", wait_inbox(d, 2)
        and d.inbox[1]["triggered"] is True
        and d.inbox[1]["image_urls"] == ["http://example/1.jpg"]
        and d.inbox[0]["image_urls"] == []
        and d.inbox[0]["merged_nearby_image"] is False,
        [(i["triggered"], i["text"], i["image_urls"]) for i in d.inbox])
finally:
    time.sleep = _REAL_SLEEP
    gate.gate.set()

# ---- 2-3 运行中开→关：已挂起的那条照旧走完 2.5s 并入，不丢不重 ----
cfg3 = {}
gate = _SleepGate()
time.sleep = gate
try:
    d = fresh2(cfg3)
    ingest_text(d, 3000000, "p1")
    gate.entered.wait(2)
    cfg3["text_img_merge_enabled"] = False     # 运行中切到关
    ingest_img_event(d, 3000001, "p2")         # pending 还在 → 图仍被压
    gate.gate.set()
    chk("2-3a 已挂起条照旧延迟后并入", wait_inbox(d, 2)
        and gate.calls == [2.5], gate.calls)
    texts = [i for i in d.inbox if i["text"] == "帮我搞定这几个人"]
    chk("2-3b 不丢不重：触发文字条恰好一条且并上图", len(texts) == 1
        and texts[0]["triggered"] is True
        and texts[0]["image_urls"] == ["http://example/1.jpg"]
        and texts[0]["merged_nearby_image"] is True,
        [(i["text"], i["triggered"], i["image_urls"]) for i in d.inbox])
    chk("2-3c 收件箱恰两条、图条未双触发", len(d.inbox) == 2
        and d.inbox[0]["triggered"] is False,
        [(i["triggered"], i["text"]) for i in d.inbox])
finally:
    time.sleep = _REAL_SLEEP
    gate.gate.set()

# ---- 2-4 连发差一：历史 1 条同人 + 间隔 < gap 也算连发 ----
d = fresh2({"burst_merge_enabled": True})
now = time.time()


def burst(ages_senders):
    """按 (距今年龄秒, 发送者) 填历史后跑 _is_bursting（判定发生在当前消息
    入史之前——hist 就是「当前这条到达时」的历史）。"""
    key = "group_GB"
    d.mem_history[key].clear()
    for age, sid in ages_senders:
        d.mem_history[key].append({"ts": now - age, "sender_id": sid,
                                   "is_bot": False, "text": "x"})
    return d._is_bursting("group", "GB", "u1")


chk("2-4a 同人两条间隔1秒：第2条到达即连发", burst([(1.0, "u1")]) is True)
chk("2-4b 间隔5秒不算连发", burst([(5.0, "u1")]) is False)
chk("2-4c 历史只有别人的消息不算", burst([(0.1, "u2")]) is False)
chk("2-4d 空历史不算", burst([]) is False)
chk("2-4e 混入他人消息仍按同人判", burst([(0.2, "u2"), (1.0, "u1")]) is True)
d2 = fresh2({"burst_merge_enabled": False})
d2.mem_history["group_GB"].append({"ts": now - 1.0, "sender_id": "u1",
                                   "is_bot": False, "text": "x"})
chk("2-4f burst 开关关照旧 False", d2._is_bursting("group", "GB", "u1") is False)
chk("2-4g 两条连发中(与改前一致)", burst([(2.0, "u1"), (0.5, "u1")]) is True)
chk("2-4h 两条但倒数第二条已远(与改前一致)",
    burst([(10.0, "u1"), (0.5, "u1")]) is False)
chk("2-4i 三条连发(与改前一致)",
    burst([(10.0, "u1"), (5.0, "u1"), (0.5, "u1")]) is True)
chk("2-4j 三条倒数第二条超窗(与改前一致)",
    burst([(10.0, "u1"), (9.5, "u1"), (0.5, "u1")]) is False)

# ---- 2-5 群管人名歧义：同档命中多个不同 QQ 不执行、列候选 ----
MEMBERS = [
    {"user_id": 111, "card": "阿明", "nickname": "ming"},
    {"user_id": 222, "card": "阿明明", "nickname": "mm"},
    {"user_id": 333, "card": "", "nickname": "小明"},
    {"user_id": 444, "card": "老王", "nickname": "小明"},
    {"user_id": 555, "card": "", "nickname": "阿凡达"},
    {"user_id": 666, "card": "大卫", "nickname": "david"},
    {"user_id": 777, "card": "大伟", "nickname": "wei"},
    {"user_id": 888, "card": "", "nickname": "大明白"},
    {"user_id": 901, "card": "列一", "nickname": "c1"},
    {"user_id": 902, "card": "列二", "nickname": "c2"},
    {"user_id": 903, "card": "列三", "nickname": "c3"},
    {"user_id": 904, "card": "列四", "nickname": "c4"},
    {"user_id": 905, "card": "列五", "nickname": "c5"},
    {"user_id": 906, "card": "列六", "nickname": "c6"},
]
gd = fresh2({})
gd.login = {"user_id": 10000}
calls = []


def fake_action(action, params, timeout=None):
    calls.append((action, params))
    if action == "get_group_member_list":
        return list(MEMBERS)
    return {}


gd._call_action = fake_action

r = gd._resolve_member("9207", "阿明明")
chk("2-5a 精确唯一照旧解析", r == ("222", "阿明明"), r)
r = gd._resolve_member("9207", "小明")
chk("2-5b 精确同名两人→歧义", r[0] is None and "不止一个人匹配" in r[1]
    and "小明" in r[1] and "老王" in r[1], r)
r = gd._resolve_member("9207", "大")
chk("2-5c 前缀命中多人→歧义", r[0] is None and "不止一个人匹配" in r[1]
    and "大卫" in r[1] and "大伟" in r[1] and "大明白" in r[1], r)
r = gd._resolve_member("9207", "大明")
chk("2-5d 前缀唯一照旧解析", r == ("888", "大明白"), r)
r = gd._resolve_member("9207", "凡达")
chk("2-5e 包含唯一照旧解析", r == ("555", "阿凡达"), r)
r = gd._resolve_member("9207", "列")
chk("2-5f 候选最多列 5 个", r[0] is None and r[1].count("、") == 4
    and "列五" in r[1] and "列六" not in r[1], r)
del calls[:]
r = gd._resolve_member("9207", "98765432")
chk("2-5g QQ号直用不查成员列表", r == ("98765432", "98765432") and not calls,
    (r, calls))
r = gd._resolve_member("9207", "123")
chk("2-5h 短数字仍走人名匹配", r[0] is None and "群里没找到" in r[1], r)

# gadmin_exec 两个调用点都必须吃下歧义、不得对第一个命中者执行
del calls[:]
r = gd.gadmin_exec({"op": "ban", "group_id": "602323545", "who": "大",
                    "minutes": 10})
chk("2-5i ban 前缀歧义不执行且有提示", r["ok"] is False
    and "不止一个人匹配" in r["detail"] and "请说全名或直接给 QQ 号" in r["detail"]
    and not any(c[0] == "set_group_ban" for c in calls), r)
del calls[:]
r = gd.gadmin_exec({"op": "recall", "group_id": "602323545", "who": "小明"})
chk("2-5j recall 精确歧义不执行且有提示", r["ok"] is False
    and "不止一个人匹配" in r["detail"]
    and not any(c[0] == "delete_msg" for c in calls), r)
del calls[:]
r = gd.gadmin_exec({"op": "ban", "group_id": "602323545", "who": "阿明明",
                    "minutes": 10})
ban_calls = [c for c in calls if c[0] == "set_group_ban"]
chk("2-5k 精确唯一照旧执行禁言", r["ok"] is True and len(ban_calls) == 1
    and ban_calls[0][1]["user_id"] == 222, (r, ban_calls))
del calls[:]
r = gd.gadmin_exec({"op": "ban", "group_id": "602323545", "who": "大明",
                    "minutes": 5})
ban_calls = [c for c in calls if c[0] == "set_group_ban"]
chk("2-5l 前缀唯一照旧执行禁言", r["ok"] is True and len(ban_calls) == 1
    and ban_calls[0][1]["user_id"] == 888 and ban_calls[0][1]["duration"] == 300,
    (r, ban_calls))
gd.mem_history["group_602323545"].append({"sender_id": "555",
                                          "message_id": "rm1",
                                          "is_bot": False, "text": "x"})
del calls[:]
r = gd.gadmin_exec({"op": "recall", "group_id": "602323545", "who": "凡达",
                    "count": 1})
del_calls = [c for c in calls if c[0] == "delete_msg"]
chk("2-5m 包含唯一照旧执行撤回", r["ok"] is True and len(del_calls) == 1
    and del_calls[0][1]["message_id"] == "rm1", (r, del_calls))

# ---- 2-6 出站剥标日志：只加日志，剥除结果逐字符与改前一致 ----
PIN_CASES = [
    ("我回复他了。他说没问题", "他说没问题", "STRATEGY_LEAD", "我回复他了。"),
    ("下次不要出现这种错了。知道吗", "知道吗", "META_CMD",
     "下次不要出现这种错了。"),
    ("把[文件]再发一遍", "把再发一遍", "PLACEHOLDER", "[文件]"),
    ("[重要]明天停课，记得带伞", "明天停课，记得带伞", "LEAD_BRACKET", "[重要]"),
    ("今晚八点吃饭\n[时间]八点带伞", "今晚八点吃饭\n八点带伞",
     "LEAD_BRACKET", "[时间]"),
]
for _i, (_src, _want, _rule, _head) in enumerate(PIN_CASES):
    del LOGS[:]
    _got = D.Daemon._strip_control_marks(_src)
    chk("2-6a%d 剥除输出钉住：%r" % (_i + 1, _src), _got == _want, repr(_got))
    _lines = [l for l in LOGS if l.startswith("SEND_MARK_STRIP")]
    chk("2-6b%d %s 日志行且字段正确" % (_i + 1, _rule),
        len(_lines) == 1 and ("rule=%s" % _rule) in _lines[0]
        and ("before=%d" % len(_src)) in _lines[0]
        and ("after=%d" % len(_want)) in _lines[0]
        and ("head=%s" % _head) in _lines[0], _lines)

del LOGS[:]
_got = D.Daemon._strip_control_marks("好了[SPLIT]先这样")
_lines = [l for l in LOGS if l.startswith("SEND_MARK_STRIP")]
chk("2-6c MARK_LEAK 剥除输出钉住", _got == "好了先这样", repr(_got))
chk("2-6d MARK_LEAK 日志字段", len(_lines) == 1
    and "rule=MARK_LEAK" in _lines[0] and "before=12" in _lines[0]
    and "after=5" in _lines[0] and "head=[SPLIT]" in _lines[0], _lines)

del LOGS[:]
_got = D.Daemon._strip_control_marks("[]又戳！")
_lines = [l for l in LOGS if l.startswith("SEND_MARK_STRIP")]
chk("2-6e EMPTY_BRACKET 剥除输出钉住", _got == "又戳！", repr(_got))
chk("2-6f EMPTY_BRACKET 日志字段", len(_lines) == 1
    and "rule=EMPTY_BRACKET" in _lines[0] and "before=5" in _lines[0]
    and "after=3" in _lines[0] and "head=[]" in _lines[0], _lines)

del LOGS[:]
_got = D.Daemon._strip_control_marks("[SPLIT][QQ表情#63]哈哈")
_lines = [l for l in LOGS if l.startswith("SEND_MARK_STRIP")]
_rules = [l.split("rule=")[1].split(" ")[0] for l in _lines]
chk("2-6g 多规则连剥输出钉住", _got == "哈哈", repr(_got))
chk("2-6h 连剥日志两条且顺序正确", _rules == ["MARK_LEAK", "PLACEHOLDER"],
    _lines)

del LOGS[:]
_got = D.Daemon._strip_control_marks("今天天气不错，一起去吃饭")
chk("2-6i 不命中句子输出原样", _got == "今天天气不错，一起去吃饭", repr(_got))
chk("2-6j 不命中不记日志",
    not [l for l in LOGS if "SEND_MARK_STRIP" in l], LOGS)

# ================= 第 3 组：引用补查重编号 / 回捞词表收窄 / /status 白名单字段 =================
# （2026-11-25 施工单 A2：延迟入箱防丢、_IMG_HIST_RE 去「然后/它」、
#   /status 新增 wl_groups / wl_allow_all）

write_wl(json.dumps({"enabled": True, "allow_all": True, "groups": []}))

# ---- 3-0 群管关键词撤回：被撤的消息不再回应（不触发不入箱不进历史/观察） ----
def fresh_ga(kw):
    """群管关键词用例专用：群管配置驻内存（不读真实文件）、_call_action 只记录
    不外发、_mem_push 计数、真实 _gadmin_keyword_check（去 fresh2 的打桩）。
    群号必须数字（_gadmin_keyword_check 里 int(chat_id)）。"""
    d = fresh2({"text_img_merge_enabled": False})
    del d._gadmin_keyword_check      # 恢复真实方法（fresh2 打桩挡住了）
    d._ga = {"groups": {"9207": kw}}
    d._group_admin_cfg = lambda: d._ga
    rec = []

    def fake_action(action, params, timeout=None):
        rec.append((action, params))
        return {}
    d._call_action = fake_action
    mp = []
    orig_mp = d._mem_push

    def counting_mp(item, mid, is_bot=False, text=None, n_images=0):
        mp.append(str(mid))
        return orig_mp(item, mid, is_bot, text, n_images)
    d._mem_push = counting_mp
    d._mp = mp
    d._rec = rec
    return d


def ingest_ga(d, ts, mid, text="低价广告速来"):
    """数字群号 9207 的纯文字条（群管关键词就配在这个群）。"""
    info = D.extract_message_info([], 10000, ("group", "9207"))
    info["text"] = text
    d._ingest_message({"time": ts, "message_id": mid},
                      "group", "9207", "测试群", "u1", "用户一", info)


# 3-0a 命中撤回关键词：撤回动作照发，本条到此为止
d = fresh_ga({"kw_recall_on": True, "kw_recall": ["广告"],
              "kw_mute_on": False, "kw_mute_words": []})
ingest_ga(d, time.time(), "k1")
_REAL_SLEEP(0.2)                     # 给后台撤回线程时间落记录
chk("3-0a 撤回动作照发 delete_msg 且无误禁言",
    ("delete_msg", {"message_id": "k1"}) in d._rec
    and not any(c[0] == "set_group_ban" for c in d._rec), d._rec)
chk("3-0b 被撤条不触发不入箱不占号不挂pending", len(d.inbox) == 0
    and d.stats["triggered"] == 0 and d.seq == 0 and not d._fwd_pending,
    (len(d.inbox), d.stats["triggered"], d.seq, dict(d._fwd_pending)))
chk("3-0c 被撤条不进历史不进观察", d._mp == [] and len(d._observe) == 0
    and not d.mem_history.get("group_9207"), (d._mp, len(d._observe)))

# 3-0d 只命中禁言关键词：行为与改前一致（动作+全流程）
d = fresh_ga({"kw_recall_on": False, "kw_recall": [],
              "kw_mute_on": True, "kw_mute_words": ["广告"]})
ingest_ga(d, time.time(), "k2")
chk("3-0d 只禁言：禁言动作照发且不撤回",
    any(c[0] == "set_group_ban" for c in d._rec)
    and not any(c[0] == "delete_msg" for c in d._rec), d._rec)
chk("3-0e 只禁言：照常触发入箱", wait_inbox(d, 1)
    and d.inbox[0]["triggered"] is True and d.inbox[0]["text"] == "低价广告速来",
    [(i["triggered"], i["text"]) for i in d.inbox])
chk("3-0f 只禁言：照常进历史进观察", len(d._mp) == 1
    and len(d._observe) == 1 and d.stats["triggered"] == 1,
    (d._mp, len(d._observe), d.stats["triggered"]))

# 3-0g 两个开关都关 / 群没有配置：照常处理
d = fresh_ga({"kw_recall_on": False, "kw_mute_on": False})
ingest_ga(d, time.time(), "k3", "正常聊天内容")
chk("3-0g 开关都关照常处理", wait_inbox(d, 1)
    and d.inbox[0]["triggered"] is True and len(d._mp) == 1
    and len(d._observe) == 1, (len(d.inbox), d._mp, len(d._observe)))
d = fresh_ga({})
ingest_ga(d, time.time(), "k4", "正常聊天内容")
chk("3-0h 群无配置照常处理", wait_inbox(d, 1) and len(d._mp) == 1
    and len(d._observe) == 1, (len(d.inbox), d._mp, len(d._observe)))

# ---- 3-1 引用补查延迟入箱：补查期间 agent 已确认更高 id → 入箱前重编号防丢 ----
class _GetMsgGate(object):
    """_call_action 替身：get_msg 卡在 gate 上（模拟补查耗时），放行后回原文。"""

    def __init__(self):
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self, action, params, timeout=None):
        if action == "get_msg":
            self.entered.set()
            self.gate.wait(5)
            return {"sender": {"nickname": "被引者", "user_id": 42},
                    "message": [{"type": "text",
                                 "data": {"text": "被引用的原文"}}]}
        return {}


def ingest_reply(d, mid, text):
    """带引用（被引消息不在内存历史 → 走后台补查延迟入箱路径）。"""
    info = D.extract_message_info([
        {"type": "reply", "data": {"id": "OLD_" + mid}},
        {"type": "text", "data": {"text": text}},
    ], 10000, ("group", "G2"))
    d._ingest_message({"time": time.time(), "message_id": mid},
                      "group", "G2", "测试群", "u1", "用户一", info)


gate = _GetMsgGate()
d = fresh2({})
d._call_action = gate
ingest_reply(d, "rq1", "引用回复")
chk("3-1a 带引用消息走补查延迟路径未即时入箱",
    gate.entered.wait(2) and len(d.inbox) == 0, len(d.inbox))
mid = d.seq                       # 该条装配时已占号
d.inbox_ack([mid])                # 模拟 agent 在补查期间确认到该号（含）
gate.gate.set()
chk("3-1b 补查完成照常入箱且内容补齐", wait_inbox(d, 1)
    and d.inbox[0]["reply_text"] == "被引用的原文"
    and d.inbox[0]["reply_to"]["sender_name"] == "被引者",
    [(i["text"], i.get("reply_text"), i.get("reply_to")) for i in d.inbox])
got = d.inbox_get()
chk("3-1c 重编号后 agent 拉得到（id>acked_max，与正常路径同规则）",
    len(got) == 1 and got[0]["id"] > d.acked_max and got[0]["id"] == d.seq
    and got[0]["text"] == "引用回复",
    (d.acked_max, [(i["id"], i["text"]) for i in got]))
ingest_reply(d, "rq2", "再引一条")
mid2 = d.seq
chk("3-1d 未被 acked_max 追上时不重编号（规则与兄弟延迟路径一致）",
    wait_inbox(d, 2) and d.inbox_get()[-1]["id"] == mid2 and d.seq == mid2,
    (mid2, d.seq, [i["id"] for i in d.inbox]))

# ---- 3-2 历史回捞词表去掉「然后」「它」，其余词不动；980 近图路径不经过词表 ----
chk("3-2a 「然后」不再命中", D._IMG_HIST_RE.search("然后呢") is None, "然后呢")
chk("3-2b 「它」不再命中", D._IMG_HIST_RE.search("它好可爱") is None, "它好可爱")
for _w in ("继续", "重试", "再来", "接着", "下一个", "下一步", "这张", "这个",
           "那个", "图", "照片", "截图", "看看", "看下", "咋样", "怎么样"):
    chk("3-2c 其余词照旧命中「%s」" % _w, D._IMG_HIST_RE.search(_w) is not None, _w)

# 行为：触发的无图纯文字，历史里有别人的近图（开关关 → 立即入箱便于断言）
d3 = fresh2({"text_img_merge_enabled": False})
d3.mem_history["group_G2"].append({
    "ts": time.time(), "message_id": "h1", "sender_id": "u2",
    "is_bot": False, "text": "[图片]", "image_urls": ["http://h/old.jpg"]})
ingest_text(d3, time.time() + 1, "x1", "然后呢")
chk("3-2d 「然后」句不回捞历史图", len(d3.inbox) == 1
    and d3.inbox[0]["image_urls"] == []
    and d3.inbox[0]["merged_nearby_image"] is False,
    [(i["text"], i["image_urls"]) for i in d3.inbox])
ingest_text(d3, time.time() + 2, "x2", "继续")
chk("3-2e 「继续」照旧回捞历史图", len(d3.inbox) == 2
    and d3.inbox[1]["image_urls"] == ["http://h/old.jpg"]
    and d3.inbox[1]["merged_nearby_image"] is True,
    [(i["text"], i["image_urls"]) for i in d3.inbox])
ingest_text(d3, time.time() + 3, "x3", "这图")
chk("3-2f 「这图」照旧回捞历史图", len(d3.inbox) == 3
    and d3.inbox[2]["image_urls"] == ["http://h/old.jpg"]
    and d3.inbox[2]["merged_nearby_image"] is True,
    [(i["text"], i["image_urls"]) for i in d3.inbox])

# 同发送者 180 秒近图路径在外层 _handle_message_event，不经过词表：含「它」照样并图
d4 = fresh2({"text_img_merge_enabled": False})
_t4 = time.time()
ingest_img_event(d4, _t4, "y1")
d4._handle_message_event({
    "time": _t4 + 1, "message_id": "y2", "message_type": "group",
    "group_id": "G2", "user_id": "u1", "sender": {"card": "用户一"},
    "message": [{"type": "text", "data": {"text": "它好可爱"}}],
})
chk("3-2g 同发送者近图路径不受词表影响", wait_inbox(d4, 2)
    and d4.inbox[1]["image_urls"] == ["http://example/1.jpg"]
    and d4.inbox[1]["merged_nearby_image"] is True,
    [(i["text"], i["image_urls"]) for i in d4.inbox])

# ---- 3-3 /status 补 wl_groups / wl_allow_all：取值即 _group_whitelist 返回条目 ----
def wl_fields(dd):
    s = st(dd)
    return s.get("wl_groups"), s.get("wl_allow_all")


rm_wl()
d = fresh()
chk("3-3a default_all → ([], True)", wl_fields(d) == ([], True), wl_fields(d))
d = fresh(cfg={"group_whitelist": ["A", "B"]})
chk("3-3b default_groups → ([A,B], False)", wl_fields(d) == (["A", "B"], False),
    wl_fields(d))
write_wl(json.dumps({"enabled": True, "allow_all": True, "groups": []}))
d = fresh()
chk("3-3c file+allow_all → ([], True)", wl_fields(d) == ([], True)
    and st(d).get("wl_state") == "file", (wl_fields(d), st(d).get("wl_state")))
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": ["A", "C"]}))
d = fresh()
chk("3-3d file+名单 → ([A,C], False)", wl_fields(d) == (["A", "C"], False),
    wl_fields(d))
write_wl(json.dumps({"enabled": True, "allow_all": False, "groups": ["KEEP"]}))
d = fresh()
d._group_allowed("KEEP")          # 先读到合法内容
write_wl("{stale坏的")
chk("3-3e stale → 沿用上一份 ([KEEP], False)", wl_fields(d) == (["KEEP"], False)
    and st(d).get("wl_state") == "stale", (wl_fields(d), st(d).get("wl_state")))
rm_wl()
d = fresh()                        # 先建实例再落坏文件 = 第一份就读不出
write_wl("{{broken坏的")
chk("3-3f broken → ([], False)", wl_fields(d) == ([], False)
    and st(d).get("wl_state") == "broken", (wl_fields(d), st(d).get("wl_state")))
write_wl(json.dumps({"enabled": False, "allow_all": True, "groups": ["A"]}))
d = fresh()
chk("3-3g 总开关关(disabled) → 如实报文件内容 ([A], True)",
    wl_fields(d) == (["A"], True) and st(d).get("wl_state") == "disabled",
    (wl_fields(d), st(d).get("wl_state")))
s = st(fresh())
want_keys = {"running", "pid", "uptime", "ws_url", "ws_connected", "ws_last_ok",
             "ws_alive", "login", "inbox_pending", "stats", "wl_state",
             "wl_error", "data_dir", "wl_groups", "wl_allow_all"}
chk("3-3h /status 字段不增不减", set(s) == want_keys, sorted(set(s) ^ want_keys))

print()
print("%d 项通过，%d 项失败" % (n_pass, len(fails)))
sys.exit(1 if fails else 0)
