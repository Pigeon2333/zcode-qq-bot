#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_scheduler.py — 会话级并行调度器（纯逻辑，不 import 项目里的其它模块）

为 qq_bot_agent 的主循环并行（WP-04）服务。约定：

  - submit(key, fn)：同一个 key 严格按提交顺序执行（每个 key 一个 FIFO 队列和
    一个懒创建的工作线程，线程空闲超过 idle_reap 秒退出）；不同 key 之间并行，
    但同时在跑的任务数不超过 workers（信号量，拿不到名额就排队）。
  - submit_owner(key, fn)：主人专用通道，单独一个线程加队列，不占 workers 名额。
  - busy()：正在执行的任务数（含主人通道）。
  - snapshot()：每个 key 的排队数、是否在执行、最早一条已等待秒数、上次执行耗时。
  - 任务抛异常不杀线程：捕获后调用 on_error(key, exc)（on_error 异常也吞掉）。

线程模型（每个 key 的状态 _KeyState）：
  submit 入队（持锁）→ 工作线程在「看队头 → 拿全局名额 → 出队执行」的循环里跑。
  名额在出队之前拿：等名额期间任务仍留在队列里（快照里它算排队，不算执行中）。
  线程退出（空闲回收）时在锁内把 state.thread 置回 None——submit 与退出判定
  用同一把锁，绝不错配「队列有活、线程已走」的窗口。
"""
import threading
import time


class _Job(object):
    __slots__ = ("fn", "ts")

    def __init__(self, fn, ts):
        self.fn = fn
        self.ts = ts      # 入队时刻（算「已等待秒数」用）


class _KeyState(object):
    __slots__ = ("queue", "thread", "wake", "running", "last_dur", "idle_since")

    def __init__(self):
        self.queue = []
        self.thread = None
        self.wake = None   # 工作线程创建时才有（lazy）
        self.running = False
        self.last_dur = 0.0
        self.idle_since = 0.0


class SessionScheduler(object):
    def __init__(self, workers=1, idle_reap=600, on_error=None):
        self.workers = max(1, int(workers))
        self.idle_reap = max(0.0, float(idle_reap))
        self.on_error = on_error
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(self.workers)
        self._keys = {}          # key → _KeyState（工作会话）
        self._owner = _KeyState()  # 主人通道（独立，永不占名额、不回收）
        self._closed = False     # close() 后空闲线程退出（换实例时回收主人线程）

    def close(self):
        """弃置本实例：只许在队列为空、无任务在跑时调（调用方负责核对）。
        普通会话线程本就会空闲自回收；这里让常驻的主人通道线程也退出。"""
        with self._lock:
            self._closed = True
            if self._owner.wake is not None:
                self._owner.wake.set()

    # ---- 提交 ----

    def submit(self, key, fn):
        """普通会话任务：同 key 保序，跨 key 受 workers 名额限制。"""
        key = str(key)
        st = self._enqueue(self._keys, key, fn)
        self._ensure_thread(key, st, owner=False)

    def submit_owner(self, key, fn):
        """主人任务：不占 workers 名额（独立线程 + 队列，同样保序）。"""
        st = self._enqueue_owner(fn)
        self._ensure_thread("__owner__", st, owner=True)

    def _enqueue(self, table, key, fn):
        with self._lock:
            st = table.get(key)
            if st is None:
                st = _KeyState()
                table[key] = st
            st.queue.append(_Job(fn, time.time()))
            if st.wake is not None:
                st.wake.set()
            return st

    def _enqueue_owner(self, fn):
        with self._lock:
            self._owner.queue.append(_Job(fn, time.time()))
            if self._owner.wake is not None:
                self._owner.wake.set()
            return self._owner

    def _ensure_thread(self, key, st, owner):
        with self._lock:
            if st.thread is not None:
                return
            st.wake = threading.Event()
            t = threading.Thread(target=self._worker,
                                 args=(key, st, owner), daemon=True)
            st.thread = t
            t.start()

    # ---- 工作线程 ----

    def _worker(self, key, st, owner):
        while True:
            # 等活：Event 置位或最多一个量子。等待时绝不持有 _lock
            # （submit 需要拿同一把锁入队）。wake 是粘性的：先 wait 再 clear，
            # clear 后在锁内核对队列——两侧交错也不会丢任务。
            if st.wake is not None:
                with self._lock:
                    if st.queue:
                        remain = 0.0        # 已有活：立即出队，不空等一个量子
                    elif owner:
                        remain = None       # 主人通道常驻：空转量子 1s
                    elif st.idle_since <= 0.0:
                        st.idle_since = time.time()
                        remain = self.idle_reap
                    else:
                        remain = st.idle_since + self.idle_reap - time.time()
                st.wake.wait(1.0 if remain is None
                             else min(1.0, max(0.0, remain)))
                st.wake.clear()
            with self._lock:
                job = None
                if st.queue:
                    job = st.queue[0]   # 先看队头、不出队：等全局名额期间它仍在队列里
                elif self._closed:
                    # 实例已被弃置且无活可干：交还「工作线程」身份后退出
                    # （close 只许在空队列时调，这里只是兜底防悬空线程）
                    st.thread = None
                    return
                elif not owner and st.idle_since > 0.0 and \
                        time.time() - st.idle_since >= self.idle_reap:
                    # 队列空且空闲超过回收线：退出前把「谁是工作线程」交还
                    # （submit 与本判定同锁，不会出现队列有活却没人管的窗口）
                    st.thread = None
                    return
                else:
                    if st.idle_since <= 0.0:
                        st.idle_since = time.time()
            if job is None:
                continue
            try:
                if not owner:
                    # 名额在出队之前拿：等名额的任务仍计在「排队」里，
                    # 「执行中」与 busy() 只统计真正占到名额的
                    self._slots.acquire()
                with self._lock:
                    st.queue.pop(0)     # 同一 key 只有一个工作线程，队头仍是它
                    st.running = True
                t0 = time.time()
                try:
                    job.fn()
                except Exception as exc:   # 任务异常不杀线程
                    cb = self.on_error
                    if cb is not None:
                        try:
                            cb(key, exc)
                        except Exception:
                            pass
                finally:
                    dur = time.time() - t0
                    if not owner:
                        self._slots.release()
                    with self._lock:
                        st.running = False
                        st.last_dur = dur
                        st.idle_since = 0.0   # 干完活重新起算空闲（回收时钟）
            except BaseException:
                # 兜底：任何一笔账没结清都不许把 running/名额留成脏状态
                if not owner:
                    try:
                        self._slots.release()
                    except ValueError:
                        pass
                with self._lock:
                    st.running = False
                raise

    # ---- 观测 ----

    def busy(self):
        """正在执行的任务数（含主人通道）。"""
        with self._lock:
            n = 1 if self._owner.running else 0
            for st in self._keys.values():
                if st.running:
                    n += 1
            return n

    def snapshot(self):
        """每个 key 的 {queued, running, waited, last_dur}；waited=最早一条已
        等待秒数（没排队为 0）。主人通道记在 key "__owner__" 下。"""
        now = time.time()
        with self._lock:
            snap = {}

            def _one(name, st):
                q = st.queue
                snap[name] = {
                    "queued": len(q),
                    "running": bool(st.running),
                    "waited": round(max(0.0, now - q[0].ts), 2) if q else 0.0,
                    "last_dur": round(st.last_dur, 2),
                }

            _one("__owner__", self._owner)
            for k, st in self._keys.items():
                _one(k, st)
            return snap
