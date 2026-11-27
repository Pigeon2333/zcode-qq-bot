# -*- coding: utf-8 -*-
"""离线冒烟：WP-04 主循环并行（会话调度器 + 定时器线程/收件线程）。

调度器本体（qq_scheduler.SessionScheduler）：
  S1  同 key 严格保序（含主人通道）
  S2  workers=3 时不同 key 确实并行（同时在跑 ≥2）
  S3  workers=1 时跨 key 串行
  S4  名额占满时主人通道不被阻塞（不占 workers 名额）
  S5  任务抛异常不杀线程：同 key 后续任务照跑、on_error 被调
  S6  空闲回收：idle 超时后工作线程退出，再 submit 自动重建且保序
  S7  snapshot 数字：queued/running/waited/last_dur/busy

主循环接线（qq_bot_agent，打桩 process 与假 daemon）：
  W1  agent_workers=1（默认）：派发顺序与从前一致（逐条、按拉到顺序）
  W2  agent_workers=3：一个会话卡住（闸门，上限 30 秒）期间另一个会话的
      消息得到处理——按事件先后断言（B 的任务在 A 的阻塞任务结束之前
      开始并完成），不依赖机器快慢
  W3  同一个会话内保持顺序（卡住的那条之后排队的消息后处理）
  W4  消息阻塞期间定时器线程照常执行周期动作（主动/定时任务/插件 tick）
  W5  处理中的消息不会被重复派发（daemon 按 id>acked_max 返回未 ack 消息，
      收件线程跳过在处理中的 id）
  W6  并发配置热切换：3→1 后走同步路径；1→3 懒创建调度器
  W7  /admin/queues 快照：workers/busy/会话打码（号码只留尾 4 位）
  W8  并行路径按序确认屏障（daemon 的 ack 是水位 acked_max，高编号先完成
      不得盖掉还在处理的低编号消息）：
      W8a 低编号卡住、高编号先完成：屏障期间假 daemon 零确认、水位不动、
          低编号仍在收件箱（此刻画面上它会被重投）；低编号完成后一次放行，
          水位 == 这一段最后一个编号
      W8b 任务抛异常不卡水位：异常的编号记完成，后面的确认照常发出
      W8c 同进程内不重复派发（屏障等待期间再拉再派会被 _INFLIGHT 跳过）
      W8d workers=1 确认行为与改动前一致（逐条直发，不经屏障、不合并）

计时口径：本机可能被其它进程占满 CPU（2026-11-26 压测实测 sleep 类等待被
拉长 2 倍以上）。所有必须等待的地方：开工先测一次短 sleep 的实际耗时作基线，
超时上限按基线放大（WT），断言尽量比「事件先后」而不是绝对时长。逻辑不变量
（同会话保序、异常不杀线程、快照数字）不放宽。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_scheduler.py
"""
import contextlib
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_scheduler_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_scheduler as S  # noqa: E402
import qq_bot_agent as A  # noqa: E402
import qq_common as C  # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:110] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换对象属性，退出时恢复（照 _smoke_botloop.py 的做法）。"""
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def set_workers(n):
    """把 agent_workers 写进隔离数据目录的 app_config.json。"""
    C.APP_CONFIG_PATH.write_text(json.dumps({"agent_workers": n}),
                                 encoding="utf-8")


