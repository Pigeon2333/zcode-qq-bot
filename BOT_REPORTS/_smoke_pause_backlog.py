# -*- coding: utf-8 -*-
"""冒烟：设置「恢复后不回暂停期间的消息」（pause_skip_backlog，用户定默认开）
——清理由收件循环自己在「暂停 → 恢复」的那一刻做（第二轮发回定死的改法：
不放管理接口线程，避免与收件循环抢跑/被在途误判跳过）（对应验收 P1–P9）。

  P1  开关开（默认）：积压 25 条（超一批 20）→ 走收件循环：先跑一轮「暂停
      中」，再把配置翻成已恢复跑下一轮——清理发生在 inbox_triggered 拉取
      之前（记录调用顺序）、两批全部确认（水位=25）、派发桩收到的全是空
      列表、process 桩 0 次、日志 SKIP n=25、之后拉取为空。
  P2  开关关：不确认（水位不动），恢复后积压照常进派发（旧行为）。
  P3  并行模式下 _INFLIGHT 非空（及 _DISPATCH_BUSY>0 的防御分支）→ 不清理、
      有 KEEP 日志。
  P4  拉收件箱抛异常 → 不外抛、循环继续跑、日志 AGENT_PAUSE_BACKLOG_ERR。
  P5  键登记：GET 默认 True；POST 存 False 并回读、落盘；读取函数对缺键 /
      True / False / 脏值（"yes"、1、null）的取值符合规定。
  P6  状态页有「恢复后不回暂停期间的消息」复选框、默认勾选、保存入口唯一。
  P7  源码级：_pause_backlog_skip( 的调用点只在 _inbox_loop 里、位于
      inbox_triggered() 之前；/admin/pause 分支里没有这个调用；
      _PAUSED_SEEN 存在且只在暂停分支置位。
  P8  定时暂停（_in_sched_pause）期间同样置位，恢复后清积压。
  P9  没经历过暂停（一直在处理）→ 不清理。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_tools.py 的做法）；收件箱/确认/派发/处理全部打桩、time.sleep
打桩不打真盹，零真实发送、零真实处理。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_pause_backlog.py
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_pause_backlog_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402,F401
import qq_bot_agent as A  # noqa: E402
import qq_admin_ui as U  # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:200] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


ALOG = []
A.out = lambda line: ALOG.append(str(line))

ISO_DIR = pathlib.Path(A.DATA_DIR)
RC_PATH = ISO_DIR / "reply_config.json"


def write_rc(name, obj):
    p = ISO_DIR / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


class InboxDaemon(object):
    """收件箱桩：inbox_triggered 每批最多 20 条（照真实水位语义），ack 弹出
    不超过最大编号的全部；ack 时记录「当时机器人是否还在暂停」用来证明清理
    先于状态翻转。"""

    def __init__(self, n_items=0, explode=False):
        self.pending = list(range(1, n_items + 1))
        self.acked = []
        self.paused_at_ack = []
        self._explode = explode

    def inbox_triggered(self):
        if self._explode:
            raise OSError("smoke: inbox 拉取失败")
        batch = self.pending[:20]
        return [{"id": i, "chat_type": "private", "chat_id": "10086",
                 "text": "暂停期间的消息"} for i in batch]

    def ack(self, ids):
        top = max(ids)
        self.paused_at_ack.append(bool(A.RT.snapshot()["paused"]))
        self.acked.append(top)
        self.pending = [i for i in self.pending if i > top]


class _StopLoop(BaseException):
    """穿透 _inbox_loop 的 except Exception，结束测试驱动。"""


class _FakeSleepTime(object):
    """time 桩：sleep 由 drive_loop 注入（计数+翻牌+收尾），不打真盹。"""

    def sleep(self, sec):        # 实例属性会覆盖它
        raise AssertionError("sleep 未注入")

    def strftime(self, fmt):
        return "2026-11-27 08:30"

    def localtime(self):
        class _LT(object):
            tm_hour = 8
            tm_min = 30
        return _LT()


def write_chain(paused):
    """写隔离目录里的 model_chain.json——「是否暂停」的真正来源
    （reload_chain 每轮从它读）。"""
    p = ISO_DIR / "model_chain.json"
    p.write_text(json.dumps({"models": ["m"], "paused": bool(paused)},
                            ensure_ascii=False), encoding="utf-8")


def drive_loop(daemon, rc_path=None, sched=None, flip_pause_to=None,
               max_sleeps=4):
    """驱动 _inbox_loop 跑若干轮：sleep 打桩计数不打真盹，数满抛 _StopLoop
    结束；第 1 次 sleep 时翻牌（写 model_chain 的 paused=flip_pause_to、把
    sched["on"] 翻成 False）——模拟「界面/定时把暂停翻成恢复后，收件循环
    下一轮自己发现」。返回 (order, dispatched, pulls, proc_calls)：
      order       'skip'/'pull' 顺序（清理是否先于拉取）
      dispatched  派发桩每批收到的消息 id
      pulls       每次拉取收到的消息 id
      proc_calls  process 桩调用数"""
    order, dispatched, pulls, proc_calls = [], [], [], []
    real_skip = A._pause_backlog_skip
    state = {"n": 0}

    def fake_sleep(sec):
        state["n"] += 1
        if state["n"] == 1:
            if flip_pause_to is not None:
                write_chain(flip_pause_to)
            if sched is not None:
                sched["on"] = False
        if state["n"] >= max_sleeps:
            raise _StopLoop()

    in_skip = {"active": False}   # 清理内部的批量拉取不算 loop 级拉取

    def skip_rec(d):
        order.append("skip")
        in_skip["active"] = True
        try:
            return real_skip(d)
        finally:
            in_skip["active"] = False

    real_pull = daemon.inbox_triggered   # 先存原桩方法再包装（否则自递归）

    def pull_rec():
        if in_skip["active"]:
            return real_pull()
        order.append("pull")
        items = real_pull()
        pulls.append([it.get("id") for it in items])
        return items

    def dispatch_rec(d, items):
        dispatched.append([it.get("id") for it in items])

    def proc_rec(*a, **k):
        proc_calls.append(1)

    daemon.inbox_triggered = pull_rec
    tmod = _FakeSleepTime()
    tmod.sleep = fake_sleep
    patches = {"time": tmod,
               "_pause_backlog_skip": skip_rec,
               "_dispatch_messages": dispatch_rec,
               "process": proc_rec}
    if rc_path is not None:
        patches["REPLY_CONFIG_PATH"] = rc_path
    if sched is not None:
        patches["_in_sched_pause"] = lambda: sched["on"]
    with patched(A, **patches):
        try:
            A._inbox_loop(daemon, 2.5)
        except _StopLoop:
            pass
    return order, dispatched, pulls, proc_calls


def ui_call(method, path, bodyq=None):
    """进程内直调管理台 UiHandler（照 _smoke_toolloop_run.py 的写法）。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    raw = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


