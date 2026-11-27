# -*- coding: utf-8 -*-
"""离线冒烟：并行路径确认放行（_AckBarrier.mark_done 真发 ack）+ 防重复回复
保险（_DONE_RECENT）。2026-11-27 事故回归：agent_workers=3 时 process 内部
提前发的确认被线程截流标记吞掉，daemon 永远收不到确认，同一条消息被反复
拉回、反复处理、反复回复。

与 _smoke_scheduler.py 的关键差别：屏障拿到的是**真 DaemonClient 实例**，
只把它的传输层 _request 换成假 HTTP 层——DaemonClient.ack 里的 _ACK_TLS
截流判断是真的；_smoke_scheduler 的 FakeDaemon.ack 直接改水位，屏障放行
时 self._daemon.ack 打到它身上根本不经过截流判断，所以抓不到这个 bug。

用例：
  R1  修前必红：3 条已触发消息，假 process 一进去就提前确认（复刻事故
      路径）。全部处理完后断言水位 == 3、再拉收件箱为空。
  R2  修前必红：轮询循环模拟——6 轮「拉收件箱→派发→小睡」，每轮等在途
      清空。断言每条消息的假 process 恰好 1 次、假回复数 == 消息数。
  R3  乱序完成：id 1 慢（闸门），2、3 快。1 完成前不放行（水位 0）、
      2、3 仍在 _INFLIGHT 不重复派发；1 完成后水位 == 3。
  R4  真发确认时 HTTP 失败一次：有 AGENT_ACK_RELEASE_FAIL；TTL 内重拉
      不重派（保险生效，AGENT_SCHED_SKIP_DONE 每条消息最多一行）；时间
      拨过 TTL 后可再次派发且确认到位。
  R5  workers=1：确认照旧直发（不合并），_DONE_RECENT 不写入。

计时口径与 _smoke_scheduler.py 相同：开工先测短 sleep 实际耗时作基线，
等待上限按基线放大（WT）。不连网、不启停进程、不发消息、不调模型；
QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时目录，绝不读写生产
数据 ~/.zcode-qq-bot。测试只用内存对象，不写任何文件（config 除外，写
在隔离目录）。
用法：python BOT_REPORTS/_smoke_ackrelease.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_ackrelease_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402
import qq_common as C  # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:130] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换对象属性，退出时恢复（照 _smoke_scheduler.py 的做法）。"""
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


