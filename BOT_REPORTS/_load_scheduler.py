# -*- coding: utf-8 -*-
"""压测（手动跑，不进自动回归）：主循环并行下 200 条消息 / 8 个会话 / workers=3。

假 daemon（收件箱按 id>acked_max 水位返回未 ack 消息）+ 假模型（每条随机延迟
0–3 秒、约一成随机抛异常），打桩 process 走真的 _inbox_loop/_dispatch_messages/
SessionScheduler 全链路。断言（逻辑不变量严格，时间上限宽放）：
  L1  跑完无未捕获异常（on_error 次数 == 假模型主动抛的次数）
  L2  无死锁：全部处理完、忙碌归零。总超时只是兜底，按开工实测的机器
      负载基线（短 sleep 实际/名义耗时）放大——2026-11-26 实测机器被其它
      进程占满时 sleep 类等待被拉长 2 倍以上，固定 240s 会误报
  L3  每个会话的处理顺序等于入队顺序
  L4  每条消息恰好被确认一次：合并确认下表现为「最终水位 == 200」（放行按
      编号连续切段 + L6 严格递增 ⇒ 每个编号恰好落在一段里）
  L5  并行生效（最大同时在跑 ≥2；若恒等于 1 视为退化为串行，判失败）
  L6  确认水位序列严格递增（按序确认屏障的放行纪律）

ack 语义照真 process：结尾无条件确认恰好一次；确认走真 DaemonClient.ack
（并行工作线程被按序确认屏障截流，合并放行——daemon 的 ack 是水位
acked_max，放任各自直发会让高编号先完成时把还在处理的低编号消息永久
判为已收）。异常在确认之后继续抛出，由调度器 on_error 接住。

QQBOT_DATA_DIR 指到一次性临时目录；不连 ws、不连真实端口、不碰生产数据。
用法：python BOT_REPORTS/_load_scheduler.py   （正常约 2 分钟，机器繁忙更久）
"""
import json
import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_scheduler_load_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402
import qq_common as C  # noqa: E402

TOTAL = 200
CHATS = 8
WORKERS = 3
random.seed(20261126)

# 机器负载基线：3 次短 sleep 取最大。理论工时 = 200 条 × 平均 1.5s ÷ 3 workers
# ≈ 100s，再乘重试余量 1.5 与基线漂移、6 倍宽放——只做死锁兜底，正常情况
# 几分钟内跑完就提前退出。
_drifts = []
for _ in range(3):
    _t = time.time()
    time.sleep(0.5)
    _drifts.append((time.time() - _t) / 0.5)
DRIFT = max(1.0, max(_drifts))
TIMEOUT = max(240.0, (TOTAL * 1.5 / WORKERS) * 1.5 * 6.0 * DRIFT)

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