# ============ P1 开关开（默认）：收件循环走「暂停→恢复」完整流程 ============

RC_ON = write_rc("rc_on.json", {"pause_skip_backlog": True})
write_chain(True)          # 第一轮：配置里是暂停中
dmp1 = InboxDaemon(25)
order1, disp1, pulls1, proc1 = drive_loop(dmp1, rc_path=RC_ON,
                                          flip_pause_to=False, max_sleeps=4)
chk("P1a 清理发生在拉取之前（先 skip 后 pull，且只清一次）",
    order1 and order1[0] == "skip" and order1.count("skip") == 1
    and "pull" in order1, order1)
chk("P1b 积压 25 条两批全部确认（ack 水位 20→25），之后每轮拉取为空",
    dmp1.acked == [20, 25] and dmp1.pending == []
    and pulls1 and all(p == [] for p in pulls1), (dmp1.acked, pulls1))
chk("P1c 没有一条被处理：派发桩收到的全是空列表、process 桩 0 次",
    disp1 and all(d == [] for d in disp1) and proc1 == [], (disp1, proc1))
chk("P1d 日志 AGENT_PAUSE_BACKLOG_SKIP n=25",
    any("AGENT_PAUSE_BACKLOG_SKIP n=25" in l for l in ALOG),
    [l for l in ALOG if "BACKLOG" in l])
chk("P1e 恢复由配置文件驱动（收件循环自己发现），状态已为恢复",
    A.RT.snapshot()["paused"] is False, A.RT.snapshot()["paused"])