class FakeHttp(object):
    """假 HTTP 层（替掉 DaemonClient._request）：带水位语义的收件箱模型，
    语义照 daemon 的 inbox_get/inbox_ack——POST /ack 取 ids 最大值升水位；
    GET /inbox 只返回 id > acked_max 的条目。ack_posts 记每次 POST /ack 的
    原始 ids（核对直发/合并）。fail_ack_once 置 True 时下一次 POST /ack
    抛异常（R4 用，只失败一次）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.inbox = []
        self.acked_max = 0
        self.ack_posts = []
        self.fail_ack_once = False

    def load(self, msgs):
        with self.lock:
            self.inbox = [dict(m) for m in msgs]

    def __call__(self, method, path, body=None, timeout=15):
        if method == "GET" and path.startswith("/inbox"):
            with self.lock:
                items = [dict(m) for m in self.inbox
                         if m["id"] > self.acked_max]
                return {"ok": True, "items": items[:20]}
        if method == "POST" and path == "/ack":
            ids = list((body or {}).get("ids") or [])
            with self.lock:
                if self.fail_ack_once:
                    self.fail_ack_once = False
                    raise OSError("fake http down")
                self.ack_posts.append(list(ids))
                valid = [int(i) for i in ids if str(i).isdigit()]
                m = max(valid, default=None)
                if m is not None and m > self.acked_max:
                    self.acked_max = m
                return {"ok": True}
        raise AssertionError("unexpected request %s %s" % (method, path))


def real_client(http):
    """真 DaemonClient（截流逻辑是真的），只把传输层 _request 换成假 HTTP。"""
    c = A.DaemonClient(0, "")
    c._request = http
    return c


def wait_until(fn, timeout=8.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


# 机器负载基线（照 _smoke_scheduler.py 的口径）
_t = time.time()
time.sleep(0.3)
DRIFT = max(1.0, (time.time() - _t) / 0.3)


def WT(sec):
    """放大后的等待上限：基线 × 6 倍余量，下限保住原秒数。"""
    return max(sec, DRIFT * sec * 6.0)


def reset_parallel_state():
    """用例之间清干净并行状态：调度器实例收掉（不漏线程）、屏障重绑、
    防重复回复表清空。只在quiet之后调用。"""
    s = A._SCHED.pop("s", None)
    if s is not None:
        s.close()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None
    # 防重复回复表修复后才存在：getattr 兜底，让修复前的红色运行也能跑到
    getattr(A, "_DONE_RECENT", {}).clear()
    getattr(A, "_DONE_SKIP_LOGGED", {}).clear()
    A._DISPATCH_BUSY[0] = 0


def wait_quiet(timeout=8.0):
    """等在途任务清空：忙碌计数归零且 _INFLIGHT 空。"""
    return wait_until(
        lambda: A._DISPATCH_BUSY[0] == 0 and not A._INFLIGHT,
        timeout=WT(timeout))


_LOGS = []
_REAL_OUT = A.out


@contextlib.contextmanager
def capture_out():
    """捕获 out() 日志行（仍照常打印），供断言 AGENT_ACK_RELEASE_FAIL /
    AGENT_SCHED_SKIP_DONE。"""
    _LOGS.clear()

    def spy(line):
        _LOGS.append(line)
        _REAL_OUT(line)

    with patched(A, out=spy):
        yield


# ---------------- R1：提前确认路径（修前必红） ----------------

def r1():
    set_workers(3)
    reset_parallel_state()
    http = FakeHttp()
    cli = real_client(http)
    calls, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        # 复刻事故路径：一进 process 就提前确认（此刻本工作线程的
        # 截流标记还挂着），然后「再做点事」
        daemon.ack([msg["id"]])
        time.sleep(0.05)
        with lk:
            calls.append(str(msg["id"]))

    http.load([{"id": i, "chat_type": "group", "chat_id": "c%d" % i}
               for i in (1, 2, 3)])
    with patched(A, process=fake_process):
        A._dispatch_messages(cli, cli.inbox_triggered())
        wait_quiet(10)
        wait_until(lambda: len(calls) == 3, timeout=WT(5))
        time.sleep(0.2)     # 给任何「迟到的真发确认」留出暴露窗口
    again = cli.inbox_triggered()
    chk("R1 提前确认路径：水位==3 且收件箱拉空（修前红）",
        http.acked_max == 3 and again == [],
        "acked_max=%s again=%s calls=%s"
        % (http.acked_max, [m["id"] for m in again], sorted(calls)))


# ---------------- R2：轮询循环模拟（修前必红） ----------------

def r2():
    set_workers(3)
    reset_parallel_state()
    http = FakeHttp()
    cli = real_client(http)
    calls, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        daemon.ack([msg["id"]])
        time.sleep(0.02)
        with lk:
            calls.append(str(msg["id"]))

    http.load([{"id": i, "chat_type": "group", "chat_id": "c%d" % i}
               for i in (1, 2, 3)])
    with patched(A, process=fake_process):
        for _round in range(6):
            A._dispatch_messages(cli, cli.inbox_triggered())
            wait_quiet(8)
            time.sleep(0.05)
        with lk:
            got = list(calls)
    per = {str(i): got.count(str(i)) for i in (1, 2, 3)}
    chk("R2 轮询6轮：每条消息恰好处理1次、回复数==消息数（修前红）",
        all(v == 1 for v in per.values()) and len(got) == 3,
        "per=%s total=%d acked_max=%s" % (per, len(got), http.acked_max))


# ---------------- R3：乱序完成 ----------------

def r3():
    set_workers(3)
    reset_parallel_state()
    http = FakeHttp()
    cli = real_client(http)
    gate = threading.Event()
    calls, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        if str(msg["id"]) == "1":
            gate.wait(30)       # id 1 慢（闸门提前放开即提前结束）
        daemon.ack([msg["id"]])
        with lk:
            calls.append(str(msg["id"]))

    http.load([{"id": i, "chat_type": "group", "chat_id": "c%d" % i}
               for i in (1, 2, 3)])
    with patched(A, process=fake_process):
        A._dispatch_messages(cli, cli.inbox_triggered())
        wait_until(lambda: sorted(calls) == ["2", "3"], timeout=WT(8))
        time.sleep(0.2)
        inflight = sorted(A._INFLIGHT)
        chk("R3a 1完成前不放行：水位0、2和3仍在_INFLIGHT",
            http.acked_max == 0 and "2" in inflight and "3" in inflight,
            "acked_max=%s inflight=%s" % (http.acked_max, inflight))
        A._dispatch_messages(cli, cli.inbox_triggered())   # 再拉再派：应跳过
        time.sleep(0.2)
        with lk:
            c2, c3 = calls.count("2"), calls.count("3")
        chk("R3b 等待期间 2、3 不重复派发", c2 == 1 and c3 == 1,
            "calls=%s" % sorted(calls))
        gate.set()
        ok = wait_until(lambda: http.acked_max == 3, timeout=WT(8))
        wait_quiet(8)
        with lk:
            total = sorted(calls)
        chk("R3c 1完成后一次放行：水位==3、各恰好1次",
            ok and total == ["1", "2", "3"],
            "acked_max=%s calls=%s" % (http.acked_max, total))


# ---------------- R4：放行发送失败 + 保险 ----------------

def r4():
    set_workers(3)
    reset_parallel_state()
    http = FakeHttp()
    http.fail_ack_once = True       # 第一次真发确认就失败
    cli = real_client(http)
    calls, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        daemon.ack([msg["id"]])
        with lk:
            calls.append(str(msg["id"]))

    http.load([{"id": 7, "chat_type": "group", "chat_id": "c7"}])
    with patched(A, process=fake_process):
        with capture_out():
            A._dispatch_messages(cli, cli.inbox_triggered())
            wait_quiet(8)
            time.sleep(0.1)
        rel_fail = [l for l in _LOGS if "AGENT_ACK_RELEASE_FAIL" in l]
        chk("R4a 真发确认失败有 AGENT_ACK_RELEASE_FAIL",
            bool(rel_fail) and http.acked_max == 0,
            "logs=%s acked_max=%s" % (rel_fail[:1], http.acked_max))
        # 保险：TTL 内重拉到这条不重派
        with capture_out():
            A._dispatch_messages(cli, cli.inbox_triggered())
            time.sleep(0.2)
        with lk:
            n1 = len(calls)
        skips = [l for l in _LOGS if "AGENT_SCHED_SKIP_DONE msg_id=7" in l]
        chk("R4b TTL 内不重派（保险生效），SKIP 日志恰好一行",
            n1 == 1 and len(skips) == 1,
            "calls=%d skips=%s" % (n1, skips[:1]))
        # 时间拨过 5 分钟（把完成时刻往回拨，等价于时钟前进过 TTL）：
        # 可再次派发且这次确认到位
        A._DONE_RECENT["7"] = time.monotonic() - (A._DONE_RECENT_TTL + 5.0)
        A._dispatch_messages(cli, cli.inbox_triggered())
        ok = wait_until(lambda: http.acked_max == 7, timeout=WT(8))
        wait_quiet(8)
        with lk:
            n2 = len(calls)
        chk("R4c 过 TTL 后可再次派发、确认到位", ok and n2 == 2,
            "acked_max=%s calls=%d" % (http.acked_max, n2))


# ---------------- R5：workers=1 与从前一致 ----------------

def r5():
    set_workers(1)
    reset_parallel_state()
    http = FakeHttp()
    cli = real_client(http)
    calls, lk = [], threading.Lock()

    def fake_process(daemon, msg):
        daemon.ack([msg["id"]])
        with lk:
            calls.append(str(msg["id"]))

    http.load([{"id": 9, "chat_type": "group", "chat_id": "c9"}])
    with patched(A, process=fake_process):
        A._dispatch_messages(cli, cli.inbox_triggered())
        wait_until(lambda: http.acked_max == 9, timeout=WT(8))
    with lk:
        got = list(calls)
    with http.lock:
        raw = list(http.ack_posts)
    chk("R5 workers=1：确认逐条直发、_DONE_RECENT 不写入",
        got == ["9"] and raw == [[9]] and http.acked_max == 9
        and not A._DONE_RECENT and not A._DONE_SKIP_LOGGED,
        "calls=%s raw=%s acked_max=%s done_recent=%s"
        % (got, raw, http.acked_max, dict(A._DONE_RECENT)))


def main():
    t0 = time.time()
    print("机器负载基线 drift=%.2f（短 sleep 实际/名义；>1=机器被占用）" % DRIFT)
    r1()
    reset_parallel_state()
    r2()
    reset_parallel_state()
    r3()
    reset_parallel_state()
    r4()
    reset_parallel_state()
    r5()
    reset_parallel_state()
    print("合计：%d 项通过，%d 项失败，用时 %.1f 秒"
          % (n_pass, len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