class FakeDaemon(object):
    """假 daemon：收件箱按「id > acked_max（水位）」返回未 ack 消息，
    语义照 qq_daemon.inbox_get/inbox_ack。ack_calls_raw 记每次调用的原始
    参数（核对水位合并）；ack_calls 展平成单个编号。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.inbox = []
        self.acked_max = 0
        self.ack_calls = []
        self.ack_calls_raw = []

    def load(self, msgs):
        with self.lock:
            self.inbox = [dict(m) for m in msgs]

    def inbox_triggered(self):
        with self.lock:
            items = [dict(m) for m in self.inbox if m["id"] > self.acked_max]
            return items[:20]

    def ack(self, ids):
        with self.lock:
            self.ack_calls_raw.append(list(ids))
            self.ack_calls.extend(list(ids))
            valid = [int(i) for i in ids if str(i).isdigit()]
            m = max(valid, default=None)
            if m is not None and m > self.acked_max:
                self.acked_max = m

    def status(self):
        return {"quiet_until": 0}


def wait_until(fn, timeout=8.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


# 机器负载基线：短 sleep 的实际耗时 / 名义耗时。机器被占满时 >1，
# 所有等待上限按它放大（断言本身尽量比事件先后，不比绝对时长）。
_t = time.time()
time.sleep(0.3)
DRIFT = max(1.0, (time.time() - _t) / 0.3)


def WT(sec):
    """放大后的等待上限：基线 × 6 倍余量，下限保住原秒数。"""
    return max(sec, DRIFT * sec * 6.0)


def _noop(*a, **k):
    return None


# ---------------- S：调度器本体 ----------------

def test_scheduler():
    # S1 同 key 保序（普通 + 主人通道）
    errs = []
    sc = S.SessionScheduler(workers=3, idle_reap=600,
                            on_error=lambda k, e: errs.append((k, e)))
    done, lk = [], threading.Lock()

    def rec(v, dur=0.0):
        def f():
            if dur:
                time.sleep(dur)
            with lk:
                done.append(v)
        return f

    for i in range(6):
        sc.submit("a", rec("a%d" % i, 0.005))
    sc.submit_owner("own", rec("o0", 0.005))
    sc.submit_owner("own", rec("o1", 0.005))
    ok = wait_until(lambda: len(done) == 8, timeout=WT(5))
    # 同 key 保序 = key 内相对顺序不变；不同通道（主人 vs 普通会话）允许交错
    a_seq = [v for v in done if v.startswith("a")]
    o_seq = [v for v in done if v.startswith("o")]
    chk("S1 同key保序（含主人通道）",
        ok and a_seq == ["a%d" % i for i in range(6)] and o_seq == ["o0", "o1"],
        done)

    # S2 workers=3 跨 key 并行
    gate = threading.Event()
    conc = {"n": 0, "max": 0}
    clk = threading.Lock()

    def hold():
        with clk:
            conc["n"] += 1
            conc["max"] = max(conc["max"], conc["n"])
        gate.wait(30)
        with clk:
            conc["n"] -= 1

    sc.submit("p1", hold)
    sc.submit("p2", hold)
    sc.submit("p3", hold)
    ok = wait_until(lambda: conc["max"] >= 3, timeout=WT(5))
    gate.set()
    wait_until(lambda: sc.busy() == 0, timeout=WT(5))
    chk("S2 workers=3 跨key并行", ok and conc["max"] >= 3,
        "max_concurrent=%s" % conc["max"])

    # S3 workers=1 串行
    sc1 = S.SessionScheduler(workers=1, idle_reap=600, on_error=None)
    g2 = threading.Event()
    seq = {"max": 0, "n": 0}

    def hold1():
        with clk:
            seq["n"] += 1
            seq["max"] = max(seq["max"], seq["n"])
        g2.wait(30)
        with clk:
            seq["n"] -= 1

    sc1.submit("x", hold1)
    sc1.submit("y", hold1)
    # 事件口径：等第一个任务真正开跑，再给一小段宽限观察第二个没有跟着跑
    wait_until(lambda: seq["n"] >= 1, timeout=WT(5))
    grace_end = time.time() + WT(1.0)
    while time.time() < grace_end and seq["max"] == 1:
        time.sleep(0.02)
    snap_max = seq["max"]
    g2.set()
    wait_until(lambda: sc1.busy() == 0, timeout=WT(5))
    chk("S3 workers=1 跨key串行", snap_max == 1 and sc1.busy() == 0,
        "max_concurrent=%s" % snap_max)

    # S4 名额占满时主人通道不被阻塞（事件口径：主人任务完成时两个名额仍被占着）
    sc4 = S.SessionScheduler(workers=2, idle_reap=600, on_error=None)
    g4 = threading.Event()
    c4 = {"n": 0, "max": 0}

    def hold4():
        with clk:
            c4["n"] += 1
            c4["max"] = max(c4["max"], c4["n"])
        g4.wait(30)
        with clk:
            c4["n"] -= 1

    sc4.submit("h1", hold4)
    sc4.submit("h2", hold4)
    wait_until(lambda: c4["n"] == 2, timeout=WT(5))
    o_done = []
    sc4.submit_owner("owner", lambda: o_done.append(1))
    ok = wait_until(lambda: bool(o_done), timeout=WT(5))
    slots_still_held = c4["n"] == 2
    g4.set()
    chk("S4 名额占满主人通道不阻塞",
        ok and slots_still_held and c4["max"] == 2,
        "slots_held=%s max=%s" % (slots_still_held, c4["max"]))

    # S5 异常不杀线程 + on_error
    sc5 = S.SessionScheduler(workers=2, idle_reap=600,
                             on_error=lambda k, e: errs.append((k, e)))
    err_flag = []

    def boom():
        err_flag.append(1)
        raise RuntimeError("boom")

    sc5.submit("e", boom)
    sc5.submit("e", rec("after_err", 0.0))
    ok = wait_until(lambda: "after_err" in done and len(errs) >= 1,
                    timeout=WT(5))
    chk("S5 异常不杀线程+on_error", ok and "after_err" in done
        and any(k == "e" for k, _ in errs), "errs=%s" % (errs[-1:],))

    # S6 空闲回收
    sc6 = S.SessionScheduler(workers=2, idle_reap=0.3, on_error=None)
    sc6.submit("r", rec("r0", 0.0))
    wait_until(lambda: "r0" in done, timeout=WT(3))
    ok_gone = wait_until(lambda: sc6._keys["r"].thread is None, timeout=WT(3))
    sc6.submit("r", rec("r1", 0.0))
    ok_back = wait_until(lambda: "r1" in done, timeout=WT(3))
    chk("S6 空闲回收后重建", ok_gone and ok_back and "r1" in done,
        "thread_reaped=%s" % ok_gone)

    # S7 snapshot 数字
    sc7 = S.SessionScheduler(workers=4, idle_reap=600, on_error=None)
    g7 = threading.Event()
    t_sub = time.time()
    sc7.submit("k", lambda: g7.wait(30))
    sc7.submit("k", lambda: g7.wait(30))
    sc7.submit("k", lambda: g7.wait(30))
    # 事件口径：等第一条真正开跑，再给 0.25s 让排队的两条排好（漂移只会拉长
    # 等待、让 waited 更大，断言方向安全）
    wait_until(lambda: sc7.snapshot()["k"]["running"], timeout=WT(3))
    time.sleep(0.25)
    snap = sc7.snapshot()
    row = snap.get("k", {})
    chk("S7 snapshot数字",
        row.get("queued") == 2 and row.get("running") is True
        and row.get("waited", 0) > 0.15 and row.get("last_dur", -1) == 0.0
        and sc7.busy() == 1,
        "snap=%s busy=%s" % (row, sc7.busy()))
    g7.set()
    wait_until(lambda: sc7.busy() == 0, timeout=WT(5))
    # last_dur 记的是「上一次执行」的耗时：gate 开了之后排队的两条几乎瞬时跑完，
    # 所以再单独跑一条已知耗时的任务来核对记账（先等它真正开跑，再等跑完）
    sc7.submit("k", lambda: time.sleep(0.2))
    wait_until(lambda: sc7.snapshot()["k"]["running"], timeout=3)
    wait_until(lambda: sc7.busy() == 0, timeout=5)
    row2 = sc7.snapshot()["k"]
    chk("S7b 跑完last_dur记账",
        row2.get("queued") == 0 and row2.get("running") is False
        and row2.get("last_dur", 0) > 0.15, "snap=%s" % row2)


# ---------------- W：主循环接线 ----------------

def test_wiring():
    # W1 默认（=1）派发顺序与从前一致
    set_workers(1)
    d = FakeDaemon()
    seen, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        time.sleep(0.01)
        with lk:
            seen.append(str(msg["id"]))
        daemon.ack([msg["id"]])

    msgs = [{"id": i, "chat_type": "group", "chat_id": "100%d" % (i % 3)}
            for i in range(1, 8)]
    d.load(msgs)
    with patched(A, process=fake_process):
        A._dispatch_messages(d, d.inbox_triggered())
    chk("W1 workers=1 派发顺序与从前一致",
        seen == [str(m["id"]) for m in msgs] and len(d.ack_calls) == 7,
        "seen=%s" % seen)

    # W2/W3/W4 workers=3：一个会话卡住（闸门，上限 30 秒），另一个会话的
    # 消息在它卡住期间得到处理——按事件先后断言，不比绝对时长；
    # 同会话保序；阻塞期间定时器线程照常执行周期动作
    set_workers(3)
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d2 = FakeDaemon()
    order, lk2 = [], threading.Lock()
    gate = threading.Event()

    def fake_process2(daemon, msg):
        cid = str(msg["chat_id"])
        if cid == "2001":
            with lk2:
                order.append(("enter", "g1", str(msg["id"]), time.time()))
            gate.wait(30)   # 派单口径：卡 30 秒（闸门提前放开即提前结束）
            with lk2:
                order.append(("done", "g1", str(msg["id"]), time.time()))
        else:
            time.sleep(0.05)
            with lk2:
                order.append(("done", cid, str(msg["id"]), time.time()))
        daemon.ack([msg["id"]])

    ticks, tlk = [], threading.Lock()

    def rec_tick(name):
        def f(*a, **k):
            with tlk:
                ticks.append(name)
        return f

    d2.load([
        {"id": 101, "chat_type": "group", "chat_id": "2001"},
        {"id": 102, "chat_type": "group", "chat_id": "2002"},
        {"id": 103, "chat_type": "group", "chat_id": "2001"},
    ])
    with patched(A, process=fake_process2,
                 _proactive_tick=rec_tick("proactive"),
                 _cron_tick=rec_tick("cron"),
                 _nudge_tick=rec_tick("nudge")):
        # 收件线程在后台线程里跑（真机驻主线程；测试里等价于派发逻辑）
        inbox = threading.Thread(target=A._inbox_loop, args=(d2, 0.05),
                                 daemon=True)
        timer = threading.Thread(target=A._timer_loop, args=(d2, 0.05),
                                 daemon=True)
        timer.start()
        inbox.start()
        # 等会话 2002 的 102 处理完（此时 101 应还卡着）
        def g2_done():
            with lk2:
                return any(e[0] == "done" and e[2] == "102" for e in order)
        ok_b = wait_until(g2_done, timeout=WT(15))
        with lk2:
            g1_done_yet = any(e[0] == "done" and e[2] == "101" for e in order)
            ts_101_enter = next((e[3] for e in order
                                 if e[0] == "enter" and e[2] == "101"), None)
            ts_102_done = next((e[3] for e in order
                                if e[0] == "done" and e[2] == "102"), None)
        # 事件先后：102 在 101 已经进入（卡住）之后完成，且 101 尚未完成
        after_enter = (ts_101_enter is not None and ts_102_done is not None
                       and ts_102_done >= ts_101_enter)
        chk("W2 另一会话在卡住会话阻塞期间得到处理（事件先后）",
            ok_b and not g1_done_yet and after_enter,
            "ok=%s g1_stuck=%s enter101=%s done102=%s"
            % (ok_b, not g1_done_yet, ts_101_enter, ts_102_done))
        # W4 阻塞期间定时器线程照常执行周期动作（等它至少跑满一轮）
        ok_ticks = wait_until(lambda: len(ticks) >= 3, timeout=WT(10))
        with tlk:
            ticked = list(ticks)
        chk("W4 阻塞期间定时器线程照常执行周期动作",
            ok_ticks and len(ticked) >= 3, "ticks=%s" % ticked[:8])
        # W3 同会话保序：103 排在 101 之后（101 未 done 前 103 不该 done）
        with lk2:
            o103 = [e for e in order if e[2] == "103"]
        chk("W3a 同会话排队消息在卡住消息完成前未处理", not o103, str(o103))
        gate.set()
        # 屏障合并确认：101 完成放行 [101,102]（ack 带 102），103 完成放行 [103]
        # ——全部确认到位的标志是水位到 103，不是 ack 调用里的编号个数
        ok_all = wait_until(lambda: d2.acked_max == 103, timeout=WT(10))
        with lk2:
            g1_ids = [e[2] for e in order if e[1] == "g1" and e[0] == "done"]
        chk("W3b 同会话保序（101 先于 103 完成，确认全部到位）",
            ok_all and g1_ids == ["101", "103"], "g1_done=%s wmark=%s"
            % (g1_ids, d2.acked_max))

    # W4b 默认（=1）模式：沿用从前「周期性动作不打断消息处理」——process 卡住
    # 期间定时器一轮都不做，处理完之后才做（并行模式才允许同时进行，见 W4）。
    # 先起收件线程、等处理进入卡住状态之后再起定时器线程，避开起步时的交错。
    set_workers(1)
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d2b = FakeDaemon()
    gate2b = threading.Event()
    entered2b = threading.Event()
    ticks2b, tlk2b = [], threading.Lock()

    def rec_tick2b(name):
        def f(*a, **k):
            with tlk2b:
                ticks2b.append(name)
        return f

    def fake_process2b(daemon, msg):
        entered2b.set()
        gate2b.wait(30)
        daemon.ack([msg["id"]])

    d2b.load([{"id": 201, "chat_type": "group", "chat_id": "3001"}])
    with patched(A, process=fake_process2b,
                 _proactive_tick=rec_tick2b("proactive"),
                 _cron_tick=rec_tick2b("cron"),
                 _nudge_tick=rec_tick2b("nudge")):
        ib = threading.Thread(target=A._inbox_loop, args=(d2b, 0.05),
                              daemon=True)
        ib.start()
        ok_enter = wait_until(entered2b.is_set, timeout=WT(10))
        tm = threading.Thread(target=A._timer_loop, args=(d2b, 0.05),
                              daemon=True)
        tm.start()
        time.sleep(WT(1.0))       # 卡住期间：留给定时器足够多轮
        with tlk2b:
            during = list(ticks2b)
        chk("W4b 默认（=1）模式：消息处理卡住期间定时器不做周期动作",
            ok_enter and not during,
            "enter=%s ticks_during=%s" % (ok_enter, during[:4]))
        gate2b.set()
        ok_after = wait_until(lambda: len(ticks2b) >= 3, timeout=WT(10))
        chk("W4b 默认（=1）模式：处理完之后定时器照常做周期动作",
            ok_after, "ticks=%d" % len(ticks2b))

    # W5 处理中的消息不重复派发：process 卡住未 ack 时，下一轮拉取会再
    # 返回同一条（FakeDaemon 水位语义），派发必须跳过它
    set_workers(3)
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d3 = FakeDaemon()
    calls, lk3 = [], threading.Lock()
    g3 = threading.Event()

    def fake_process3(daemon, msg):
        with lk3:
            calls.append(str(msg["id"]))
        g3.wait(30)
        daemon.ack([msg["id"]])

    d3.load([{"id": 201, "chat_type": "group", "chat_id": "3001"}])
    with patched(A, process=fake_process3):
        A._dispatch_messages(d3, d3.inbox_triggered())
        wait_until(lambda: len(calls) >= 1, timeout=WT(5))
        time.sleep(0.3)
        # 模拟下一轮拉取：消息仍未 ack，还会被返回
        again = d3.inbox_triggered()
        chk("W5a 假daemon水位语义返回未ack消息",
            [str(m["id"]) for m in again] == ["201"],
            str([m["id"] for m in again]))
        A._dispatch_messages(d3, again)
        time.sleep(0.3)
        with lk3:
            n_calls = len(calls)
        g3.set()
        wait_until(lambda: A._DISPATCH_BUSY[0] == 0, timeout=WT(8))
    chk("W5b 处理中的消息不重复派发", n_calls == 1, "calls=%s" % calls)

    # W6 热切换：3 → 1 走同步路径；1 → 3 懒创建调度器
    set_workers(1)
    d4 = FakeDaemon()
    seen4, lk4 = [], threading.Lock()

    def fake_process4(daemon, msg):
        with lk4:
            seen4.append(str(msg["id"]))
        daemon.ack([msg["id"]])

    d4.load([{"id": 301, "chat_type": "group", "chat_id": "4001"}])
    with patched(A, process=fake_process4):
        A._dispatch_messages(d4, d4.inbox_triggered())
        wait_until(lambda: "301" in seen4, timeout=WT(5))
        # 旧实例排空后应被弃置（切回逐个处理）
        ok_off = wait_until(lambda: A._SCHED.get("s") is None, timeout=WT(5))
    set_workers(3)
    d4.load([{"id": 302, "chat_type": "group", "chat_id": "4001"}])
    with patched(A, process=fake_process4):
        A._dispatch_messages(d4, d4.inbox_triggered())

        def done302():
            with lk4:
                return "302" in seen4
        wait_until(done302, timeout=WT(5))
        ok_sched = wait_until(lambda: A._SCHED.get("s") is not None
                              and A._SCHED["s"].workers == 3, timeout=WT(5))
        wait_until(lambda: A._DISPATCH_BUSY[0] == 0, timeout=WT(8))
    chk("W6 热切换（3→1 同步路径+弃置；1→3 懒创建）",
        ok_off and ok_sched, "off=%s sched=%s" % (ok_off, ok_sched))

    # W7 /admin/queues 快照与打码
    snap = A._queues_snapshot()
    chats = [r["chat"] for r in snap["sessions"]]
    masked_ok = all(
        (":" in c and "…" in c and len(c.split("…", 1)[1]) <= 4)
        or c == "owner" for c in chats)
    chk("W7 queues快照+号码打码（只留尾4位）",
        snap["workers"] == 3 and isinstance(snap["busy"], int) and masked_ok,
        "workers=%s busy=%s sessions=%s" % (snap["workers"], snap["busy"], chats))
    chk("W7b 长号码只露尾4位",
        A._mask_key_tail("group:12345678") == "group:…5678"
        and A._mask_key_tail("private:42") == "private:…42",
        A._mask_key_tail("group:12345678") + " " + A._mask_key_tail("private:42"))

    # ---- W8 并行路径按序确认屏障 ----
    set_workers(3)
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d8 = FakeDaemon()
    gate8 = threading.Event()
    done8, lk8 = [], threading.Lock()

    def fake8(daemon, msg):
        if str(msg["id"]) == "10":
            gate8.wait(30)      # 低编号（会话 A）卡住
        with lk8:
            done8.append(str(msg["id"]))
        # 走真 DaemonClient.ack（以假 daemon 为 self）：截流判断在那里面，
        # 工作线程挂着屏障标记时编号进屏障而不是发包
        A.DaemonClient.ack(daemon, [msg["id"]])

    d8.load([{"id": 10, "chat_type": "group", "chat_id": "8001"},
             {"id": 20, "chat_type": "group", "chat_id": "8002"}])
    with patched(A, process=fake8):
        A._dispatch_messages(d8, d8.inbox_triggered())

        def b_done():
            with lk8:
                return "20" in done8
        wait_until(b_done, timeout=WT(5))
        time.sleep(0.2)   # 给任何「不该发生的直发确认」留出暴露窗口
        with d8.lock:
            raw = list(d8.ack_calls_raw)
            wmark = d8.acked_max
        pending = [m["id"] for m in d8.inbox_triggered()]   # 别在 d8.lock 里调
        chk("W8a1 低编号卡住期间零确认、水位不动",
            raw == [] and wmark == 0, "raw=%s wmark=%s" % (raw, wmark))
        # 此刻画面上 A 的消息仍在收件箱：进程若在这一瞬重启，它会被重投
        chk("W8a2 低编号仍在收件箱（重启会被重投，不丢）",
            10 in pending and A._INFLIGHT and "10" in A._INFLIGHT,
            "pending=%s inflight=%s" % (pending, sorted(A._INFLIGHT)))
        A._dispatch_messages(d8, d8.inbox_triggered())   # 再拉再派：应被跳过
        time.sleep(0.1)
        with lk8:
            runs = list(done8)
        chk("W8c 屏障等待期间不重复派发", runs == ["20"], "runs=%s" % runs)
        gate8.set()
        ok_w = wait_until(lambda: d8.acked_max == 20, timeout=WT(5))
        with d8.lock:
            raw2 = list(d8.ack_calls_raw)
        chk("W8a3 低编号完成后按序放行，水位==这段最后编号（合并一次）",
            ok_w and raw2 == [[20]], "raw=%s acked_max=%s"
            % (raw2, d8.acked_max))

    # W8b 异常不卡水位：低编号任务抛异常（不 ack），后面的确认照常发出
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d9 = FakeDaemon()
    g9 = threading.Event()
    errs9 = []

    def fake9(daemon, msg):
        if str(msg["id"]) == "30":
            raise RuntimeError("boom30")   # 异常路径：不走 ack，靠屏障记完成
        A.DaemonClient.ack(daemon, [msg["id"]])   # 走真截流逻辑（见 fake8 注）

    def sched9(workers):
        s = real_sched_for(workers)     # 引用打桩前存下的真函数（别递归自己）
        s.on_error = lambda k, e: errs9.append((k, e))
        return s

    d9.load([{"id": 30, "chat_type": "group", "chat_id": "9001"},
             {"id": 31, "chat_type": "group", "chat_id": "9002"}])
    real_sched_for = A._sched_for
    with patched(A, process=fake9, _sched_for=sched9):
        A._dispatch_messages(d9, d9.inbox_triggered())

        def w9():
            return d9.acked_max >= 31
        ok9 = wait_until(w9, timeout=WT(8))
        wait_until(lambda: A._DISPATCH_BUSY[0] == 0, timeout=WT(8))
    with d9.lock:
        raw9 = list(d9.ack_calls_raw)
        w9_max = d9.acked_max
    chk("W8b 异常不卡水位、后续确认照常发出",
        ok9 and w9_max == 31 and raw9 and raw9[-1] == [31],
        "raw=%s acked_max=%s on_err=%d"
        % (raw9, w9_max, len(errs9)))
    A._sched_for = real_sched_for

    # W8d workers=1：确认行为与改动前一致（逐条直发、不合并、不经屏障）
    set_workers(1)
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    d10 = FakeDaemon()
    seen10, lk10 = [], threading.Lock()

    def fake10(daemon, msg):
        with lk10:
            seen10.append(str(msg["id"]))
        daemon.ack([msg["id"]])

    d10.load([{"id": 40, "chat_type": "group", "chat_id": "a"},
              {"id": 41, "chat_type": "group", "chat_id": "a"}])
    with patched(A, process=fake10):
        A._dispatch_messages(d10, d10.inbox_triggered())
        wait_until(lambda: len(seen10) == 2, timeout=WT(5))
    with d10.lock:
        raw10 = list(d10.ack_calls_raw)
        w10 = d10.acked_max
    chk("W8d workers=1 确认照旧直发（逐条、不经屏障、不合并）",
        raw10 == [[40], [41]] and w10 == 41,
        "raw=%s acked_max=%s" % (raw10, w10))

    # 收尾：清掉调度器与在处理集合，不给后续冒烟留状态
    s = A._SCHED.pop("s", None)
    if s is not None:
        s.close()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None


def main():
    t0 = time.time()
    print("机器负载基线 drift=%.2f（短 sleep 实际/名义；>1=机器被占用）" % DRIFT)
    test_scheduler()
    test_wiring()
    print("合计：%d 项通过，%d 项失败，用时 %.1f 秒"
          % (n_pass, len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