# ============ P2 开关关：不确认，恢复后积压照常进派发（旧行为） ============

RC_OFF = write_rc("rc_off.json", {"pause_skip_backlog": False})
write_chain(True)
dmp2 = InboxDaemon(25)
order2, disp2, pulls2, proc2 = drive_loop(dmp2, rc_path=RC_OFF,
                                          flip_pause_to=False, max_sleeps=4)
chk("P2 开关关：清理不确认（ack 水位不动）、恢复后积压 20 条照常进派发",
    dmp2.acked == [] and disp2 and disp2[0] == list(range(1, 21))
    and len(disp2[0]) == 20, (dmp2.acked, disp2 and disp2[0]))

# ============ P3 并行在途：不清理、KEEP 日志 ============

dmp3 = InboxDaemon(3)
with patched(A, REPLY_CONFIG_PATH=RC_ON, _INFLIGHT={"mid-1"}):
    A._pause_backlog_skip(dmp3)
chk("P3a 并行模式在途（_INFLIGHT 非空）：不清理、日志 KEEP",
    dmp3.acked == [] and len(dmp3.pending) == 3
    and any("AGENT_PAUSE_BACKLOG_KEEP" in l for l in ALOG),
    (dmp3.acked, [l for l in ALOG if "KEEP" in l]))
dmp3b = InboxDaemon(3)
with patched(A, REPLY_CONFIG_PATH=RC_ON, _DISPATCH_BUSY=[1]):
    A._pause_backlog_skip(dmp3b)
chk("P3b 防御分支（_DISPATCH_BUSY>0，在该调用位置恒为 0）：同样不清理",
    dmp3b.acked == [] and len(dmp3b.pending) == 3,
    (dmp3b.acked, dmp3b.pending))

# ============ P4 拉收件箱抛异常：不外抛、循环继续跑 ============

write_chain(True)
dmp4 = InboxDaemon(0, explode=True)
order4, disp4, pulls4, proc4 = drive_loop(dmp4, rc_path=RC_ON,
                                          flip_pause_to=False, max_sleeps=7)
chk("P4a 拉取失败不外抛、收件循环继续跑（清理试过、拉取反复重试）",
    order4.count("skip") == 1 and order4.count("pull") >= 2
    and any("AGENT_LOOP_ERROR" in l for l in ALOG),
    (order4, [l for l in ALOG if "LOOP_ERROR" in l][:1]))
chk("P4b 日志 AGENT_PAUSE_BACKLOG_ERR（清理内部的异常只记日志）",
    any("AGENT_PAUSE_BACKLOG_ERR" in l for l in ALOG),
    [l for l in ALOG if "ERR" in l])

# ============ P8 定时暂停同样置位，恢复后清积压 ============

write_chain(False)         # 手动未暂停；暂停来自定时暂停
sched8 = {"on": True}
dmp8 = InboxDaemon(25)
order8, disp8, pulls8, proc8 = drive_loop(dmp8, rc_path=RC_ON, sched=sched8,
                                          max_sleeps=4)
chk("P8 定时暂停期间置位、恢复后清积压（水位 20→25、SKIP n=25）",
    order8 and order8[0] == "skip" and dmp8.acked == [20, 25]
    and dmp8.pending == []
    and any("AGENT_PAUSE_BACKLOG_SKIP n=25" in l for l in ALOG),
    (order8, dmp8.acked))

# ============ P9 没经历过暂停：不清理 ============

write_chain(False)
dmp9 = InboxDaemon(2)
order9, disp9, pulls9, proc9 = drive_loop(dmp9, rc_path=RC_ON, max_sleeps=3)
chk("P9 一直在处理（没经历过暂停）：不触发清理，积压照常进派发",
    "skip" not in order9 and dmp9.acked == []
    and disp9 and disp9[0] == [1, 2],
    (order9, dmp9.acked, disp9))

# ============ P5 键登记与读取函数 ============

rp5 = ui_call("do_GET", "/api/reply-config")
chk("P5a GET 默认值里 pause_skip_backlog=True（无配置文件时）",
    rp5 and rp5[0][0] == 200 and rp5[0][1].get("pause_skip_backlog") is True,
    rp5 and rp5[0][1].get("pause_skip_backlog"))