class LoadDaemon(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.inbox = []
        self.acked_max = 0
        self.ack_count = {}
        self.ack_calls_raw = []   # 每次 ack 调用的原始参数（水位序列断言用）
        self.conc = {"n": 0, "max": 0}

    def inbox_triggered(self):
        with self.lock:
            return [dict(m) for m in self.inbox if m["id"] > self.acked_max][:20]

    def ack(self, ids):
        with self.lock:
            self.ack_calls_raw.append(list(ids))
            for i in ids:
                k = str(i)
                self.ack_count[k] = self.ack_count.get(k, 0) + 1
            valid = [int(i) for i in ids if str(i).isdigit()]
            m = max(valid, default=None)
            if m is not None and m > self.acked_max:
                self.acked_max = m

    def status(self):
        return {"quiet_until": 0}


def main():
    print("机器负载基线 drift=%.2f（短 sleep 实际/名义）；死锁兜底超时 %.0fs"
          % (DRIFT, TIMEOUT))
    C.APP_CONFIG_PATH.write_text(json.dumps({"agent_workers": WORKERS}),
                                 encoding="utf-8")
    A._SCHED.clear()
    A._INFLIGHT.clear()
    A._ACK_BARRIER = None

    d = LoadDaemon()
    # 200 条消息轮转塞进 8 个会话（入队顺序 = id 顺序）
    d.inbox = [{"id": i + 1, "chat_type": "group",
                "chat_id": "g%d" % (i % CHATS)} for i in range(TOTAL)]

    on_error_hits = []
    raise_seen = []      # 假模型抛过异常的消息 id（每条恰好抛一次）
    per_chat_order = {}
    lk = threading.Lock()

    def fake_process(daemon, msg):
        # 假模型：随机延迟 0–3 秒；约一成随机抛异常。
        # ack 走真 DaemonClient.ack（以假 daemon 为 self）：并行工作线程挂着
        # 屏障标记，确认被截流进按序确认屏障，由屏障合并放行（照生产路径）。
        # 语义照真 process：结尾无条件 ack 恰好一次；异常从 finally 的 ack
        # 之后继续抛出 → 调度器 on_error 接住（线程不死）。
        r = random.random()
        with d.lock:
            d.conc["n"] += 1
            d.conc["max"] = max(d.conc["max"], d.conc["n"])
        try:
            time.sleep(random.uniform(0, 3))
            with lk:
                per_chat_order.setdefault(str(msg["chat_id"]),
                                          []).append(str(msg["id"]))
            if r < 0.1:
                with lk:
                    raise_seen.append(str(msg["id"]))
                raise RuntimeError("fake model boom")
        finally:
            A.DaemonClient.ack(daemon, [msg["id"]])
            with d.lock:
                d.conc["n"] -= 1

    def on_error(key, exc):
        with lk:
            on_error_hits.append(key)

    orig_sched_for = A._sched_for

    def sched_for(workers):
        s = orig_sched_for(workers)
        s.on_error = on_error
        return s

    real_process = A.process
    A.process = fake_process
    A._sched_for = sched_for
    t0 = time.time()
    try:
        th = threading.Thread(target=A._inbox_loop, args=(d, 0.05), daemon=True)
        th.start()

        def all_acked():
            with d.lock:
                return d.acked_max >= TOTAL

        while time.time() - t0 < TIMEOUT:
            if all_acked() and A._DISPATCH_BUSY[0] == 0:
                break
            time.sleep(0.2)
        took = time.time() - t0
        with d.lock:
            wmark_final = d.acked_max
            ack_raw = [list(c) for c in d.ack_calls_raw]
        # 屏障合并确认后，daemon 侧不再有「每条一次」的 ack 调用可数；
        # 「每条消息恰好被确认一次」由两条可观测事实钉住：
        #   a) 每条消息恰好被处理一次（L3 的会话顺序记录即事实）；
        #   b) 最终水位 == 最大编号 且 放行序列严格递增（L6）——放行按编号
        #      连续切段，两者合起来 = 每个编号恰好落在一段里、没有遗漏或重复
        chk("L2 无死锁（兜底超时 %.0fs 内全部处理完）" % TIMEOUT,
            took < TIMEOUT and wmark_final == TOTAL
            and A._DISPATCH_BUSY[0] == 0,
            "took=%.1fs busy=%s wmark=%d drift=%.2f"
            % (took, A._DISPATCH_BUSY[0], wmark_final, DRIFT))
        chk("L4 恰好覆盖：最终水位==200",
            wmark_final == TOTAL,
            "wmark=%s" % wmark_final)
        marks = [c[0] for c in ack_raw if c]
        inc = all(b > a for a, b in zip(marks, marks[1:]))
        chk("L6 确认水位序列严格递增",
            inc and len(marks) >= 1 and marks[0] >= 1,
            "calls=%d marks=%s" % (len(marks), marks[:8]))
        # 假模型抛的每一条异常都应被调度器接住（线程不死、不逃逸为未捕获），
        # 且消息本身已按真 process 的语义在结尾确认恰好一次
        chk("L1 无未捕获异常（on_error 次数=假模型异常次数）",
            sorted(on_error_hits) != [] and
            len(on_error_hits) == len(raise_seen) and A._DISPATCH_BUSY[0] == 0,
            "on_error=%d raises=%d busy=%s"
            % (len(on_error_hits), len(raise_seen), A._DISPATCH_BUSY[0]))
        # L3 每会话处理顺序 == 入队顺序（每条消息只处理一次，记录即事实顺序）
        ok_order = True
        detail = []
        for cid, seq in per_chat_order.items():
            want = [str(i + 1) for i in range(TOTAL) if "g%d" % (i % CHATS) == cid]
            if seq != want:
                ok_order = False
                detail.append((cid, seq[:5], want[:5]))
        chk("L3 每会话处理顺序==入队顺序", ok_order and len(per_chat_order) == CHATS,
            str(detail[:2]))
        chk("L5 并行生效（最大同时在跑 ≥2）",
            d.conc["max"] >= 2, "max_concurrent=%s" % d.conc["max"])
        print("---- 压测读数：总耗时 %.1fs，最终水位=%d，确认调用=%d 次，"
              "on_error=%d，最大并行=%d，每会话均摊 %.1f 条 ----"
              % (took, wmark_final, len(ack_raw), len(on_error_hits),
                 d.conc["max"], TOTAL / CHATS))
    finally:
        A.process = real_process
        A._sched_for = orig_sched_for
        s = A._SCHED.pop("s", None)
        if s is not None:
            s.close()
        A._INFLIGHT.clear()
        A._ACK_BARRIER = None

    print("合计：%d 项通过，%d 项失败，用时 %.1f 秒"
          % (n_pass, len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