rp5b = ui_call("do_POST", "/api/reply-config", {"pause_skip_backlog": False})
rp5c = ui_call("do_GET", "/api/reply-config")
on_disk5 = json.loads(RC_PATH.read_text(encoding="utf-8"))
chk("P5b POST 能存成 False：回读 False、落盘 False",
    rp5b and rp5b[0][1].get("ok") is True
    and rp5c[0][1].get("pause_skip_backlog") is False
    and on_disk5.get("pause_skip_backlog") is False, (rp5c[0][1], on_disk5))

f_missing = write_rc("rc_missing.json", {"tool_loop_enabled": True})
f_true = write_rc("rc_true.json", {"pause_skip_backlog": True})
f_false = write_rc("rc_false.json", {"pause_skip_backlog": False})
f_str = write_rc("rc_str.json", {"pause_skip_backlog": "yes"})
f_int = write_rc("rc_int.json", {"pause_skip_backlog": 1})
f_null = write_rc("rc_null.json", {"pause_skip_backlog": None})
vals = []
for p in (f_missing, f_true, f_false, f_str, f_int, f_null):
    with patched(A, REPLY_CONFIG_PATH=p):
        vals.append(A._pause_skip_backlog_on())
chk("P5c 读取函数：缺键/True/False/脏值(str,1)/null → T/T/F/T/T/T",
    vals == [True, True, False, True, True, True], vals)

# ============ P6 界面复选框 ============

SRC_UI = pathlib.Path("plugins", "qq-bot", "scripts",
                      "qq_admin_ui.py").read_text(encoding="utf-8")
chk("P6a 状态页有「恢复后不回暂停期间的消息」复选框，挂在暂停按钮旁",
    "恢复后不回暂停期间的消息" in SRC_UI
    and 'saveKVLoad(\'/api/reply-config\',{pause_skip_backlog:this.checked?1:0' in SRC_UI,
    "")
chk("P6b 默认勾选（缺键/非 false 都按勾选渲染），保存入口唯一",
    SRC_UI.count("rv.pause_skip_backlog!==false?'checked':''") == 1
    and SRC_UI.count("{pause_skip_backlog:this.checked?1:0") == 1, "")
chk("P6c 状态页数据源带上了 reply-config（rv）",
    "api('/api/reply-config').catch(()=>({}))" in SRC_UI, "")

# ============ P7 源码级：调用点只在收件循环里、先清后拉 ============

SRC_A = pathlib.Path("plugins", "qq-bot", "scripts",
                     "qq_bot_agent.py").read_text(encoding="utf-8")
i_loop = SRC_A.index("def _inbox_loop")
i_loop_end = SRC_A.index("\ndef ", i_loop + 10)
loop_body = SRC_A[i_loop:i_loop_end]
call_sites = [m.start() for m in
              re.finditer(r"_pause_backlog_skip\(daemon\)", SRC_A)]
i_admin = SRC_A.index('path == "/admin/pause"')
i_admin_end = SRC_A.index("AGENT_PAUSE set", i_admin)
chk("P7a 全源码只有定义+一处调用，且调用点在 _inbox_loop 函数体内",
    SRC_A.count("def _pause_backlog_skip(") == 1
    and len(call_sites) == 2 and i_loop <= call_sites[1] < i_loop_end,
    [c for c in call_sites])
chk("P7b 调用点位于 inbox_triggered() 拉取之前（先清后拉，顺序不能变）",
    loop_body.index("_pause_backlog_skip(daemon)")
    < loop_body.index("items = daemon.inbox_triggered()"), "")
chk("P7c /admin/pause 分支里没有清理调用（不再从管理接口线程做）",
    all(not (i_admin <= c < i_admin_end) for c in call_sites)
    and "want_paused" in SRC_A, [c for c in call_sites])
chk("P7d _PAUSED_SEEN 存在，置位/复位各一处（置位只在暂停分支）",
    "_PAUSED_SEEN = [False]" in SRC_A
    and SRC_A.count("_PAUSED_SEEN[0] = True") == 1
    and SRC_A.count("_PAUSED_SEEN[0] = False") == 1, "")

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
for _h in list(A._usage_log.handlers):
    try:
        _h.close()
    except Exception:
        pass
    A._usage_log.removeHandler(_h)
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
