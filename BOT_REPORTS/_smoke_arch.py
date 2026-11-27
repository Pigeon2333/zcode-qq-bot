# -*- coding: utf-8 -*-
"""对话架构改造冒烟（离线：不联网、不写用户数据文件）。

覆盖：回复策略 v2（结构化输出解析 / 桥接 / 直接对话必回 / 判定失败按沉默）、
后台识图（登记去重 / 描述线程 / 观察队列轮询 / 提示词里的图速写 / 处理流程分流 /
插件观察记录不外发图链 / 每日额度与过期任务）、视觉调用重构后行为不变。

用法：python BOT_REPORTS/_smoke_arch.py
"""
import contextlib
import copy
import json
import os
import pathlib
import random
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）：import qq_bot_agent 时就会打开
# usage_log.jsonl 句柄、部分用例会落盘——默认把 QQBOT_DATA_DIR 指到一次性临时目录，
# 绝不读写生产数据 ~/.zcode-qq-bot。外部已显式设置 QQBOT_DATA_DIR 时尊重原值。
_SMOKE_OWN_DATA_DIR = None
if not os.environ.get("QQBOT_DATA_DIR"):
    _SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_smoke_iso_")
    os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:110] if extra else ""))
    if not cond:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换模块属性，退出时恢复。值用 (obj, name) 记录原值。"""
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


class _Stop(BaseException):
    """穿透 process() 里的 except Exception，用来在指定位置停下。"""


class NullDaemon(object):
    """只提供显式写出的方法；其余任何方法都是无副作用的空操作。"""

    def __init__(self, history=None, observe_items=None):
        self._history = history or []
        self._observe_items = observe_items or []
        self.acked = []
        self.observe_calls = []

    def history(self, chat_type, chat_id, limit=30):
        h = self._history
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def ack(self, ids):
        self.acked += list(ids)

    def observe(self, since=0):
        self.observe_calls.append(since)
        items = [dict(x) for x in self._observe_items if x.get("seq", 0) > since]
        # 当前序号 = 队列里最大的一条（与真实 daemon 一致）
        return max([x.get("seq", 0) for x in self._observe_items] + [0]), items

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}

        return _noop


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["seq"], A._CAP["init"], A._CAP["poll"] = 0, False, 0.0
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


def group_msg(text, mid, urls, trig="chat_random", sender="10001", **kw):
    m = {"id": 7001, "chat_type": "group", "chat_id": "555001", "chat_name": "冒烟群",
         "sender_id": sender, "sender_name": "小张", "text": text, "trigger_reason": trig,
         "message_id": mid, "image_urls": list(urls), "at_me": trig == "at",
         "at_users": [], "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0}
    m.update(kw)
    return m


def read_src(name):
    return pathlib.Path(os.path.join("plugins", "qq-bot", "scripts", name)).read_text(
        encoding="utf-8")


LOG = []


def _no_net(*a, **k):
    raise AssertionError("离线冒烟不允许调用模型（未打桩的联网路径）")


with patched(A, log_usage=lambda *x, **k: None, _log_context_dump=lambda *x, **k: None,
             out=lambda line: LOG.append(str(line)), search_web=lambda q: ([], "none"),
             web_search_enabled=lambda: False, glm_chat=_no_net):
    A.PLUGIN_HOST.on_message_all = lambda msg: None
    A.PLUGIN_HOST.log = lambda line: None

    # ---------- 1. 结构化输出解析 ----------
    ok, o = A._parse_reply_json('{"want": 2, "say": "哈哈", "emoji": null, "memory": [], '
                                '"wait": false, "voice": false, "sticker": null, '
                                '"quiet_minutes": null, "actions": []}')
    chk("1a say 是字符串 → 单元素数组", ok and o["say"] == ["哈哈"], o)
    ok, o = A._parse_reply_json('```json\n{"say": ["a", "b", "c", "d"]}\n```')
    chk("1b 代码块包裹可解析；say 最多 3 条", ok and o["say"] == ["a", "b", "c"], o and o["say"])
    ok, o = A._parse_reply_json('{"want": 9, "say": ["x"], "memory": [{"qq": "12345", "text": "t"}, {"text": "无号"}]}')
    chk("1c 非法 want 回落；记忆带号/不带号", ok and o["want"] == 2
        and o["memory"] == [("12345", "t"), (None, "无号")], o and o["memory"])
    chk("1d 不是 JSON → 失败", A._parse_reply_json("直接说了一句话")[0] is False)
    chk("1e 缺 say → 失败", A._parse_reply_json('{"want": 3}')[0] is False)

    # ---------- 2. 桥接成旧协议文本 ----------
    base = {"want": 2, "say": ["一", "二"], "emoji": None, "memory": [], "wait": False,
            "voice": False, "sticker": None, "quiet": None, "actions": []}
    t = A._json_to_legacy(base)
    chk("2a 连发两条 → 用分隔标记；能被 parse_reply 还原成两条",
        t == "一[[SPLIT]]二" and A.parse_reply(t)[0] == ["一", "二"], t)
    t = A._json_to_legacy(dict(base, say=[], want=1, emoji=63))
    chk("2b 只挂表情 → [回应:id]，无文字", t == "[回应:63]" and not A.parse_reply(t)[0]
        and A._REACT_MARK_RE.search(t) is not None, t)
    t = A._json_to_legacy(dict(base, voice=True, actions=["[连线]"]))
    chk("2c 语音/连线标记各占一行", t == "一[[SPLIT]]二\n[连线]\n[语音]", t)
    t = A._json_to_legacy(dict(base, wait=True))
    chk("2d 等对方说完 → [WAIT]", t == "[WAIT]", t)
    t = A._json_to_legacy(dict(base, want=0, say=["不该发的话"], quiet=10))
    chk("2e want=0 时 say 不发；安静分钟数保留", t == "[QUIET:10]", t)
    t = A._json_to_legacy(dict(base, say=[], memory=[("10001", "喜欢猫")]))
    chk("2f 只有记忆 → 记忆标记", t == "[记忆@10001] 喜欢猫", t)

    # ---------- 3. 主调用：模型沿用旧习惯输出 [跳过] 不算格式错误 ----------
    calls = []


    def _always(text):
        def _f(messages, text_ctx="", budget_sec=200, **kwargs):
            calls.append(copy.deepcopy(messages))
            return "m", text
        return _f


    dm = NullDaemon()
    SU = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True):
        with patched(A, call_glm_with_fallback=_always("[跳过]")):
            m, r = A._generate_reply_v2(dm, group_msg("今天好累", "m0", []), SU)
            chk("3a 模型输出 [跳过] → 一次调用、按沉默处理",
                len(calls) == 1 and r == "[跳过]", (len(calls), r))
            chk("3b 结构化规则只加在副本上，不改传入的 messages",
                calls[0][0]["content"].startswith("S") and A.JSON_RULE_HEAD in calls[0][0]["content"]
                and calls[0][1]["content"] == "U" and SU[0]["content"] == "S")
        with patched(A, call_glm_with_fallback=_always("这不是 JSON，是一句普通回复")):
            calls.clear()
            m, r = A._generate_reply_v2(dm, group_msg("今天好累", "m0", []), SU)
            chk("3c 两次都不是 JSON → 重试一次后按文本协议处理（共两次调用）",
                r == "这不是 JSON，是一句普通回复" and len(calls) == 2, (r, len(calls)))

        # 直接对话必回
        with patched(A, call_glm_with_fallback=_always('```json\n{"say": [], "want": 0}\n```')):
            calls.clear()
            LOG.clear()
            m, r = A._generate_reply_v2(dm, group_msg("在吗", "m1", [], trig="at"), SU)
            chk("3d 直接对话两次都沉默 → 重试一次后仍沉默：只调两次，不出错",
                len(calls) == 2 and r == "[跳过]"
                and any(x.startswith("AGENT_MUST_REPLY_RETRY") for x in LOG), (len(calls), r))
        calls.clear()
        LOG.clear()
        script = iter(['{"say": []}', '{"say": ["在呢"]}'])

        def _next2(ms, t, budget_sec=200, **kwargs):
            calls.append(copy.deepcopy(ms))
            return "m", next(script)

        with patched(A, call_glm_with_fallback=_next2):
            m, r = A._generate_reply_v2(dm, group_msg("在吗", "m1", [], trig="at"), SU)
            chk("3e 直接对话只挂表情/沉默 → 带提示重试一次并采纳重试结果",
                len(calls) == 2 and A.parse_reply(r)[0] == ["在呢"], (len(calls), r))
            chk("3f 重试提示并进原 user 消息末尾，仍是 system/user 两条（不追加连续的 user）",
                len(calls[1]) == 2 and A._MUST_REPLY_NOTE in calls[1][1]["content"]
                and calls[1][1]["content"].startswith("U\n\n")
                and calls[1][0]["content"] == calls[0][0]["content"])
        calls.clear()
        with patched(A, call_glm_with_fallback=_always('{"say": ["在呢在呢"]}')):
            m, r = A._generate_reply_v2(dm, group_msg("在吗", "m1", [], trig="at"), SU)
            chk("3g 直接对话已回应 → 不重试", len(calls) == 1 and A.parse_reply(r)[0] == ["在呢在呢"])

        # 插话判定结论带进主调用
        calls.clear()
        with patched(A, call_glm_with_fallback=_always('{"want": 1, "say": [], "emoji": 63}')):
            m, r = A._generate_reply_v2(dm, group_msg("发了个表情包", "m2", []), SU, gate="E")
            sys_e = calls[0][0]["content"]
            chk("3h 判定「回个表情就够」→ 结论写进 system（结构化版），且不改传入的 messages",
                A._GATE_E_NOTE_JSON in sys_e and A._GATE_E_NOTE_TEXT not in sys_e
                and A._GATE_Y_NOTE not in sys_e and sys_e.index(A.JSON_RULE_HEAD) < sys_e.index(A._GATE_E_NOTE_JSON)
                and SU[0]["content"] == "S", sys_e[-60:])
        calls.clear()
        with patched(A, call_glm_with_fallback=_always("[回应:74]"), REPLY_JSON_FORCE=False):
            A._generate_reply_v2(dm, group_msg("发了个表情包", "m2", []), SU, gate="E")
            chk("3i 文本协议下同一条结论用文本版措辞", A._GATE_E_NOTE_TEXT in calls[0][0]["content"]
                and A._GATE_E_NOTE_JSON not in calls[0][0]["content"])
        calls.clear()
        with patched(A, call_glm_with_fallback=_always('{"say": ["哈哈"]}'), REPLY_JSON_FORCE=True):
            A._generate_reply_v2(dm, group_msg("发了个表情包", "m2", []), SU, gate="Y")
            sys_y = calls[0][0]["content"]
            chk("3j 判定「有话想说」→ 带上对应结论", A._GATE_Y_NOTE in sys_y and A._GATE_E_NOTE_JSON not in sys_y)
        calls.clear()
        with patched(A, call_glm_with_fallback=_always('{"say": ["哈哈"]}')):
            A._generate_reply_v2(dm, group_msg("发了个表情包", "m2", []), SU)
            chk("3k 没有判定结论（直接对话）→ 什么结论都不加",
                A._GATE_Y_NOTE not in calls[0][0]["content"] and A._GATE_E_NOTE_JSON not in calls[0][0]["content"])

    # 判定
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: ("m", " Y。")):
        chk("3g 判定返回 Y", A._reply_gate_v2(dm, group_msg("x", "m", []), [
            {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "智谱娘") == "Y")
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: ("m", "不知道")):
        chk("3h 判定输出不合法 → 按沉默", A._reply_gate_v2(dm, group_msg("x", "m", []), [
            {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "智谱娘") == "N")

    def _raise(ms, t, budget_sec=200, **kwargs):
        raise RuntimeError("down")

    with patched(A, call_glm_with_fallback=_raise):
        LOG.clear()
        chk("3i 判定调用失败 → 按沉默并记日志",
            A._reply_gate_v2(dm, group_msg("x", "m", []), [
                {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "智谱娘") == "N"
            and any(x.startswith("AGENT_GATE_ERR") for x in LOG))

    # ---------- 4. 后台识图：登记 / 去重 ----------
    U1, U2 = "http://img.example/a.jpg", "http://img.example/b.jpg"
    reset_cap()
    with patched(A, _cap_chat_allowed=lambda ct, cid: True):
        A._caption_enqueue("group", "555001", "m1", [U1])
        A._caption_enqueue("group", "555001", "m1", [U1])
        chk("4a 同一条消息重复登记只排一次", len(A._CAP["queue"]) == 1 and len(A._CAP["pending"]) == 1)
        A._caption_enqueue("group", "555001", "m2", [])
        A._caption_enqueue("group", "555001", "", [U1])
        A._caption_enqueue("group", "555001", "m3", ["not-a-url"])
        chk("4b 没消息编号 / 没图 / 不是 http 图链 → 不登记", len(A._CAP["queue"]) == 1)
        A._caption_enqueue("group", "555001", "m9", [U2], front=True)
        chk("4c 插队任务排在最前", A._CAP["queue"][0]["key"][2] == "m9" and A._CAP["queue"][0]["front"])
    with patched(A, _cap_chat_allowed=lambda ct, cid: False):
        A._caption_enqueue("group", "555002", "m5", [U1])
    chk("4d 名单外的群不登记", len(A._CAP["queue"]) == 2 and "m5" not in str(A._CAP["pending"]))

    # ---------- 5. 后台识图：描述任务 ----------
    reset_cap()
    with patched(A, _cap_chat_allowed=lambda ct, cid: True,
                 _caption_call=lambda dmn, u: "这张图片是一只戴墨镜的狗。" if u == U1 else None):
        A._caption_enqueue("group", "555001", "m1", [U1, U2])
        job = A._CAP["queue"].popleft()     # 线程取任务时出队
        A._caption_run(NullDaemon(), job)
        c = A._caption_get("group", "555001", "m1")
        chk("5a 识出一张、另一张失败 → 只留识出的；描述去掉「这张图片是」开头并去掉句号",
            c and c["texts"] == ["一只戴墨镜的狗"], c)
        chk("5b 任务完成即出队", not A._CAP["queue"] and not A._CAP["pending"])
        A._caption_enqueue("group", "555001", "m2", [U1])
        c2 = A._caption_get("group", "555001", "m2")
        chk("5c 同一张图再出现 → 直接复用，不排队", c2 and c2["texts"] == ["一只戴墨镜的狗"]
            and not A._CAP["queue"], c2)
        A._caption_enqueue("group", "555001", "m3", [U2])
        A._caption_run(NullDaemon(), A._CAP["queue"].popleft())
        chk("5d 全部识不出 → 记一条空描述（主线程不必干等）",
            A._caption_get("group", "555001", "m3")["texts"] == [])
        # 每日额度
        A._caption_enqueue("group", "555001", "m4", ["http://img.example/c.jpg"], front=True)
        _n = A._CAP["n"]
        with patched(A, vision_caption_daily_cap=lambda: _n):
            A._caption_run(NullDaemon(), A._CAP["queue"].popleft())
        chk("5e 额度用完 → 任务丢弃不入库", not A._CAP["queue"] and not A._CAP["pending"]
            and A._caption_get("group", "555001", "m4") is None and A._CAP["n"] == _n)
        A._caption_enqueue("group", "555001", "m6", ["http://img.example/d.jpg"])
        A._CAP["queue"][0]["ts"] = time.time() - 400
        A._caption_run(NullDaemon(), A._CAP["queue"].popleft())
        chk("5f 普通任务排队超过 3 分钟 → 放弃",
            A._caption_get("group", "555001", "m6") is None and not A._CAP["pending"]
            and A._CAP["n"] == _n)

    # ---------- 6. 观察队列轮询 ----------
    reset_cap()
    recs = [
        {"seq": 991, "chat_type": "group", "chat_id": "555001", "message_id": "a1", "image_urls": [U1]},
        {"seq": 992, "chat_type": "group", "chat_id": "555001", "message_id": "a2", "image_urls": []},
        {"seq": 993, "chat_type": "group", "chat_id": "555001", "message_id": "a3", "image_urls": [U2]},
    ]
    fd = NullDaemon(observe_items=recs)
    with patched(A, _cap_chat_allowed=lambda ct, cid: True):
        A._caption_poll(fd)
        keys = sorted(j["key"][2] for j in A._CAP["queue"])
        chk("6a 启动首轮：对齐水位后只回放最近一段；只登记带图的消息", keys == ["a1", "a3"], keys)
        chk("6b 水位线推进到当前 seq", A._CAP["seq"] == 993 and A._CAP["init"])
        A._CAP["poll"] = 0.0
        fd2 = NullDaemon(observe_items=recs)
        fd2.observe = lambda since=0: (5, [dict(recs[0])]) if since < 10 else (5, [])
        A._caption_poll(fd2)
        chk("6c daemon 重启（seq 回零）→ 重新对齐，不重复登记",
            A._CAP["init"] is False and len(A._CAP["queue"]) == 2)

    # ---------- 7. 描述线程 ----------
    reset_cap()
    seen = []
    thread_err = []


    class _Obs(object):
        def __init__(self):
            self.n = 0

        def observe(self, since=0):
            self.n += 1
            if since >= 10 ** 9:
                return 10 ** 9, []
            return 10 ** 9, [{"seq": 1, "chat_type": "group", "chat_id": "555001",
                              "message_id": "t1", "image_urls": [U1]}] if self.n == 2 else []

        def __getattr__(self, name):
            return lambda *a, **k: {"ok": False}


    obs = _Obs()
    with patched(A, VCAP_FORCE=True, vision_enabled=lambda: True, vision_mode=lambda: "all_prob",
                 _in_sched_pause=lambda: False, _cap_chat_allowed=lambda ct, cid: True,
                 _caption_call=lambda dmn, u: seen.append(u) or "一张聊天截图，在约饭"):
        th = threading.Thread(target=A._caption_loop, args=(obs,), daemon=True)
        th.start()
        t_end = time.time() + 6
        while time.time() < t_end and A._caption_get("group", "555001", "t1") is None:
            time.sleep(0.1)
        chk("7a 线程从观察队列取到图并识完", seen == [U1] and (
            A._caption_get("group", "555001", "t1") or {}).get("texts") == ["一张聊天截图，在约饭"], seen)
        with patched(A, VCAP_FORCE=False):
            time.sleep(5.5)
        chk("7b 线程没有抛异常", not any(x.startswith("AGENT_VCAP_LOOP_ERR") for x in LOG), LOG[-3:])
        chk("7c 关闭开关期间线程只是空转", seen == [U1])
        # 收线程：后面的用例要确定性地控制「有没有描述」，不能再有线程在后台识图
        A._CAP["stop"].set()
        A._CAP["wake"].set()
        th.join(8)
        chk("7d 置停止位后线程退出", not th.is_alive())
        A._CAP["stop"].clear()
        A._CAP["wake"].clear()

    # ---------- 8. 插件观察记录不外发图链 ----------
    got = []
    reset_cap()
    A._OBS_PUMP.update({"seq": 0, "ts": 0.0, "init": False})
    fd = NullDaemon(observe_items=[dict(recs[0]), dict(recs[1])])
    with patched(A.PLUGIN_HOST, has_observers=lambda: True, observe_all=lambda it: got.append(dict(it))):
        A._observe_pump(fd)          # 首次：只对齐水位
        A._OBS_PUMP.update({"seq": 990, "ts": 0.0})
        A._observe_pump(fd)          # 之后：按 seq 增量取，交给插件
    chk("8a 插件拿到的观察记录没有图链和消息编号",
        got and all("image_urls" not in x and "message_id" not in x for x in got)
        and all(x["chat_id"] == "555001" for x in got), [sorted(x) for x in got[:1]])

    # ---------- 9. 提示词里的图速写 ----------
    reset_cap()
    now = time.time()
    hist = [
        {"ts": now - 300, "sender_id": "10001", "sender_name": "小张", "is_bot": False,
         "text": "[图片]", "message_id": "h1", "image_urls": [U1]},
        {"ts": now - 200, "sender_id": "10002", "sender_name": "小李", "is_bot": False,
         "text": "看这个", "message_id": "h2", "image_urls": [U2]},
        {"ts": now - 100, "sender_id": "10003", "sender_name": "小王", "is_bot": False,
         "text": "[图片]", "message_id": "h3", "image_urls": ["http://img.example/never.jpg"]},
    ]
    with A._CAP["lock"]:
        A._CAP["by_mid"][("group", "555001", "h1")] = {"texts": ["一只橘猫瞪眼"], "ts": now}
        A._CAP["by_mid"][("group", "555001", "h2")] = {"texts": ["游戏战绩截图，赢了"], "ts": now}
    block, _legend, n_cap = A._render_history_v2(hist, now, 50, chat=("group", "555001"))
    plain, _l2, n2 = A._render_history_v2(hist, now, 50)
    chk("9a 有描述的图 → 行里带速写；纯图占位被替换", n_cap == 2
        and "小张：（图：一只橘猫瞪眼）" in block and "小王：（发了张图）" in block, block)
    chk("9b 带文字的图 → 文字后面加速写", "小李：看这个 （图：游戏战绩截图，赢了）" in block, block)
    chk("9c 不给 chat（识图关着）→ 与从前完全一样", n2 == 0 and "（图：" not in plain
        and "小张：（发了张图）" in plain, plain)
    chk("9d 纯图消息的「现在」正文换成口语，不再出现方括号占位",
        A._now_body_v2({"text": "[图片]"}) == "（发了张图）"
        and A._now_body_v2({"text": ""}) == "（发了一张图/表情）"
        and A._now_body_v2({"text": "你看"}) == "你看")
    chk("9e 当前消息带速写",
        A._now_body_v2({"text": "[图片]", "_vcap": "一只柯基"}) == "（图：一只柯基）"
        and A._now_body_v2({"text": "这啥", "_vcap": "一只柯基"}) == "这啥 （图：一只柯基）")

    # 装配：速写与说明进提示词；识图关着时提示词里没有任何图速写相关内容
    reset_cap()
    with patched(A, PROMPT_V2_FORCE=True, VCAP_FORCE=False):
        msgs, _p = A.build_messages(NullDaemon(history=hist), group_msg("[图片]", "h9", [U1]))
        c_off = msgs[1]["content"]
    with patched(A, PROMPT_V2_FORCE=True, VCAP_FORCE=True, vision_enabled=lambda: True,
                 vision_mode=lambda: "all_prob", _cap_chat_allowed=lambda ct, cid: True,
                 vision_caption_daily_cap=lambda: 600):
        reset_cap()
        m = group_msg("[图片]", "h9", [U1])
        m["_vcap"] = "一只戴帽子的青蛙"
        msgs, _p = A.build_messages(NullDaemon(history=hist), m)
        c_on = msgs[1]["content"]
        chk("9f 识图开着：当前消息的速写进「现在」，说明只出现一次",
            "【现在】" in c_on and "（图：一只戴帽子的青蛙）" in c_on
            and c_on.count(A._VCAP_NOTE) == 1 and "[图片]" not in c_on)
        chk("9g 识图关着：提示词里没有图速写说明", A._VCAP_NOTE not in c_off and "（图：" not in c_off)
        m2 = group_msg("这图什么意思", "h8", [], has_reply=True, reply_has_image=True)
        m2["_vcap"] = "一张梗图"
        c_on2 = A.build_messages(NullDaemon(history=hist), m2)[0][1]["content"]
        chk("9h 引用了带图的消息：不再说「图已附在下方」，改指向上面的速写",
            "内容就是上面「图：…」那句速写" in c_on2 and "下方附的图" not in c_on2
            and "图已附在下方" not in c_on2)

    # ---------- 10. 处理流程分流（真实 process()） ----------
    def run_process(msg, gate_verdict="N", preloaded=None, vcap=True,
                    main_reply='{"say": ["在呢"]}', v2=True, emoji_ok=False, lean=True, vprob=1.0,
                    react_only=True):
        """走真实 process()，记录：同步识图有没有被调、判定看到的内容、确认了几次、
        一共调了几次模型、发没发文字。
        lean=「精简上下文」开关；vprob=「识图概率」（默认 1.0：后台识图下带图的旁观消息
        不被概率拦下，才测得到后面的判定）；react_only=「允许只挂表情不说话」。"""
        vis, gates, gate_sys, mcalls, sent = [], [], [], [], []
        dm = NullDaemon(history=hist)
        dm.send = lambda ct, cid, texts, reply_message_id=None: sent.append(list(texts))
        dm.set_msg_reaction = lambda mid, emoji, on=False: (bool(emoji_ok), "")   # 真实客户端返回 (ok, detail)
        A.PLUGIN_HOST.daemon = dm

        def _vision(ms, urls, daemon=None):
            vis.append(1)
            raise _Stop()

        def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
            mcalls.append(messages[0]["content"][:20])
            if len(messages) == 2 and _gate_head in messages[0]["content"]:
                gates.append(messages[1]["content"])
                gate_sys.append(messages[0]["content"])
                return "m", gate_verdict
            return "m", main_reply

        _gate_head = "的插话判定器"     # 判定提示词的固定片段（人设名在它前面）

        with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=v2, REPLY_JSON_FORCE=v2,
                     VCAP_FORCE=vcap, REACT_ONLY_FORCE=react_only,
                     vision_enabled=lambda: True, vision_mode=lambda: "all_prob",
                     vision_prob=lambda: vprob, call_vision=_vision,
                     call_glm_with_fallback=_chain, chat_anti_interrupt_enabled=lambda: True,
                     lean_ctx_on=lambda: lean,
                     _cap_chat_allowed=lambda ct, cid: True,
                     reaction_allowed=lambda: (True, ["63"])):
            reset_cap()
            if preloaded:
                with A._CAP["lock"]:
                    A._CAP["by_mid"][("group", "555001", msg["message_id"])] = {
                        "texts": preloaded, "ts": time.time()}
            LOG.clear()
            where = "returned"
            try:
                A.process(dm, msg)
            except _Stop:
                where = "vision"
            return dict(vis=vis, gates=gates, gate_sys=gate_sys, acked=dm.acked, logs=list(LOG),
                        where=where, mcalls=len(mcalls), sent=sent)

    r = run_process(group_msg("[图片]", "p1", [U1]), preloaded=["一张猫咪表情包"])
    chk("10a 闲聊触发+图已有描述 → 不再同步识图，交给统一判定",
        r["vis"] == [] and len(r["gates"]) == 1 and r["where"] == "returned"
        and r["acked"] == [7001], (r["vis"], len(r["gates"]), r["acked"]))
    chk("10b 判定看到的是图的描述，不是方括号占位",
        r["gates"] and "（图：一张猫咪表情包）" in r["gates"][0] and "[图片]" not in r["gates"][0])
    chk("10c 记日志：命中描述", any(x.startswith("AGENT_VCAP_HIT") for x in r["logs"]))
    chk("10d 判定前已先登记这条消息（供以后的历史行用）",
        A._caption_get("group", "555001", "p1") is not None)

    # 2026-11-25 改：识图概率的骰子挪到等描述之前。此前概率为 0 时每张闲聊图都要先干等
    # 满 4 秒才发现「不用回」，而主循环是一条一条处理消息的，这几秒会拖慢后面的消息。
    t0 = time.time()
    r = run_process(group_msg("[图片]", "p2", [U1]), vprob=0.0)
    chk("10e 识图概率为 0：带图的闲聊消息不等描述、不调模型，直接沉默",
        r["vis"] == [] and r["gates"] == [] and r["acked"] == [7001]
        and not any("VCAP_MISS" in x for x in r["logs"])
        and any("识图概率 0% 未中，未等待描述" in x for x in r["logs"]),
        (r["vis"], len(r["gates"]), r["logs"][-3:]))
    chk("10f 不等描述：整条消息不到 1 秒处理完（此前要先等满 4 秒）",
        time.time() - t0 < 1.5, round(time.time() - t0, 1))
    chk("10f' 没评论的图照样登记后台描述（供以后的对话用）；没人在等，所以不插队",
        ("group", "555001", "p2") in A._CAP["pending"]
        and not any(j.get("key") == ("group", "555001", "p2") and j.get("front")
                    for j in A._CAP["queue"]))
    t0 = time.time()
    with patched(random, random=lambda: 0.0):         # 必定掷中
        r = run_process(group_msg("[图片]", "p2b", [U1]), vprob=0.3)
    chk("10f'' 掷中了才等描述，等待有上限（不超过 5 秒）",
        time.time() - t0 < 5.5 and any("VCAP_MISS" in x for x in r["logs"]),
        round(time.time() - t0, 1))

    r = run_process(group_msg("看看这张图", "p3", [U1]))
    chk("10g 没描述但话里明确在问这张图 → 仍同步识图（旧路径保留）",
        r["vis"] == [1] and r["where"] == "vision", r["vis"])

    r = run_process(group_msg("这是什么", "p4", [U1]), gate_verdict="Y")
    chk("10h 没描述、话没提图、判定放行 → 判定不跑，按旧路径同步识图",
        r["gates"] == [] and r["vis"] == [1], (len(r["gates"]), r["vis"]))

    r = run_process(group_msg("这是什么", "p5", [U1], trig="at"), preloaded=["一张菜单照片"])
    chk("10i 直接@+图 → 即使有描述也让模型亲眼看图（同步识图）",
        r["vis"] == [1] and r["where"] == "vision", r["vis"])

    r = run_process(group_msg("今天天气不错", "p6", [U1]), vcap=False, vprob=0.0)
    chk("10j 识图开关关着 → 整个后台识图分支不参与（等都不等，直接走旧路径）",
        not any("VCAP" in x for x in r["logs"]) and r["vis"] == []
        and any("VISION_ZERO_SKIP" in x for x in r["logs"]), r["logs"][:4])
    r = run_process(group_msg("哈哈哈", "p7", []), preloaded=None)
    chk("10k 不带图的消息 → 不涉及识图，照常判定", r["vis"] == [] and len(r["gates"]) == 1)
    # 设置页的「识图概率」在后台识图下照样管用（和旧路径同一个骰子、同一个例外）
    r = run_process(group_msg("[图片]", "p10", [U1]), preloaded=["一张梗图"], vprob=0.0)
    chk("10o 后台识图开着 + 识图概率为 0：不评论这张图，也不去取描述",
        r["gates"] == [] and r["mcalls"] == 0 and r["acked"] == [7001]
        and any(x.startswith("AGENT_VISION_PROB_SKIP") for x in r["logs"]), (len(r["gates"]), r["logs"][-2:]))
    r = run_process(group_msg("看看这张图", "p11", [U1]), preloaded=["一张梗图"], vprob=0.0)
    chk("10p 话里明确在问这张图 → 不受识图概率影响，照常交给判定",
        len(r["gates"]) == 1 and r["vis"] == [], (len(r["gates"]), r["vis"]))
    r = run_process(group_msg("哈哈哈", "p12", []), gate_verdict="N", lean=False)
    chk("10q「精简上下文」关着 → 不先判定，一次调用直接决定（和旧闸门同一个前提）",
        r["gates"] == [] and r["mcalls"] == 1 and r["sent"] == [["在呢"]],
        (len(r["gates"]), r["mcalls"], r["sent"]))
    # 「允许只挂表情不说话」默认关：判定里没有这个选项；模型仍答 E 时按接话处理
    r = run_process(group_msg("发了个表情包", "9001", []), gate_verdict="E", react_only=False,
                    main_reply='{"say": ["哈哈这个好玩"]}')
    chk("10r 默认（不允许只挂表情）：判定提示词里没有「回个表情就够」；模型答 E 也按接话处理，照常说话",
        "回个表情就够" not in (r["gate_sys"] or [""])[0] and r["sent"] == [["哈哈这个好玩"]],
        (r["gate_sys"][:1], r["sent"]))
    r = run_process(group_msg("发了个表情包", "9001", []), gate_verdict="E", react_only=True,
                    main_reply='{"say": ["哈哈这个好玩"]}')
    chk("10s 设置页打开后：判定里才有这个选项", "回个表情就够" in (r["gate_sys"] or [""])[0])

    # 只挂表情的回复：表情发出去了就到此为止；没发出去也不补话
    _REACT = '{"want": 1, "say": [], "emoji": 63}'
    r = run_process(group_msg("发了个表情包", "9001", []), gate_verdict="E", main_reply=_REACT, emoji_ok=True)
    chk("10l v2 判定「回个表情就够」+ 模型只挂表情 → 到此为止：不补文字、不进复读重试（共 2 次调用）",
        r["acked"] == [7001] and r["sent"] == [] and r["mcalls"] == 2
        and any("reaction-only" in x and "表情未发出" not in x for x in r["logs"]), (r["acked"], r["sent"], r["mcalls"]))
    r = run_process(group_msg("发了个表情包", "9001", []), gate_verdict="E", main_reply=_REACT, emoji_ok=False)
    chk("10m 表情没发出去（被关掉/接口失败）→ 同样不回，不凭空生成一段话",
        r["acked"] == [7001] and r["sent"] == [] and r["mcalls"] == 2
        and any("reaction-only (表情未发出，本条不回)" in x for x in r["logs"]), (r["sent"], r["mcalls"], r["logs"][-2:]))
    r = run_process(group_msg("发了个表情包", "9001", []), gate_verdict="Y", main_reply='{"say": ["哈哈这也太离谱了"]}')
    chk("10n v2 判定「有话想说」+ 正常回复 → 文字发出去",
        r["sent"] == [["哈哈这也太离谱了"]] and r["acked"] == [7001], (r["sent"], r["logs"][-2:]))

    # ---------- 11. 视觉调用重构后行为不变 ----------
    asks = []


    class _RT(object):
        cooldowns = {}

        def set_current(self, m):
            pass

        def cooldown(self, k, s=None):
            self.cooldowns[k] = time.time() + (s or 300)


    def _scripted(*resp):
        """按顺序回放响应：每项 (status, content, finish)。"""
        it = iter(resp)

        def _glm(model, messages, timeout=40, extra_body=None, key=None):
            asks.append((model, key, dict(extra_body or {}), messages))
            st, content, finish = next(it)
            if st != 200:
                return st, {"error": {"message": content}}
            return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return _glm


    base11 = dict(glm_chat=None, _img_to_datauri=lambda u, daemon=None: "data:image/png;base64,AA",
                  vision_model=lambda: "vm", RT=_RT(), _NO_VISION_MODELS=set(),
                  resolve_thinking=lambda m, t="": ({"thinking": {"type": "disabled"}}, 600),
                  _chain_candidates=lambda: [])
    # ① 不接受关闭思考 → low 档重试；② low 档正文为空且被截断 → 放大预算重试
    base11["glm_chat"] = _scripted(
        (400, "该模型不支持关闭思考", None), (200, "", "length"), (200, "看见图了", "stop"))
    with patched(A, **base11):
        m, text = A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
    chk("11a 不接受关闭思考→low 档；正文为空且截断→放大预算，最后拿到正文",
        text == "看见图了" and len(asks) == 3 and asks[1][2]["thinking"]["level"] == "low"
        and asks[2][2]["max_tokens"] == 8192, [(a[0], a[2]) for a in asks])
    _c0 = asks[0][3][1]["content"]
    chk("11b 图片以本地化后的数据直传，接地指令仍拼在文字后面",
        isinstance(_c0, list) and "必须先看懂图片" in _c0[0]["text"]
        and _c0[1]["image_url"]["url"].startswith("data:image/png;base64,")
        and asks[0][3][0]["content"] == "S", _c0[0]["text"][-20:] if isinstance(_c0, list) else type(_c0))
    asks.clear()
    base11["glm_chat"] = _scripted((200, "直接看到", "stop"))
    base11["RT"] = _RT()
    with patched(A, **base11):
        m, text = A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
        chk("11c 一次成功 → 只调一次、输出预算至少 1024", text == "直接看到" and len(asks) == 1
            and asks[0][2]["max_tokens"] == 1024, asks)
    with patched(A, **dict(base11, _img_to_datauri=lambda u, daemon=None: None)):
        try:
            A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
            raised = ""
        except RuntimeError as e:
            raised = str(e)
    chk("11d 一张图都取不到 → 直接判失败，不调模型", raised == "视觉调用：图片全部获取失败"
        and len(asks) == 1, raised)
    # 显式模型失败：主 Key 限流 → 模型链里给同一个模型绑定的 Key → 才是链上下一个模型。
    # （此前主 Key 一失败就跳过链上同名条目，配置的识图模型等于只认主 Key：2026-11-24
    #   线上 28 次速写全落到了链上别的模型，配置的识图模型一次没上。）
    asks.clear()
    rt = _RT()
    base11["RT"] = rt
    base11["glm_chat"] = _scripted((429, "该模型当前访问量过大，请您稍后再试", None),
                                   (200, "用链上绑定的 Key 看到", "stop"))
    base11["_chain_candidates"] = lambda: [("k2", {"key": "k"}, "vm", 8, 0), ("k9", {"key": "z"}, "m9", 8, 0)]
    with patched(A, **base11):
        m, text = A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
    chk("11e 显式模型主 Key 限流 → 接着用模型链里给它绑定的 Key",
        m == "vm" and text == "用链上绑定的 Key 看到" and len(asks) == 2, (m, text, len(asks)))
    asks.clear()
    _rt11 = _RT()
    _rt11.cooldowns = {}            # 夹具的冷却表是类级别共享的，这条要从干净状态开始
    base11["RT"] = _rt11
    base11["glm_chat"] = _scripted((429, "该模型当前访问量过大，请您稍后再试", None),
                                   (429, "该模型当前访问量过大，请您稍后再试", None),
                                   (200, "链上模型看到", "stop"))
    with patched(A, **base11):
        m, text = A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
    chk("11e' 这个模型所有的 Key 都不行 → 才落到链上下一个模型",
        m == "m9" and text == "链上模型看到" and len(asks) == 3, (m, text, len(asks)))

    # ---------- 12. 识图模型选择（不打回复链的冷却） ----------
    cool_log = {}


    class _RT2(object):
        cooldowns = {}

        def set_current(self, m):
            cool_log["current"] = m


    def _glm2(model, messages, timeout=40, extra_body=None, key=None):
        if model == "glm-4.6v-flash":
            return 404, {"error": {"message": "模型不存在"}}
        if model == "m-big":
            return 200, {"choices": [{"message": {"content": "链上模型看图"}, "finish_reason": "stop"}]}
        return 429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}}


    A._NO_VISION_MODELS = set()
    with patched(A, glm_chat=_glm2, _img_to_datauri=lambda u, daemon=None: "data:image/png;base64,AA",
                 vision_caption_model=lambda: "glm-4.6v-flash",
                 _chain_candidates=lambda: [("k1", {"key": "x"}, "m-flash", 8, 0), ("k2", {"key": "y"}, "m-big", 8, 0)],
                 RT=_RT2()):
        reset_cap()
        t = A._caption_call(NullDaemon(), U1)
        chk("12a 识图模型用不了 → 落到回复链", t == "链上模型看图" and "current" not in cool_log, t)
        chk("12b 冷却记在识图自己的表里，不动回复链；限流的只冷 45 秒、用不了的冷一刻钟",
            A._CAP["cool"].get("main:glm-4.6v-flash", 0) - time.time() > 800
            and 30 < A._CAP["cool"].get("k1:m-flash", 0) - time.time() <= 45
            and not _RT2.cooldowns,
            {k: round(v - time.time()) for k, v in A._CAP["cool"].items()})

    # ---------- 13. daemon：观察记录带字段 / 历史图链不再截到 200 ----------
    src = read_src("qq_daemon.py")
    i = src.index("self._observe.append({")
    seg = src[i:src.index("})", i)]
    chk("13a 观察记录含 message_id 与 image_urls",
        '"message_id": str(item.get("message_id")' in seg and '"image_urls": [str(u) for u in' in seg)
    chk("13b 历史行图链上限提到 600", "str(u)[:600]" in src and "str(u)[:200]" not in src)
    hist_src = src[src.index("def _mem_push"):src.index("def _mem_push") + 2600]
    chk("13c 历史行仍只存前 2 张", "[:2]]" in hist_src)

# ================= 第 4 步：主动消息 / 追聊 v2、停用旧补丁、设置页开关 =================
_real_ljc = A.load_json_const


def _cfg_ljc(reply_cfg):
    """替换 agent 的配置读取：回复配置用给定字典，其余文件照常读（只读）。"""
    def _f(path, default=None):
        if path == A.REPLY_CONFIG_PATH:
            return dict(reply_cfg)
        return _real_ljc(path, {} if default is None else default)
    return _f


HOUR15 = {"hour": 15, "iso": "2026-11-24 15:00", "weekday": "周二", "clock24": "15:00", "clock12": "下午3:00"}


def row(ts, name, text, is_bot=False, qq="10001", **kw):
    r = {"ts": ts, "sender_id": "1" if is_bot else qq, "sender_name": "BOT" if is_bot else name,
         "is_bot": is_bot, "text": text}
    r.update(kw)
    return r


with patched(A, log_usage=lambda *x, **k: None, _log_context_dump=lambda *x, **k: None,
             out=lambda line: LOG.append(str(line)), search_web=lambda q: ([], "none"),
             web_search_enabled=lambda: False, glm_chat=_no_net):
    A.PLUGIN_HOST.on_message_all = lambda msg: None
    A.PLUGIN_HOST.log = lambda line: None

    # ---------- 14. 追聊 v2 ----------
    ok, on, say = A._parse_flag_json('{"nudge": true, "say": "后来去成了吗"}', "nudge")
    chk("14a 结构化判断：要追", ok and on and say == "后来去成了吗", (ok, on, say))
    ok, on, say = A._parse_flag_json('```json\n{"nudge": false, "say": ""}\n```', "nudge")
    chk("14b 不追", ok and not on and say == "", (ok, on, say))
    for _mk in ("[不发]", "[跳过]", "[SKIP]"):
        ok, on, say = A._parse_flag_json(_mk, "nudge")
        chk("14c %s 算合法的不追" % _mk, ok and not on)
    ok, on, say = A._parse_flag_json('{"nudge": "true", "say": ["那后来呢", "去成了吗"]}', "nudge")
    chk("14d 字符串 true / say 是数组 → 拼成一句", ok and on and say == "那后来呢 去成了吗", (ok, on, say))
    chk("14e 不是 JSON → 解析失败（调用方按不追处理）", A._parse_flag_json("我觉得应该问一下", "nudge")[0] is False)
    ok, on, say = A._parse_flag_json('{"nudge": "maybe"}', "nudge")
    chk("14f 布尔值写成别的字符串 → 按不追", ok and not on)
    chk("14g 布尔字段不是布尔 → 解析失败", A._parse_flag_json('{"nudge": 1, "say": "x"}', "nudge")[0] is False)

    class _NudgeDm(NullDaemon):
        def status(self):
            return {}

        def recent_chats(self, within=0):
            return [{"chat_type": "private", "chat_id": "20002"}]

    now = time.time()
    ndm = _NudgeDm(history=[
        row(now - 400, "阿明", "我去趟楼下", qq="20002"),
        row(now - 200, "BOT", "行，顺便帮我带瓶可乐", is_bot=True)])
    c14 = []
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c14.append(ms) or (
            "m", '{"nudge": true, "say": "楼下的事办完了吗"}')):
        t14 = A._nudge_compose_v2(ndm, "private", "20002")
    txt14 = c14[0][0]["content"] + "\n" + c14[0][1]["content"]
    chk("14h 追聊 v2：要追时返回那句话", t14 == "楼下的事办完了吗", t14)
    chk("14i 提示词：只问「话说到一半断了 / 有新进展」，没有分类框架，不找新话头",
        "说到一半" in txt14 and "新进展" in txt14 and "距现在多久" in txt14
        and "先判断对话停在哪一种" not in txt14 and "新话头" not in txt14
        and '"nudge": false' in txt14)
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: ("m", '{"nudge": false, "say": ""}')):
        chk("14j 判断不追 → 空串", A._nudge_compose_v2(ndm, "private", "20002") == "")
    LOG.clear()
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: ("m", "要问一下吗")):
        chk("14k 输出不是 JSON → 不追并记日志",
            A._nudge_compose_v2(ndm, "private", "20002") == ""
            and any(x.startswith("AGENT_NUDGE_JSON_FAIL") for x in LOG), LOG[-2:])
    g14 = []
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: g14.append(ms) or (
            "m", '{"nudge": false}')):
        A._nudge_compose_v2(ndm, "group", "555001")
    chk("14l 群聊版更保守：明说冷场是常态、不@人不找话",
        "冷场是常态" in g14[0][0]["content"] and "不@人" in g14[0][0]["content"])

    def _nudge_run(v2, mode=None):
        """mode=None → 不打桩，读真实的 nudge_mode（配置里没写 = 默认 classic）。"""
        used = []
        A._NUDGE_STATE.clear()
        A._NUDGE_LAST_RUN = 0.0
        _pm = {"nudge_mode": lambda: mode} if mode else {}
        with patched(A, REPLY_V2_FORCE=v2, _nudge_cfg=lambda: (True, 30, 300, 1, False),
                     _in_silent_window=lambda cfg, kind, lt: False,
                     load_json_const=_cfg_ljc({}),
                     _nudge_compose_v2=lambda dm, ct, cid: used.append("v2") or "",
                     _nudge_compose=lambda dm, ct, cid: used.append("v1") or "", **_pm), \
                patched(A.C, tz_now_info=lambda: dict(HOUR15)):
            A._nudge_tick(ndm)
        return used

    chk("14m 回复策略 v2 开 + 追聊风格「克制」→ 走 v2", _nudge_run(True, "restrained") == ["v2"])
    chk("14n 回复策略 v2 关 → 仍走旧版（风格设置不起作用）", _nudge_run(False, "restrained") == ["v1"])
    # 下面几项验的是「配置里没写时的默认值」——必须显式打桩成空配置，不能读
    # 线上真实配置：用户可能已经把风格开关打开了，那样测的就不是默认值。
    with patched(A, load_json_const=_cfg_ljc({})):
        _def_nudge = A.nudge_mode()
    chk("14o 回复策略 v2 开 + 默认风格（经典）→ 仍走旧版：风格默认保持从前的做法",
        _nudge_run(True) == ["v1"] and _def_nudge == "classic", _def_nudge)
    with patched(A, load_json_const=_cfg_ljc({"nudge_mode": "whatever"})):
        _bad_mode = A.nudge_mode()
    chk("14p 配置里的值不合法 → 回到默认", _bad_mode == "classic", _bad_mode)
    with patched(A, load_json_const=_cfg_ljc({"nudge_mode": "restrained"})):
        chk("14q 设置页选了「克制」→ 生效", A.nudge_mode() == "restrained")
    with patched(A, load_json_const=_cfg_ljc({})):
        chk("14r 打桩已还原（又读到默认值）", A.nudge_mode() == "classic", A.nudge_mode())

    # ---------- 15. 主动消息 v2 ----------
    ok, speak, say, kind, anchor = A._parse_proactive_json('{"speak": false}')
    chk("15a 不发", ok and not speak and say == [], (ok, speak, say))
    ok, speak, say, kind, anchor = A._parse_proactive_json(
        '```json\n{"speak": true, "kind": "接话", "anchor": "小张问有没有人打过那个游戏", "say": ["我打过，后面特别刀"]}\n```')
    chk("15b 发：取出要说的话、类型、由头", ok and speak and say == ["我打过，后面特别刀"]
        and kind == "接话" and anchor == "小张问有没有人打过那个游戏", (ok, speak, say, kind, anchor))
    ok, speak, say, kind, anchor = A._parse_proactive_json('{"speak": true, "say": []}')
    chk("15c 说要发但没给内容 → 不发", ok and not speak and say == [])
    ok, speak, say, kind, anchor = A._parse_proactive_json('{"speak": true, "say": "一\\n二  三", "kind": "' + "x" * 20 + '"}')
    chk("15d say 是字符串、带换行 → 压成一行；类型限长", ok and speak and say == ["一 二 三"] and len(kind) == 8, (ok, say))
    ok, speak, say, kind, anchor = A._parse_proactive_json('{"speak": true,\n "say": "我打过，\n后面特别刀"}')
    chk("15d2 字符串里直接换行（不合法 JSON）→ 容错解析成功", ok and speak and say == ["我打过， 后面特别刀"], (ok, say))
    ok, on, say = A._parse_flag_json('{"nudge": true, "say": "后来\n去成了吗"}', "nudge")
    chk("15d3 追聊判断同样容错", ok and on and say == "后来 去成了吗", (ok, on, say))
    ok, o = A._parse_reply_json('{"say": ["第一行\n第二行"], "want": 2}')
    chk("15d4 回复的结构化解析同样容错", ok and o["say"] == ["第一行\n第二行"], o)
    chk("15d5 容错不改变合法 JSON 的结果", A._loads_lenient('{"a": "x\\ny", "b": [1, 2]}') == {"a": "x\ny", "b": [1, 2]})
    chk("15d6 真解析不了的仍然失败", A._loads_lenient('{"a": ') is None
        and A._parse_proactive_json('{"speak": true, "say": "a')[0] is False
        and A._parse_flag_json('{"nudge": true, "say": "a', "nudge")[0] is False)
    ok, speak, say, kind, anchor = A._parse_proactive_json('{"speak": true, "say": ["a", "b", "c"]}')
    chk("15e 最多两条", speak and len(say) == 2, say)
    for _mk in ("[SKIP]", "[跳过]"):
        ok, speak, say, kind, anchor = A._parse_proactive_json(_mk)
        chk("15f %s 算不发" % _mk, ok and not speak)
    chk("15g 不是 JSON → 解析失败（不发）", A._parse_proactive_json("大家晚上好呀")[0] is False)

    pdm = NullDaemon(history=hist)
    pmsgs, _pn = A._proactive_compose_v2(pdm, "group", "555001")
    ptxt = pmsgs[0]["content"] + "\n" + pmsgs[1]["content"]
    chk("15h 提示词：由头只认三种、十次里九次不该发、结构化输出、历史带相对时间",
        "由头只认三种" in ptxt and "十次里九次都不该发" in ptxt and '"speak": false' in ptxt
        and "距现在多久" in pmsgs[1]["content"] and "不算由头" in ptxt)
    chk("15i「有具体由头才发」模式的提示词不再是「主动开个话头」", "开个话头" not in ptxt, ptxt[-120:])
    with patched(A, _weather_proactive_bridge=lambda: "【所在地天气】晴 12 度"):
        _wm = A._proactive_compose_v2(pdm, "group", "555001")[0]
    chk("15i' 天气插件给的素材行照旧带上（和旧版一样，不因为换模式而丢功能）",
        "【所在地天气】晴 12 度" in _wm[1]["content"], _wm[1]["content"][-80:])

    def _pro_run(v2, reply, mode="reason"):
        """mode=None → 不打桩，读真实的 proactive_mode（配置里没写 = 默认 open）。"""
        sent, used = [], []
        dm = NullDaemon(history=[])
        dm.send = lambda ct, cid, texts, reply_message_id=None: sent.append(list(texts))
        dm.status = lambda: {}
        A._proactive_state.clear()
        A._proactive_state.update({"groups": {"last_attempt": 0.0, "last_sent": 0.0},
                                   "private": {"last_attempt": 0.0, "last_sent": 0.0}})
        A._proactive_send_times.clear()
        LOG.clear()
        _pm = {"proactive_mode": lambda: mode} if mode else {}
        with patched(A, REPLY_V2_FORCE=v2, _proactive_targets=lambda dm, cfg: [("group", "555001")],
                     _proactive_pick=lambda cfg, kind, pool, now, daemon=None: ("group", "555001"),
                     _proactive_cap_left=lambda cfg, now: 1,
                     _proactive_state_save=lambda: None,      # 不写真实的 proactive_state.json
                     _dedup_reply=lambda dm, m, texts: (list(texts), 0),
                     load_json_const=_cfg_ljc({"proactive_groups_enabled": True}),
                     call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: used.append(ms[0]["content"][:12]) or ("m", reply),
                     _proactive_compose=lambda dm, ct, cid: (used.append("v1") or [
                         {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "p"), **_pm):
            A._proactive_tick(dm)
        return sent, used, [x for x in LOG if x.startswith("AGENT_PROACTIVE")]

    sent, used, logs = _pro_run(True, '{"speak": false}')
    chk("15j「有具体由头才发」：没有由头 → 不发", sent == [] and used and used[0] != "v1"
        and any("没有具体由头" in x for x in logs), (sent, used, logs))
    sent, used, logs = _pro_run(True, '{"speak": true, "kind": "接话", "anchor": "小张问有没有人打过", "say": ["我打过"]}')
    chk("15k「有具体由头才发」：有由头 → 发出那句话，并记下由头", sent == [["我打过"]]
        and any(x.startswith("AGENT_PROACTIVE_REASON") and "接话" in x for x in logs), (sent, logs))
    sent, used, logs = _pro_run(True, "大家晚上好呀")
    chk("15l「有具体由头才发」：输出不是 JSON → 不发并记日志", sent == []
        and any(x.startswith("AGENT_PROACTIVE_JSON_FAIL") for x in logs), (sent, logs))
    sent, used, logs = _pro_run(False, "大家晚上好呀")
    chk("15m 回复策略关着 → 走旧版主动消息（原文照发）", used[:1] == ["v1"] and sent == [["大家晚上好呀"]], (used, sent))
    sent, used, logs = _pro_run(True, "大家晚上好呀", mode=None)
    with patched(A, load_json_const=_cfg_ljc({})):
        _def_pm = A.proactive_mode()
    chk("15m' 回复策略 v2 开 + 默认风格（随缘找话头）→ 仍走旧版：风格默认保持从前的做法",
        used[:1] == ["v1"] and sent == [["大家晚上好呀"]] and _def_pm == "open", (used, sent, _def_pm))
    with patched(A, load_json_const=_cfg_ljc({"proactive_mode": "nope"})):
        _bm = A.proactive_mode()
    chk("15m'' 主动消息风格取值不合法 → 回到默认", _bm == "open", _bm)
    with patched(A, load_json_const=_cfg_ljc({"proactive_mode": "reason"})):
        chk("15m''' 设置页选了「有具体由头才发」→ 生效", A.proactive_mode() == "reason")

    src_agent = read_src("qq_bot_agent.py")
    chk("15n v2 下停用「闲聊超长压缩重试」；旧路径原样保留",
        'if not reply_v2_on() and _cml and msg.get("trigger_reason")' in src_agent)
    chk("15o 旧补丁保留：同话题提示、语义去重、连贯性守卫都还在",
        "_same_topic_note(msg)" in src_agent and "_semantic_dup_check(msg, s, _recent_bot)" in src_agent
        and "_coherence_issue(texts)" in src_agent)

    # ---------- 17. 风格不写死：长度、必须回应、表情选项、提示词内容 ----------
    _chat17 = group_msg("哈哈哈", "9101", [])
    _at17 = group_msg("在吗", "9102", [], trig="at")
    _SU = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    with patched(A, reaction_allowed=lambda: (True, ["63"]), chat_max_len=lambda: 80):
        _r_chat = A.json_rule_text(_chat17)
        _r_at = A.json_rule_text(_at17)
        _cap_chat, _cap_at = A._len_cap_for(_chat17), A._len_cap_for(_at17)
    chk("17a 闲聊回复：长度要求来自设置页的「闲聊回复长度上限」",
        "80 字以内" in _r_chat and _cap_chat == 80, _r_chat[-80:])
    chk("17b 直接@：不受长度限制（和从前一致）", "不设上限" in _r_at and _cap_at == 0)
    chk("17c 不再写死「十来个字 / 不超过两句话」",
        "十来个字" not in _r_chat and "不超过两句话" not in _r_chat)
    with patched(A, reaction_allowed=lambda: (True, ["63"]), chat_max_len=lambda: 0):
        chk("17d 上限设为 0（不限）→ 闲聊回复也不限制长度",
            "不设上限" in A.json_rule_text(_chat17) and A._len_cap_for(_chat17) == 0)
    with patched(A, reaction_allowed=lambda: (False, [])):
        _r_off = A.json_rule_text(_chat17)
        _g_off = A._gate_system("智谱娘")
    chk("17e 表情回应关着：格式说明里没有「只挂表情」这一档",
        "1=只给对方这条消息挂个表情" not in _r_off and "固定填 null" in _r_off
        and "0、2 或 3" in _r_off, _r_off[:150])
    chk("17f 表情回应关着：判定提示词里没有「回个表情就够」这个选项",
        "回个表情就够" not in _g_off and "只输出 N 或 Y" in _g_off, _g_off[-30:])
    with patched(A, reaction_allowed=lambda: (True, ["63"]), REACT_ONLY_FORCE=True):
        _g_on = A._gate_system("智谱娘")
        _r_e = A.json_rule_text(_chat17)
    chk("17g 表情回应开着 + 允许只挂表情：判定才有「回个表情就够」",
        "回个表情就够" in _g_on and "N、E 或 Y" in _g_on)
    chk("17g' 允许只挂表情时格式说明里才有 want=1", "1=只给对方这条消息挂个表情" in _r_e)
    with patched(A, reaction_allowed=lambda: (True, ["63"]), REACT_ONLY_FORCE=False):
        _g_ro = A._gate_system("智谱娘")
        _r_ro = A.json_rule_text(_chat17)
        # 默认值同样要打桩成空配置：线上可能已被用户打开
        with patched(A, load_json_const=_cfg_ljc({})):
            _def_ro = A.react_only_on()
    chk("17g'' 默认（表情回应开着、但不允许只挂表情）：判定里没有这个选项，格式里没有 want=1，"
        "并写明表情只作点缀——和从前一样总是说话",
        "回个表情就够" not in _g_ro and "1=只给对方这条消息挂个表情" not in _r_ro
        and "不要只挂表情不说话" in _r_ro and _def_ro is False, _r_ro[:150])
    with patched(A, reaction_allowed=lambda: (False, []),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: ("m", "E")):
        chk("17h 表情回应关着时模型仍答了 E → 按接话处理，不白白沉默",
            A._reply_gate_v2(NullDaemon(), _chat17, list(_SU), "智谱娘") == "Y")
    # 默认下旁观回复只挂了表情 → 请它说一句（重试一次）；重试仍是表情就保持原样，不硬凑
    c17 = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}', '{"say": ["哈哈这个好玩"]}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        LOG.clear()
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="Y")
    chk("17h' 默认：只挂了表情 → 重试一次并采用文字回复",
        A.parse_reply(_r)[0] == ["哈哈这个好玩"] and len(c17) == 2
        and any(x.startswith("AGENT_REACT_ONLY_RETRY") for x in LOG), (len(c17), LOG[-1:]))
    c17[:] = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}', '{"want": 1, "say": [], "emoji": 63}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="Y")
    chk("17h'' 重试后还是只挂表情 → 不再重试（保持原样，由后面的流程处理）",
        len(c17) == 2 and A._REACT_MARK_RE.search(_r) is not None, (len(c17), _r))
    c17[:] = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=True,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="E")
    chk("17h''' 允许只挂表情时 → 不重试", len(c17) == 1, len(c17))

    # 超长兜底：设置页的上限在 v2 下照样管用（按结构化输出重试，不走旧的文本协议重试）
    _LONG = '{"say": ["' + "很长" * 60 + '"]}'
    c17 = []
    _seq = iter([_LONG, '{"say": ["短的了"]}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 direct_must_reply=lambda: True,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        LOG.clear()
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU])
    chk("17i 闲聊回复超过上限 → 带上限要求重试一次，采用压缩后的",
        A.parse_reply(_r)[0] == ["短的了"] and len(c17) == 2 and "40 字" in c17[1][1]["content"]
        and any(x.startswith("AGENT_CHAT_LEN_SQUEEZE") for x in LOG), (len(c17), LOG[-1:]))
    c17[:] = []
    _seq = iter([_LONG])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _at17, [dict(x) for x in _SU])
    chk("17j 直接@的长回复不触发压缩重试", len(c17) == 1 and "很长" in _r, len(c17))
    c17[:] = []
    _seq = iter([_LONG])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 0,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU])
    chk("17k 上限为 0 → 再长也不重试", len(c17) == 1 and "很长" in _r, len(c17))
    c17[:] = []
    _seq = iter([_LONG, "```json\n{\"say\": [], \"want\": 0}\n```"])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200, **kwargs: c17.append(ms) or ("m", next(_seq))):
        LOG.clear()
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU])
    chk("17l 压缩后变成了「不回」→ 不采纳，保留原来的长回复（宁可长也不丢回复）",
        len(c17) == 2 and "很长" in _r and not any("SQUEEZE msg_id" in x for x in LOG), (_r[:20], LOG[-1:]))

    # 「直接@必须回应」是开关
    calls.clear()
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, direct_must_reply=lambda: False,
                 call_glm_with_fallback=_always('```json\n{"say": [], "want": 0}\n```')):
        m, r = A._generate_reply_v2(dm, group_msg("在吗", "m1", [], trig="at"), SU)
        chk("17m 关掉「直接@必须回应」→ 模型沉默就沉默，不重试", len(calls) == 1 and r == "[跳过]", (len(calls), r))

    # 「向群友的消息长度看齐」默认关；设置页打开后才给
    _hist10 = [row(time.time() - i * 30, "小张%d" % i, "好的没问题%d" % i, qq=str(20000 + i)) for i in range(10)]
    with patched(A, load_json_const=_cfg_ljc({})):
        chk("17n 默认不给「向群友长度看齐」提示", A._group_len_hint(_hist10) == "" and not A.len_follow_group())
    with patched(A, load_json_const=_cfg_ljc({"len_follow_group": True})):
        chk("17o 设置页打开后才给", "个字左右" in A._group_len_hint(_hist10), A._group_len_hint(_hist10))

    # 提示词内容：自进化风格观察每次都带（不再只给直接回复）；热点清单不再按相关性过滤
    chk("17p 热点相关性过滤已移除（机器人知道的热点和从前一样）", not hasattr(A, "_aware_filter_hot"))
    # 显式给一个人设桩：数据目录隔离后没有真实激活人设，而自进化注入只对
    # 「有激活人设」的调用生效（active_name 为空时 evolved_notes 根本不会被调）
    with patched(A, PROMPT_V2_FORCE=True, VCAP_FORCE=False,
                 load_persona_collection=lambda: ({"active": "冒烟人设", "global": ""}, "冒烟人设"),
                 persona_for=lambda ct, cid, sid=None: ("冒烟人设", "smoke"),
                 evolved_notes=lambda name: {"enabled": True, "notes": ["观察甲", "观察乙"]}):
        _bystander = A.build_messages(NullDaemon(history=hist), group_msg("哈哈哈", "h20", []))[0][0]["content"]
        _direct = A.build_messages(NullDaemon(history=hist), group_msg("在吗", "h21", [], trig="at"))[0][0]["content"]
    chk("17q 旁观消息的提示词也带自进化风格观察", "观察甲" in _bystander and "观察乙" in _bystander)
    chk("17r 直接回复同样带", "观察甲" in _direct)

    # 历史窗口：默认值与取值范围
    with patched(A, load_json_const=_cfg_ljc({})):
        chk("17s 历史窗口默认 50 行 / 5000 字", A.hist_lines_v2() == 50 and A.hist_budget_v2() == 5000)
    with patched(A, load_json_const=_cfg_ljc({"hist_lines": 9999, "hist_budget_chars": 10})):
        chk("17t 超出范围被夹住", A.hist_lines_v2() == 120 and A.hist_budget_v2() == 1000,
            (A.hist_lines_v2(), A.hist_budget_v2()))

    # ---------- 18. 设置页开关（保存路径指向临时文件，不碰真实配置） ----------
    import io
    import shutil
    import tempfile
    import qq_admin_ui as U

    _tmpd = tempfile.mkdtemp(prefix="qqbot_smoke_rc_")
    _real_rc = U.REPLY_CONFIG_PATH
    U.REPLY_CONFIG_PATH = os.path.join(_tmpd, "reply_config.json")

    def _ui_call(method, bodyq=None):
        h = U.UiHandler.__new__(U.UiHandler)
        h.path = "/api/reply-config"
        raw = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
        h.headers = {"Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        replies = []
        h._reply = lambda code, obj: replies.append((code, obj))
        h._authed = lambda u, q: True
        getattr(h, method)()
        return replies

    def _saved():
        return json.loads(pathlib.Path(U.REPLY_CONFIG_PATH).read_text(encoding="utf-8"))

    try:
        r = _ui_call("do_POST", {"prompt_v2": 1, "reply_v2": True, "reply_json": 0, "vision_caption": True,
                                 "vision_caption_model": " glm-4.6v-flash ", "vision_caption_daily_cap": 300,
                                 "direct_must_reply": 0, "len_follow_group": True,
                                 "proactive_mode": "reason", "nudge_mode": "restrained",
                                 "hist_lines": 80, "hist_budget_chars": 8000})
        s = _saved() if pathlib.Path(U.REPLY_CONFIG_PATH).exists() else {}
        chk("18a 四个开关 + 识图模型 + 每日上限都能保存",
            bool(r) and r[0][0] == 200 and s.get("prompt_v2") is True and s.get("reply_v2") is True
            and s.get("reply_json") is False and s.get("vision_caption") is True
            and s.get("vision_caption_model") == "glm-4.6v-flash" and s.get("vision_caption_daily_cap") == 300, s)
        chk("18a' 风格选项都能保存：必须回应、向群友长度看齐、主动消息风格、追聊风格、历史窗口",
            s.get("direct_must_reply") is False and s.get("len_follow_group") is True
            and s.get("proactive_mode") == "reason" and s.get("nudge_mode") == "restrained"
            and s.get("hist_lines") == 80 and s.get("hist_budget_chars") == 8000, s)
        r = _ui_call("do_POST", {"proactive_mode": "随便", "nudge_mode": ""})
        chk("18a'' 风格取值不合法 → 回到默认，不报错", bool(r) and r[0][0] == 200
            and _saved().get("proactive_mode") == "open" and _saved().get("nudge_mode") == "classic", r[:1])
        _ui_call("do_POST", {"hist_lines": 5, "hist_budget_chars": 999999})
        chk("18a'' 历史窗口超出范围 → 夹到 10 / 20000",
            _saved().get("hist_lines") == 10 and _saved().get("hist_budget_chars") == 20000, _saved())
        r = _ui_call("do_POST", {"hist_lines": "x"})
        chk("18a''' 历史窗口不是整数 → 400", bool(r) and r[0][0] == 400, r[:1])
        _ui_call("do_POST", {"vision_caption_daily_cap": 99999})
        chk("18b 上限夹到 5000", _saved().get("vision_caption_daily_cap") == 5000)
        r = _ui_call("do_POST", {"vision_caption_daily_cap": "abc"})
        chk("18c 上限不是整数 → 400", bool(r) and r[0][0] == 400, r[:1])
        r = _ui_call("do_POST", {"vision_caption_model": 'a"b{c}'})
        chk("18d 模型名去掉引号和花括号", _saved().get("vision_caption_model") == "abc", _saved().get("vision_caption_model"))
        r = _ui_call("do_POST", {"vision_caption_model": "   "})
        chk("18e 模型名留空 → 存空串（不指定，按模型链找能看图的；不再回落到厂商型号）",
            _saved().get("vision_caption_model") == "")
        _ui_call("do_POST", {"chat_probability": 0.2, "prompt_v2": False})
        s = _saved()
        chk("18f 保存其它设置时新开关不丢；再点一次能关掉",
            s.get("prompt_v2") is False and s.get("reply_v2") is True and s.get("vision_caption") is True
            and s.get("chat_probability") == 0.2, s)
        r = _ui_call("do_GET")
        g = r[0][1] if r else {}
        chk("18g GET 返回新开关，取值与保存的一致",
            bool(r) and r[0][0] == 200 and g.get("prompt_v2") is False and g.get("reply_v2") is True
            and g.get("vision_caption_daily_cap") == 5000
            and g.get("direct_must_reply") is False and g.get("len_follow_group") is True
            and g.get("proactive_mode") == "open" and g.get("nudge_mode") == "classic"
            and g.get("hist_lines") == 10 and g.get("hist_budget_chars") == 20000,
            {k: g.get(k) for k in ("prompt_v2", "reply_v2", "reply_json", "vision_caption",
                                   "vision_caption_model", "vision_caption_daily_cap", "direct_must_reply",
                                   "len_follow_group", "proactive_mode", "nudge_mode", "hist_lines",
                                   "hist_budget_chars")} if isinstance(g, dict) else r[:1])
        # 配置里一项都没写时，GET 给出的是默认值（= 从前的做法，「直接@必须回应」除外）
        pathlib.Path(U.REPLY_CONFIG_PATH).unlink(missing_ok=True)
        r = _ui_call("do_GET")
        g = r[0][1] if r else {}
        chk("18g' 配置里没写的键：GET 给默认值（风格默认保持从前的做法）",
            g.get("prompt_v2") is True and g.get("direct_must_reply") is True
            and g.get("len_follow_group") is False and g.get("proactive_mode") == "open"
            and g.get("nudge_mode") == "classic" and g.get("hist_lines") == 50
            and g.get("hist_budget_chars") == 5000 and g.get("vision_caption") is False,
            {k: g.get(k) for k in ("prompt_v2", "direct_must_reply", "len_follow_group", "proactive_mode",
                                   "nudge_mode", "hist_lines", "hist_budget_chars", "vision_caption")})
    finally:
        U.REPLY_CONFIG_PATH = _real_rc
        shutil.rmtree(_tmpd, ignore_errors=True)
    chk("18h 真实的回复配置没有被这组测试改动", os.path.realpath(U.REPLY_CONFIG_PATH) == os.path.realpath(_real_rc))
    src_ui = read_src("qq_admin_ui.py")
    chk("18i 设置页有全部开关（含「直接@必须回应」「向群友长度看齐」）", all(("{%s:this.checked?1:0}" % k) in src_ui
                                   for k in C_KEYS) if (C_KEYS := tuple(A.C.ARCH_V2_BOOL_KEYS)) else False,
        tuple(A.C.ARCH_V2_BOOL_KEYS))
    chk("18i' 设置页有风格下拉与历史窗口输入框",
        all(k in src_ui for k in ("{proactive_mode:this.value}", "{nudge_mode:this.value}",
                                  "{hist_lines:parseInt(this.value)||50}", "{hist_budget_chars:parseInt(this.value)||5000}")))
    # 识图模型/每日上限这两项读的是配置（用户可能改过），只在空配置下才等于默认值
    with patched(A, load_json_const=_cfg_ljc({})):
        _def_cap = A.vision_caption_daily_cap()
        _def_vm = A.vision_caption_model()
    chk("18j 默认值只有一处：agent 与设置页都读 qq_common.ARCH_V2_DEFAULTS",
        A.PROMPT_V2_DEFAULT is A.C.ARCH_V2_DEFAULTS["prompt_v2"]
        and A.REPLY_V2_DEFAULT is A.C.ARCH_V2_DEFAULTS["reply_v2"]
        and A.REPLY_JSON_DEFAULT is A.C.ARCH_V2_DEFAULTS["reply_json"]
        and A.VCAP_DEFAULT is A.C.ARCH_V2_DEFAULTS["vision_caption"]
        and _def_vm == A.C.ARCH_V2_DEFAULTS["vision_caption_model"]
        and _def_cap == A.C.ARCH_V2_DEFAULTS["vision_caption_daily_cap"], (_def_vm, _def_cap))
    _d = A.C.ARCH_V2_DEFAULTS
    chk("18k 发布默认值：提示词 / 回复策略 / 结构化输出开，后台识图关",
        _d["prompt_v2"] is True and _d["reply_v2"] is True and _d["reply_json"] is True
        and _d["vision_caption"] is False, {k: _d[k] for k in A.C.ARCH_V2_BOOL_KEYS})
    chk("18k' 风格类的默认值 = 从前的做法（只有「直接@必须回应」默认开）",
        _d["len_follow_group"] is False and _d["proactive_mode"] == "open"
        and _d["nudge_mode"] == "classic" and _d["direct_must_reply"] is True, _d)
    _no_force = dict(PROMPT_V2_FORCE=None, REPLY_V2_FORCE=None, REPLY_JSON_FORCE=None, VCAP_FORCE=None,
                     vision_enabled=lambda: True, vision_mode=lambda: "all_prob")
    with patched(A, load_json_const=_cfg_ljc({}), **_no_force):
        chk("18l 配置里一项都没写 → 按默认值生效", A.prompt_v2_on() is True and A.reply_v2_on() is True
            and A.reply_json_on() is True and A.vision_caption_on() is False)
    with patched(A, load_json_const=_cfg_ljc({"vision_caption": True}), **_no_force):
        chk("18m 在设置页打开后台识图 → 生效", A.vision_caption_on() is True)
    with patched(A, load_json_const=_cfg_ljc({"prompt_v2": False, "reply_json": False, "reply_v2": False}), **_no_force):
        chk("18n 关掉任一项 → 这一项回到从前，其余照旧",
            A.prompt_v2_on() is False and A.reply_v2_on() is False and A.reply_json_on() is False)

    # ---------- 19. 识图：速写不外泄、识图用谁、对话链归对话链（2026-11-24 事故与次日复审） ----------
    # 19a–19b：2026-11-24 23:23 实测，整段「（图：…）」速写被当成回复发进了群
    for src_txt, want in [
        ("(图：东方灵梦Q版表情包：黑发大红蝴蝶结女孩半睁红眼啃西瓜，一脸不爽)", []),
        ("（图：一只猫）", []),
        ("哈哈这个好玩(图：柴犬表情包)", ["哈哈这个好玩"]),
        ("哈哈(图：一只猫(橘色)趴在桌上)这个好玩", ["哈哈这个好玩"]),
        ("（图：猫）笑死（图：狗（吐舌头（真的很可爱）））了", ["笑死了"]),
        ("前半（图：没有闭合括号的内容", ["前半（图：没有闭合括号的内容"]),
        ("正常说话（图里那只猫好胖）", ["正常说话（图里那只猫好胖）"]),
        ("这小拳头举起来是在给自己打气还是在憋着要揍人啊",
         ["这小拳头举起来是在给自己打气还是在憋着要揍人啊"]),
    ]:
        _p19 = A.parse_reply(src_txt)[0]
        chk("19a 识图速写不外泄：%r" % src_txt[:20], _p19 == want, _p19)
    _code19 = "def f():\n    return (图：x) 1\n    pass"
    chk("19a' 只剥速写和它紧挨着的空白，代码缩进原样保留",
        A._strip_vcap_leak(_code19) == "def f():\n    return 1\n    pass",
        repr(A._strip_vcap_leak(_code19)))
    chk("19b 提示词里的速写说明写了「别把这个写法带到回复里」",
        "别把「（图：…）」这几个字或它的写法带到回复里" in A._VCAP_NOTE)

    # 19c：对话链就是对话链。模型链里配了谁来对话就用谁，不按名字另加禁令——
    # 「设置为识图的是识图的，设置为对话的是对话的」是用户定的原则（2026-11-25）。
    _calls19 = []
    with patched(A, _chain_candidates=lambda: [("k1", {"key": "x"}, "glm-4v-flash", 4, 0),
                                               ("k3", {"key": "y"}, "deepseek-chat", 4, 0)],
                 glm_chat=lambda model, msgs, key=None, extra_body=None: (
                     _calls19.append(model) or (200, {"choices": [{"message": {"content": "ok"}}]}))):
        A.call_glm_with_fallback([{"role": "system", "content": "s"},
                                  {"role": "user", "content": "u"}])
    chk("19c 对话链不按模型名字跳过：链里排第一的是视觉模型也照常用",
        _calls19 == ["glm-4v-flash"], _calls19)
    chk("19c' 「按名字判断视觉模型」的函数已经删掉", not hasattr(A, "_looks_vision_model"))

    # 19d：识图用谁。「识图模型」只填模型名，用哪把 Key 靠模型链里给它的绑定。
    _chain19 = [("main", {"key": "M"}, "zz-flagship", 4, 0),
                ("k1", {"key": "A"}, "zzv-flash", 8, 0),
                ("k3", {"key": "C"}, "zz-text", 4, 0)]
    _names = lambda cs: [(c[0], c[2]) for c in cs]
    with patched(A, _chain_candidates=lambda: list(_chain19), econ_on=lambda: False):
        _vc = _names(A._vision_candidates("zzv-flash"))
        _vc2 = _names(A._vision_candidates(""))
    chk("19d 识图候选：配置的识图模型先用主 Key、再用链里给它绑定的 Key，然后才是链上其它模型",
        _vc == [("main", "zzv-flash"), ("k1", "zzv-flash"),
                ("main", "zz-flagship"), ("k3", "zz-text")], _vc)
    chk("19d' 没配识图模型 → 只按模型链", _vc2 == [("main", "zz-flagship"), ("k1", "zzv-flash"), ("k3", "zz-text")], _vc2)
    with patched(A, _chain_candidates=lambda: [("main", {"key": "M"}, "zzv-flash", 4, 0),
                                               ("k1", {"key": "A"}, "zzv-flash", 4, 0)],
                 econ_on=lambda: False):
        _vc3 = _names(A._vision_candidates("zzv-flash"))
    chk("19d'' 链里已经用主 Key 列了这个模型 → 不重复加一次主 Key",
        _vc3 == [("main", "zzv-flash"), ("k1", "zzv-flash")], _vc3)
    with patched(A, _chain_candidates=lambda: list(_chain19), econ_on=lambda: True):
        _vc4 = _names(A._vision_candidates("zzv-flash"))
    chk("19d''' 高峰降耗：链上兜底照旧跳过链首，配置的识图模型不受影响",
        _vc4 == [("main", "zzv-flash"), ("k1", "zzv-flash"), ("k3", "zz-text")], _vc4)

    # 19e：后台速写真的会用上链里绑定的 Key。
    # 2026-11-24 线上：主 Key 视觉调用 429，链里给识图模型绑了能用的 Key 却被跳过，
    # 28 次速写全部落到链上别的模型，配置的识图模型一次没上。
    _tried19 = []

    def _va19(m, kv, vmsgs, th, mt):
        _tried19.append((m, (kv or {}).get("key")))
        if m == "zzv-flash" and (kv or {}).get("key") == "A":
            return 200, {"choices": [{"message": {"content": "一只橘猫"}}], "usage": {}}
        return 429, {"error": {"message": "套餐已到期"}}
    reset_cap()
    with patched(A, _img_to_datauri=lambda u, d: "data:image/png;base64,AAAA",
                 vision_caption_model=lambda: "zzv-flash",
                 _chain_candidates=lambda: [("main", {"key": "M"}, "zzv-flash", 4, 0),
                                            ("k1", {"key": "A"}, "zzv-flash", 4, 0)],
                 _vision_attempt=_va19, econ_on=lambda: False):
        _txt19 = A._caption_call(NullDaemon(), U1)
    chk("19e 后台速写：主 Key 不行时，用模型链里给识图模型绑定的 Key",
        _txt19 == "一只橘猫" and _tried19 == [("zzv-flash", "M"), ("zzv-flash", "A")], (_txt19, _tried19))
    # 19f：同步识图（@/回复时）同一个道理
    _tried19b = []

    def _va19b(m, kv, vmsgs, th, mt):
        _tried19b.append((m, (kv or {}).get("key")))
        if (kv or {}).get("key") == "A":
            return 200, {"choices": [{"message": {"content": "看见了"}}], "usage": {}}
        return 429, {"error": {"message": "busy"}}
    with patched(A, _img_to_datauri=lambda u, d: "data:image/png;base64,AAAA",
                 vision_model=lambda: "zzv-flash",
                 _chain_candidates=lambda: [("main", {"key": "M"}, "zzv-flash", 4, 0),
                                            ("k1", {"key": "A"}, "zzv-flash", 4, 0),
                                            ("k3", {"key": "C"}, "zz-text", 4, 0)],
                 _vision_attempt=_va19b, resolve_thinking=lambda m, t: ({"thinking": {"type": "disabled"}}, 300),
                 econ_on=lambda: False), patched(A.RT, cooldowns={}):
        _mv19, _rv19 = A.call_vision([{"role": "system", "content": "s"},
                                      {"role": "user", "content": "看看这张图"}], [U1])
    chk("19f 同步识图：显式配的视觉模型同样能用上链里绑定的 Key",
        (_mv19, _rv19) == ("zzv-flash", "看见了")
        and _tried19b == [("zzv-flash", "M"), ("zzv-flash", "A")], (_mv19, _rv19, _tried19b))

    # 19g：结构化输出的解析。2026-11-24 事故的起点是这条单引号输出被判成解析失败、
    # 多花一次重试，重试落到了更弱的模型上——它本来是个完全正常的回复。
    ok, o = A._parse_reply_json('{"want": 1, "say": [\'😂\'], "emoji": null, "memory": [], '
                                '"wait": false, "voice": false, "sticker": null, "actions": []}')
    chk("19g 单引号写法直接解析，不再触发重试", ok and o["want"] == 1 and o["say"] == ["😂"], (ok, o))
    ok, o = A._parse_reply_json("{'want': 2, 'say': ['I like it', \"it's fine\"], 'emoji': None, "
                                "'memory': [], 'wait': False, 'voice': False, 'sticker': None, 'actions': []}")
    chk("19g' Python 字面量写法也能解析，字符串里的内容原样保留",
        ok and o["say"] == ["I like it", "it's fine"], (ok, o))
    ok, o = A._parse_reply_json('{"want": 2, "say": ["this is true and null"], "emoji": null,}')
    chk("19g'' 结尾多一个逗号也行；字符串里的 true / null 字样不被改写",
        ok and o["say"] == ["this is true and null"], (ok, o))
    chk("19g''' 不是对象、缺 say 仍然判失败（不乱解析）",
        A._parse_reply_json("直接说了一句话")[0] is False and A._parse_reply_json("{'want': 3}")[0] is False)

    # 19h：速写没等到、要走同步识图时，先掷「识图概率」再花那次带人设的调用。
    # 此前是先识图再掷点：2026-11-24 线上 17 条带图消息里 12 条走到这里，七成的识图调用白花。
    # 同一天稍后又挪了一次：等描述之前就掷（见 10e），掷中了才等描述、才识图。
    with patched(random, random=lambda: 0.99):        # 必定没掷中（概率 30%）
        r = run_process(group_msg("[图片]", "p19a", [U1]), vprob=0.3)
    chk("19h 识图概率没中 → 不等描述、不调用视觉，直接沉默",
        r["vis"] == [] and r["sent"] == [] and r["acked"] == [7001]
        and any("识图概率 30% 未中，未等待描述" in x for x in r["logs"]), (r["vis"], r["sent"], r["logs"]))
    with patched(random, random=lambda: 0.0):         # 必定掷中
        r = run_process(group_msg("[图片]", "p19b", [U1]), vprob=0.3)
    chk("19h' 没等到速写 + 概率掷中 → 才去同步识图", r["vis"] == [1], (r["vis"], r["logs"]))
    with patched(random, random=lambda: 0.99):
        r = run_process(group_msg("看看这张图", "p19c", [U1]), vprob=0.3)
    chk("19h'' 话里明确在问这张图 → 不掷点，照常识图（对话正等着图的信息）",
        r["vis"] == [1] and not any("概率未中" in x for x in r["logs"]), (r["vis"], r["logs"]))
    _rolls = []
    with patched(random, random=lambda: (_rolls.append(1), 0.0)[1]):    # 必定掷中，并数掷了几次
        r = run_process(group_msg("[图片]", "p19d", [U1]), vprob=0.3)
    chk("19i 没等到速写、走去同步识图：骰子只有等描述之前那一次，不会掷第二遍",
        r["vis"] == [1] and len(_rolls) == 1, (r["vis"], len(_rolls)))
    _rolls = []
    with patched(random, random=lambda: (_rolls.append(1), 0.0)[1]):
        r = run_process(group_msg("[图片]", "p19e", [U1]), preloaded=["一张梗图"], vprob=0.3)
    chk("19j 等到了描述：判定照常跑，骰子同样只有一次",
        r["vis"] == [] and len(r["gates"]) == 1 and len(_rolls) == 1,
        (r["vis"], len(r["gates"]), len(_rolls)))

    # ---------- 20. 暂停的来源可追溯（2026-11-24 夜里被暂停了 23 分钟，查不出是谁暂停的） ----------
    import http.client as _hc20
    _tok20, _paused20 = dict(A._ADMIN_TOKEN), A.RT.paused
    A._ADMIN_TOKEN["t"] = "smoke-token-20"
    _srv20 = A.AdminServer((A.DAEMON_HOST, 0), A.AdminHandler)
    _port20 = _srv20.server_address[1]
    threading.Thread(target=_srv20.serve_forever, daemon=True).start()

    def _post20(body):
        cc = _hc20.HTTPConnection("127.0.0.1", _port20, timeout=5)
        cc.request("POST", "/admin/pause", json.dumps(body).encode("utf-8"),
                   {"Content-Type": "application/json", "X-QQBot-Token": "smoke-token-20"})
        rr = cc.getresponse()
        raw = rr.read()
        cc.close()
        return rr.status, json.loads(raw.decode("utf-8"))
    try:
        LOG.clear()
        _st20, _r20 = _post20({"paused": False, "src": "smoke-test"})
        _line20 = [x for x in LOG if x.startswith("AGENT_PAUSE ")]
        chk("20a 暂停接口的日志带上来源", _st20 == 200 and _r20.get("ok") is True
            and _line20 and "src=smoke-test" in _line20[0], (_st20, _r20, _line20))
        LOG.clear()
        _post20({"paused": False})
        _line20 = [x for x in LOG if x.startswith("AGENT_PAUSE ")]
        chk("20b 没报来源的记成 unknown", _line20 and "src=unknown" in _line20[0], _line20)
    finally:
        _srv20.shutdown()
        _srv20.server_close()
        A._ADMIN_TOKEN.clear()
        A._ADMIN_TOKEN.update(_tok20)
        with A.RT.lock:
            A.RT.paused = _paused20
    _src_ui20 = read_src("qq_admin_ui.py")
    chk("20c 状态页按钮自报来源 ui；管理台转发时带上来源，并记一行含对端地址的日志",
        "src:'ui'" in _src_ui20 and '"src": _src' in _src_ui20
        and "UI_PAUSE paused=%s src=%s from=%s" in _src_ui20)
    chk("20d 主人的遥控指令自报来源 remote", '"src": "remote"' in read_src("qq_bot_agent.py"))
    chk("20e agent 启动时若处于暂停状态会明说（不会回复任何消息）",
        "AGENT_READY_PAUSED" in read_src("qq_bot_agent.py"))

    # ---------- 21. 识图模型留空＝按模型链（2026-11-25）：默认值 / 可用性探测 / 状态 / 速写统计 / 掉线提醒 ----------
    # _NO_VISION_MODELS / _VISION_OK_MODELS / _VISION_PROBE / _VISION_REMINDED 都是模块级状态：
    # 每一小节用 patched 换上临时的、退出恢复原对象，不污染后面的用例（仿照 RT/_ADMIN_TOKEN 的做法）
    chk("21a 识图模型的内置默认值＝空串（不设任何厂商的内置默认）",
        A.C.ARCH_V2_DEFAULTS["vision_caption_model"] == "")
    with patched(A, load_json_const=_cfg_ljc({})):
        chk("21b 回复配置为空 → 识图模型就是空串（不再换成厂商型号）",
            A.vision_caption_model() == "")
    _chain21 = [("main", {"key": "M"}, "m-a", 4, 0),
                ("k1", {"key": "A"}, "m-b", 8, 0),
                ("k2", {"key": "B"}, "m-c", 4, 0)]
    with patched(A, _chain_candidates=lambda: list(_chain21), econ_on=lambda: False):
        _vc21 = A._vision_candidates("")
    chk("21c 识图模型留空 → 候选就是模型链本身（不在最前面插「主 Key + 某个型号」）",
        _vc21 == _chain21, _vc21)
    chk("21d 400 且报错含 content → 纯文本模型",
        A._is_text_only_error(400, {"error": {"message": "传入的 content 里混进了不支持的类型，取值范围 …"}}))
    chk("21d' 400 但报错是「图片输入格式/解析错误」→ 不算（可能是某一张图的问题，不能据此判成纯文本）",
        not A._is_text_only_error(400, {"error": {"message": "图片输入格式/解析错误"}}))
    chk("21d'' 429 → 不算", not A._is_text_only_error(429, {"error": {"message": "content…取值范围"}}))

    M1, M2 = "mv-one", "mv-two"
    _chain221 = [("main", {"key": "M"}, M1, 0, 0), ("k1", {"key": "A"}, M2, 0, 0)]
    _vis_base = dict(vision_enabled=lambda: True, vision_caption_on=lambda: True,
                     vision_caption_model=lambda: "", _chain_candidates=lambda: list(_chain221),
                     econ_on=lambda: False)
    def _probe0():
        return {"cls": {}, "sig": "", "ts": 0.0, "running": False}

    with patched(A, **_vis_base, _NO_VISION_MODELS={M1, M2}, _VISION_OK_MODELS=set(),
                 _VISION_PROBE=_probe0(), _VISION_REMINDED={"ts": 0.0}):
        _st21 = A.vision_status()
        chk("21e 链里两个模型都被试出不能看图 → no_vision_model",
            _st21["state"] == "no_vision_model", _st21["state"])
    with patched(A, **_vis_base, _NO_VISION_MODELS={M1, M2}, _VISION_OK_MODELS={M2},
                 _VISION_PROBE=_probe0(), _VISION_REMINDED={"ts": 0.0}):
        _st21 = A.vision_status()
        chk("21f 有一个能看图的 → ok，且给出现在用谁",
            _st21["state"] == "ok" and _st21["model"] == M2, (_st21["state"], _st21["model"]))
    with patched(A, **_vis_base, _NO_VISION_MODELS=set(), _VISION_OK_MODELS=set(),
                 _VISION_PROBE=_probe0(), _VISION_REMINDED={"ts": 0.0}):
        _st21 = A.vision_status()
        A._VISION_PROBE["cls"][M1] = "vision"
        _st21b = A.vision_status()
        chk("21g 什么都不知道 → unknown；后台试出一个能看图 → ok 且用它",
            _st21["state"] == "unknown" and _st21b["state"] == "ok" and _st21b["model"] == M1,
            (_st21["state"], _st21b["state"], _st21b["model"]))

    class _RT21(object):
        cooldowns = {}

    def _glm21(model, messages, timeout=40, extra_body=None, key=None):
        if model == M1:
            return 200, {"choices": [{"message": {"content": "红色"}, "finish_reason": "stop"}]}
        if model == M2:
            return 400, {"error": {"message": "模型不支持传入 content 中的 image 类型，取值范围 …"}}
        return 429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}}

    M3 = "mv-three"
    with patched(A, glm_chat=_glm21, _vision_pref=lambda: "", RT=_RT21(),
                 _chain_candidates=lambda: [("main", {"key": "M"}, M1, 0, 0),
                                            ("k1", {"key": "A"}, M2, 0, 0),
                                            ("k2", {"key": "B"}, M3, 0, 0)],
                 econ_on=lambda: False, _NO_VISION_MODELS=set(), _VISION_OK_MODELS=set(),
                 _VISION_PROBE=_probe0(), _VISION_REMINDED={"ts": 0.0}):
        LOG.clear()
        A._vision_probe([M1, M2, M3])
        chk("21h 后台探测：逐个试出 vision / text / error；试不动的（429）不进纯文本表；"
            "结束后记日志、运行位复位",
            A._VISION_PROBE["cls"] == {M1: "vision", M2: "text", M3: "error"}
            and M2 in A._NO_VISION_MODELS and M3 not in A._NO_VISION_MODELS
            and M1 in A._VISION_OK_MODELS
            and any(x.startswith("AGENT_VISION_PROBE ") for x in LOG)
            and A._VISION_PROBE["running"] is False,
            (dict(A._VISION_PROBE["cls"]),
             [x for x in LOG if x.startswith("AGENT_VISION_PROBE")][-1:]))
    with patched(A, **_vis_base, _NO_VISION_MODELS={M1, M2}, _VISION_OK_MODELS=set(),
                 _VISION_PROBE=_probe0(), _VISION_REMINDED={"ts": 0.0}):
        LOG.clear()
        _w21i = A.vision_wanted()
        A._vision_tick()
        _n1 = sum(1 for x in LOG if x.startswith("AGENT_VISION_NO_MODEL"))
        _st21i = A.vision_status()
        A._vision_tick()
        _n2 = sum(1 for x in LOG if x.startswith("AGENT_VISION_NO_MODEL"))
        chk("21i 用户开着识图、链里没有能看图的：日志提醒一次；10 分钟内不重复提醒",
            _n1 == 1 and _n2 == 1, (_n1, _n2, _w21i, _st21i["state"],
                                    dict(A._VISION_REMINDED)))

    reset_cap()
    with A._CAP["lock"]:
        _stat0 = dict(A._CAP["stat"])
        A._CAP["stat"].update({"ok": 0, "fail": 0, "timeout": 0, "last_model": "", "by_model": {}})
    try:
        with patched(A, _img_to_datauri=lambda u, daemon=None: "data:image/png;base64,AA",
                     vision_caption_model=lambda: "", econ_on=lambda: False,
                     _chain_candidates=lambda: [("main", {"key": "M"}, "m-cap", 0, 0)],
                     _VISION_OK_MODELS=set(),
                     _vision_attempt=lambda m, kv, vmsgs, th, mt: (
                         200, {"choices": [{"message": {"content": "一只橘猫"}}], "usage": {}})):
            reset_cap()
            _t21j = A._caption_call(NullDaemon(), U1)
            with A._CAP["lock"]:
                _ok21j, _last21j = A._CAP["stat"]["ok"], A._CAP["stat"]["last_model"]
        chk("21j 速写成功：返回描述，stat.ok 记 1 次、last_model 记下用的模型",
            _t21j == "一只橘猫" and _ok21j == 1 and _last21j == "m-cap", (_t21j, _ok21j, _last21j))
        with patched(A, _img_to_datauri=lambda u, daemon=None: "data:image/png;base64,AA",
                     vision_caption_model=lambda: "", econ_on=lambda: False,
                     _chain_candidates=lambda: [("main", {"key": "M"}, "m-cap", 0, 0)],
                     _VISION_OK_MODELS=set(),
                     _vision_attempt=lambda m, kv, vmsgs, th, mt: (
                         429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}})):
            reset_cap()
            _t21j2 = A._caption_call(NullDaemon(), U1)
            with A._CAP["lock"]:
                _fail21j = A._CAP["stat"]["fail"]
        chk("21j' 模型都不行：返回 None，stat.fail 记 1 次",
            _t21j2 is None and _fail21j == 1, (_t21j2, _fail21j))
    finally:
        with A._CAP["lock"]:
            A._CAP["stat"].clear()
            A._CAP["stat"].update(_stat0)

    # 21k：缓存只裁最旧的一段，不再像旧代码那样把整张表删掉。预填 4000 条（ts 递增），
    # 跑一次 _caption_run 写入第 4001 条触发裁剪 → 剩 3001 条，最新的和刚写入的都在。
    reset_cap()
    with A._CAP["lock"]:
        _base21k = time.time() - 100000
        for i in range(4000):
            A._CAP["by_mid"][("group", "555001", "k%04d" % i)] = {
                "texts": ["旧%d" % i], "ts": _base21k + i}
    _job21k = {"key": ("group", "555001", "k-new"), "urls": [U1], "ts": time.time(), "front": False}
    try:
        with patched(A, _caption_call=lambda dmn, u: "新的描述", vision_caption_daily_cap=lambda: 600):
            A._caption_run(NullDaemon(), _job21k)
        with A._CAP["lock"]:
            _n21k = len(A._CAP["by_mid"])
            _oldest21k = A._CAP["by_mid"].get(("group", "555001", "k0000"))
            _newest21k = A._CAP["by_mid"].get(("group", "555001", "k3999"))
            _job21k_in = A._CAP["by_mid"].get(_job21k["key"])
    finally:
        reset_cap()
    chk("21k 缓存超限：仍然是一张表、只剩最旧的被裁掉（≤3001 条），最新的和刚写入的都在",
        _n21k <= 3001 and _oldest21k is None and _newest21k and _job21k_in,
        (_n21k, bool(_oldest21k), bool(_newest21k), bool(_job21k_in)))

    src_ui21 = read_src("qq_admin_ui.py")
    chk("21l 设置页：识图模型占位是「留空＝按模型链」，不再出现厂商型号占位",
        'placeholder="留空＝按模型链"' in src_ui21 and 'placeholder="glm-4.6v-flash"' not in src_ui21)
    chk("21l' 保存分支：留空就存空串，不再回落到内置默认值",
        'data["vision_caption_model"] = _vcm' in src_ui21
        and 'data["vision_caption_model"] = _vcm or C.ARCH_V2_DEFAULTS["vision_caption_model"]' not in src_ui21)
    chk("21l'' 切到「看图」或「识图模型」分页时刷新识图状态行",
        "if(__ST_TAB==='style'||__ST_TAB==='model')visionStatePaint();" in src_ui21)
    src_agent21 = read_src("qq_bot_agent.py")
    chk("21m 管理台状态带上识图状态；主循环里定期跑识图探测/提醒",
        'snap["vision"] = vision_status()' in src_agent21
        and "\n                _vision_tick()" in src_agent21,
        ("\n                _vision_tick()" in src_agent21))
    src_app21 = read_src("qq_app.py")
    chk("21m' 主程序：掉线看守、用户提醒、托盘引用、启动看守线程都在",
        "def _offline_watch" in src_app21 and "def _alert_user" in src_app21
        and "threading.Thread(target=_offline_watch, daemon=True).start()" in src_app21
        and '_tray_ref["icon"] = tray' in src_app21)

    # ---------------------------------------------------------------------
    # 22. 快修九项（2026-11-25）：管理接口只起一次 / HTTPException 降级 /
    #     画图日志不带密钥 / 多条记忆逐条入库 / 主动消息不教记忆标记 /
    #     表情包描述关思考 / 任务执行器链首取名 / 上下文流水过期清理
    # ---------------------------------------------------------------------
    import http.client as _hc22
    import socket as _sk22
    import tempfile as _tf22

    # 22a 无密钥首次启动的二次绑定：独占绑定下同进程同端口第二次 bind 必抛；
    # main() 走过等待分支后 start_admin_server 全程只生效一次
    _sk22a = _sk22.socket()
    _sk22a.bind(("127.0.0.1", 0))
    _port22a = _sk22a.getsockname()[1]
    _sk22a.close()
    _srv22a = A.AdminServer(("127.0.0.1", _port22a), A.AdminHandler)
    threading.Thread(target=_srv22a.serve_forever, daemon=True).start()
    time.sleep(0.2)
    try:
        A.AdminServer(("127.0.0.1", _port22a), A.AdminHandler)
        _dup22a = False
    except OSError:
        _dup22a = True
    finally:
        _srv22a.shutdown()
        _srv22a.server_close()
    chk("22a 同进程同端口二次绑定 AdminServer 抛 OSError（独占绑定，二次启动必须跳过）",
        _dup22a)

    class _TH22(object):
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    class _TMod22(object):
        Thread = _TH22

    class _PH22(object):
        daemon = None

        def load_all(self):
            pass

    _starts22 = []

    def _sas22(port, daemon=None):
        _starts22.append(port)

    _usable22n = {"n": 0}

    def _usable22():
        _usable22n["n"] += 1
        return _usable22n["n"] > 1   # 首问：没 Key（走等待分支）；之后：配好了

    def _out22(line):
        line = str(line)
        if line.startswith("AGENT_READY chain="):
            raise _Stop()   # 在主循环开跑前停下（不再往后走）
        LOG.append(line)

    _argv22 = sys.argv
    _stopped22 = False
    try:
        with patched(sys, argv=["qq_bot_agent.py"]), \
                patched(A, start_admin_server=_sas22, any_key_usable=_usable22,
                        DaemonClient=lambda port, token=None: object(),
                        PLUGIN_HOST=_PH22(), out=_out22, threading=_TMod22):
            try:
                A.main()
            except _Stop:
                _stopped22 = True
    finally:
        sys.argv = _argv22
    chk("22a' 走过无 Key 等待分支后：main() 里 start_admin_server 只调用一次"
        "（第二次绑定会 10048，配好 Key 的瞬间再也不会崩）",
        _stopped22 and len(_starts22) == 1, (_stopped22, _starts22))

    # 22b glm_chat 抛 http.client.HTTPException（IncompleteRead/BadStatusLine）
    #     → 和断网同级：冷却本候选、换下一个，绝不穿出去
    def _rt22():
        class _R(object):
            pinned = None

            def __init__(self):
                self.cooldowns = {}
                self.current_model = None

            def cooldown(self, ck, seconds=None):
                self.cooldowns[ck] = time.time() + (seconds if seconds is not None else 300)

            def set_current(self, m):
                self.current_model = m
        return _R()

    _MA22, _MB22 = "m-drop-a", "m-drop-b"

    def _chain22(raiser):
        def _glm(model, messages, timeout=40, extra_body=None, key=None):
            if model == _MA22:
                raise raiser
            return 200, {"choices": [{"message": {"content": "换了候选"},
                                      "finish_reason": "stop"}]}
        return _glm

    def _fb22(raiser):
        with patched(A, RT=_rt22(), glm_chat=_chain22(raiser),
                     _chain_candidates=lambda: [("main", {"key": "K"}, _MA22, 0, 0),
                                                ("k1", {"key": "K2"}, _MB22, 0, 0)],
                     econ_on=lambda: False):
            LOG.clear()
            return A.call_glm_with_fallback([{"role": "user", "content": "嗨"}])

    _m22b, _t22b = _fb22(_hc22.IncompleteRead(b"abc", 10))
    chk("22b glm_chat 抛 IncompleteRead → 冷却后走下一候选，不穿出去",
        _m22b == _MB22 and _t22b == "换了候选"
        and any("AGENT_MODEL_FAIL" in x and _MA22 in x for x in LOG),
        (_m22b, _t22b, LOG[:1]))
    _m22c, _t22c = _fb22(_hc22.BadStatusLine("x"))
    chk("22b' glm_chat 抛 BadStatusLine → 同样降级换候选",
        _m22c == _MB22 and _t22c == "换了候选",
        (_m22c, _t22c))

    # 22c 画图失败日志不带密钥的任何前缀（此前 key=%.6s 把前 6 位写进日志）
    class _NoConn22(object):
        # 构造不失败（真实 HTTPSConnection 构造只是建对象）；请求阶段断网
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            raise OSError("冒烟不出网")

        def close(self):
            pass

    class _HCClient22(object):
        HTTPSConnection = _NoConn22

    class _HCMod22(object):
        client = _HCClient22

    _key22 = "SK-abcdef123456"
    with patched(A, http=_HCMod22, glm_key=lambda provider="zhipu": _key22,
                 load_api_keys=lambda: {}):
        LOG.clear()
        _img22 = A.generate_image("画一张测试图")
    _drawfail22 = [x for x in LOG if x.startswith("AGENT_DRAW_FAIL")]
    chk("22c 画图失败日志不含密钥任何前缀，只记 attempt 与错误信息",
        _img22 == "" and len(_drawfail22) == 2
        and all(("SK-abcdef123456"[:i] not in x) and "abcdef" not in x and "key=" not in x
                for i in (1, 2, 4, 6, 8) for x in _drawfail22),
        (_img22, _drawfail22[:1]))

    # 22d 一条回复里的多条记忆全部收集（≤3，超出 MEMO_DROP），process() 逐条入库
    def _memo_parse22(text):
        LOG.clear()
        _parts, _st, _memo, _dr, _sg = A.parse_reply(text)
        return _parts, _memo, [x for x in LOG if x.startswith("MEMO_DROP")]

    _p22, _m22, _drop22 = _memo_parse22(
        "你好呀[记忆@10086] 喜欢周一早起\n[记忆@10010] 讨厌香菜")
    chk("22d 两条记忆都收集且正文剥离干净",
        _m22 == [("10086", "喜欢周一早起"), ("10010", "讨厌香菜")]
        and not _drop22 and all("记忆" not in p for p in _p22), (_m22, _p22))
    _p22b, _m22b2, _drop22b = _memo_parse22(
        "[记忆] 甲喜欢钓鱼\n[记忆@20001] 乙在杭州上班\n[记忆@20002] 丙养猫")
    chk("22d' 三条全部保留", len(_m22b2) == 3 and not _drop22b, (_m22b2, _drop22b))
    _p22c, _m22c2, _drop22c = _memo_parse22("[记忆] 一\n[记忆] 二\n[记忆] 三\n[记忆] 四")
    chk("22d'' 四条只留前 3 条并记一行 MEMO_DROP",
        len(_m22c2) == 3 and _m22c2[2][1] == "三" and len(_drop22c) == 1,
        (_m22c2, _drop22c))
    _p22e, _m22e, _drop22e = _memo_parse22("开头[记忆@30001] 记住这件事\n结尾")
    chk("22d''' 单条行为与改前一致：一条记忆、正文无标记残留",
        _m22e == [("30001", "记住这件事")] and not _drop22e
        and _p22e == ["开头\n结尾"], (_m22e, _p22e))

    def _run_memo22(reply_text, mid):
        _sent22, _emitted22 = [], []
        _dm22 = NullDaemon(history=[])
        _dm22.send = lambda ct, cid, texts, reply_message_id=None: _sent22.append(list(texts))

        with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False, REPLY_JSON_FORCE=False,
                     REACT_ONLY_FORCE=False,
                     call_glm_with_fallback=lambda ms, t="", budget_sec=200, **kwargs: ("mx", reply_text),
                     _emit_memo=lambda daemon, msg, memo: _emitted22.append(tuple(memo)),
                     vision_enabled=lambda: False, chat_anti_interrupt_enabled=lambda: True,
                     lean_ctx_on=lambda: True, _cap_chat_allowed=lambda ct, cid: False):
            reset_cap()
            LOG.clear()
            A.process(_dm22, group_msg("今天中午吃什么好", mid, [], trig="at"))
        return _emitted22, [x for x in LOG if x.startswith("MEMO_DROP")], _sent22

    _em22, _dlog22, _sent22b = _run_memo22(
        "今天吃了食堂[记忆@40001] 甲喜欢爬山\n[记忆@40002] 乙在减肥", "m22-1")
    chk("22d'''' process() 把 2 条记忆逐条入库",
        _em22 == [("40001", "甲喜欢爬山"), ("40002", "乙在减肥")] and _sent22b,
        (_em22, _sent22b))
    _em22b, _dlog22b, _s22b2 = _run_memo22(
        "嗯嗯[记忆] 一\n[记忆] 二\n[记忆] 三\n[记忆] 四", "m22-2")
    chk("22d''''' 4 条只入库 3 条且 MEMO_DROP 落日志",
        len(_em22b) == 3 and _em22b[2] == (None, "三") and len(_dlog22b) == 1,
        (_em22b, _dlog22b))
    _em22c, _dlog22c, _s22c2 = _run_memo22(
        "好的呀[记忆@40001] 甲喜欢爬山", "m22-3")
    chk("22d'''''' 单条入库一次（与改前一致）",
        _em22c == [("40001", "甲喜欢爬山")] and not _dlog22c, (_em22c, _dlog22c))

    # 22e 主动消息不再教记忆标记（教了也不入库 → 纯属浪费 token 和误写源）；
    #     主回复提示词的 [记忆@QQ号] 教学原样保留
    with patched(A, PROMPT_V2_FORCE=True, VCAP_FORCE=False):
        _pro22 = A._proactive_compose(NullDaemon(history=[]), "group", "555001")[0][0]["content"]
        _main22 = A.build_messages(NullDaemon(history=hist),
                                   group_msg("在吗", "h22", [], trig="at"))[0][0]["content"]
    chk("22e 主动消息提示词不再出现 [记忆@…] 教学；主回复提示词的教学原样保留",
        "[记忆@" not in _pro22 and "值得长期记住" not in _pro22
        and "[记忆@QQ号]" in _main22)
    src22 = read_src("qq_bot_agent.py")
    chk("22e' 主动消息路径不写记忆：parse 的 memo 丢弃，_emit_memo 仍只有 def + process 一处调用",
        "texts, stickers, _memo, _draw, _sendgrp = parse_reply(reply)" in src22
        and "for _m in memo:" in src22 and src22.count("_emit_memo(daemon") == 2,
        src22.count("_emit_memo(daemon"))

    # 22f 表情包自动描述：与后台速写一致显式关思考，输出上限仍 200
    _args22f = {}

    def _glm22f(model, messages, timeout=40, extra_body=None, key=None):
        _args22f["extra"] = dict(extra_body or {})
        return 200, {"choices": [{"message": {"content": "一只猫点头"}}], "usage": {}}

    _png22 = os.path.join(_tf22.mkdtemp(prefix="qqbot_smoke_g22_"), "st.png")
    pathlib.Path(_png22).write_bytes(b"\x89PNG smoke")
    with patched(A, glm_chat=_glm22f,
                 _chain_candidates=lambda: [("main", {"key": "K"}, "mv-cap", 0, 0)],
                 _NO_VISION_MODELS=set()):
        _got22f = A._vision_describe_file(_png22)
    chk("22f 表情包自动描述显式关闭思考（思考模型下不再恒为空），输出上限仍 200",
        _got22f == "一只猫点头" and _args22f.get("extra", {}).get("thinking") == {"type": "disabled"}
        and _args22f.get("extra", {}).get("max_tokens") == 200,
        (_got22f, _args22f.get("extra")))
    shutil.rmtree(os.path.dirname(_png22), ignore_errors=True)   # 用完即清

    # 22g 任务执行器取链首模型：dict 条目取 model 字段，str 照旧，取不到回退默认
    # （经裁决：修复落点在 qq_task_worker.py）
    import qq_task_worker as W22
    with patched(A, load_json_const=lambda p, d=None: {"models": [{"model": "glm-4.7-flash",
                                                                   "key": "k1"}]}):
        chk("22g 链条是 dict 条目 → 取的是模型名而不是整个对象",
            W22._chain_first_model() == "glm-4.7-flash", W22._chain_first_model())
    with patched(A, load_json_const=lambda p, d=None: {"models": ["glm-4.6v-flash",
                                                                  {"model": "zzz"}]}):
        chk("22g' 链条是 str 条目 → 与改前一致",
            W22._chain_first_model() == "glm-4.6v-flash", W22._chain_first_model())
    with patched(A, load_json_const=lambda p, d=None: {}):
        chk("22g'' 空链条 → 回退默认 glm-5.3-flash",
            W22._chain_first_model() == "glm-5.3-flash", W22._chain_first_model())
    with patched(A, load_json_const=lambda p, d=None: {"models": [{"key": "k1"}]}):
        chk("22g''' dict 缺 model 字段 → 同样回退默认值",
            W22._chain_first_model() == "glm-5.3-flash", W22._chain_first_model())

    # 22h 上下文流水 logs/context/ctx_*.jsonl 跟着 logs_keep_days 过期清理；
    #     开关关了都不删；根目录 *.log 行为不变；非 ctx_*.jsonl 一律不动
    _logs22 = _tf22.mkdtemp(prefix="qqbot_smoke_logs_")
    _ctx22 = os.path.join(_logs22, "context")
    os.makedirs(_ctx22, exist_ok=True)
    _now22 = time.time()
    _old22, _new22 = _now22 - 40 * 86400, _now22 - 86400

    def _mk22(name, mtime, sub=""):
        p = os.path.join(_logs22, sub, name)
        pathlib.Path(p).write_text("x", encoding="utf-8")
        os.utime(p, (mtime, mtime))
        return p

    def _autoclean22(enabled):
        class _CStub22(object):
            @staticmethod
            def load_app_config():
                return {"logs_autoclean_enabled": enabled, "logs_keep_days": 30}

            @staticmethod
            def logs_dir():
                return _logs22
        with patched(A, C=_CStub22(), _CTX_DIR=_ctx22):
            LOG.clear()
            A._logs_autoclean()
        return [x for x in LOG if x.startswith("AGENT_LOGCLEAN")]

    _old_files22 = [("agent.log", "", _old22), ("x.log.old", "", _old22),
                    ("keep.log", "", _new22), ("usage_like.txt", "", _old22),
                    ("ctx_20200101.jsonl", "context", _old22),
                    ("ctx_20990101.jsonl", "context", _new22),
                    ("other.jsonl", "context", _old22)]
    for _n22, _sub22, _t22 in _old_files22:
        _mk22(_n22, _t22, _sub22)
    _clean22 = _autoclean22(True)
    chk("22h 开关开：过期 *.log / *.log.old / ctx_*.jsonl 被删，未过期的和非 ctx 文件都在",
        not os.path.exists(os.path.join(_logs22, "agent.log"))
        and not os.path.exists(os.path.join(_logs22, "x.log.old"))
        and os.path.exists(os.path.join(_logs22, "keep.log"))
        and os.path.exists(os.path.join(_logs22, "usage_like.txt"))
        and not os.path.exists(os.path.join(_ctx22, "ctx_20200101.jsonl"))
        and os.path.exists(os.path.join(_ctx22, "ctx_20990101.jsonl"))
        and os.path.exists(os.path.join(_ctx22, "other.jsonl"))
        and any("removed=3" in x for x in _clean22), (_clean22,))
    for _n22, _sub22, _t22 in _old_files22:
        _mk22(_n22, _t22, _sub22)
    _clean22b = _autoclean22(False)
    chk("22h' 开关关：一个都不删",
        all(os.path.exists(os.path.join(_logs22, _sub22, _n22))
            for _n22, _sub22, _t22 in _old_files22) and not _clean22b, (_clean22b,))
    shutil.rmtree(_logs22, ignore_errors=True)   # 用完即清

    # ---------------------------------------------------------------------
    # 23. 机器人 agent 安全七项（2026-11-25）：保护目录设备路径前缀 / open
    #     目标校验 / 二次验证关闭直接授权 / 记忆净化与归属 / 互聊试探剥标记 /
    #     对话群管执行侧开关
    # ---------------------------------------------------------------------
    import webbrowser as _wb23
    _tmp23 = tempfile.mkdtemp(prefix="qqbot_smoke_g23_")
    _sub23 = os.path.join(_tmp23, "inner")
    os.makedirs(_sub23, exist_ok=True)
    for _ext23 in (".txt", ".png", ".exe", ".bat", ".lnk", ".ps1"):
        pathlib.Path(os.path.join(_tmp23, "f" + _ext23)).write_bytes(b"x")
    pathlib.Path(os.path.join(_sub23, "secret.txt")).write_bytes(b"x")

    # 23a 保护目录：\\?\ / \\.\ / \??\ 等设备路径前缀写法不再绕过
    _sysroot23 = os.environ.get("SystemRoot") or r"C:\Windows"
    chk("23a-1 _norm_win_path：设备前缀/大小写混写/正斜杠都归到同一形态；\\?\\UNC\\ 还原成 \\\\",
        A._norm_win_path("\\\\?\\C:\\Windows") == A._norm_win_path(_sysroot23)
        and A._norm_win_path("\\\\.\\C:\\Windows") == A._norm_win_path(_sysroot23)
        and A._norm_win_path("\\??\\C:\\Windows") == A._norm_win_path(_sysroot23)
        and A._norm_win_path("c:/wInDoWs") == A._norm_win_path(_sysroot23)
        and A._norm_win_path(_sysroot23 + "\\") == A._norm_win_path(_sysroot23)
        and A._norm_win_path("\\\\?\\UNC\\srv\\share") == "\\\\srv\\share",
        (A._norm_win_path("\\\\?\\C:\\Windows"),
         A._norm_win_path("\\\\?\\UNC\\srv\\share")))
    with patched(A, _protected_dirs=lambda: [_tmp23]):
        chk("23a-2 保护目录：前缀写法/大小写/正斜杠/下级 → True",
            A._is_protected("\\\\?\\" + _tmp23)
            and A._is_protected("\\\\.\\" + _tmp23)
            and A._is_protected("\\??\\" + _tmp23)
            and A._is_protected(_tmp23.upper().replace("\\", "/"))
            and A._is_protected(_sub23),
            [A._is_protected(p) for p in ("\\\\?\\" + _tmp23, _sub23)])
        chk("23a-3 只是前缀相同的兄弟目录不是保护目录 → False",
            not A._is_protected(_tmp23 + "X")
            and not A._is_protected(_tmp23 + "X\\file.txt"),
            A._is_protected(_tmp23 + "X"))
    with patched(A, _protected_dirs=lambda: ["\\\\srv\\share"]):
        chk("23a-4 UNC 保护目录：\\?\\UNC\\ 目标与 \\\\srv\\share 等价 → True",
            A._is_protected("\\\\?\\UNC\\srv\\share\\file.txt")
            and A._is_protected("//srv/share/file.txt"),
            A._is_protected("\\\\?\\UNC\\srv\\share\\file.txt"))
    class _Boom23(object):
        def __bool__(self):
            return True

        def __str__(self):
            raise RuntimeError("boom")

    chk("23a-5 规范化抛异常 → 一律按受保护",
        A._is_protected(_Boom23()) is True)

    # 23b open 动作：网址只认 http/https；本地目标过保护目录+类型白名单
    def _open23(kind, payload, allow_exe=False, prot=None):
        calls = {"web": [], "start": []}
        with patched(A, _actions_enabled=lambda: True,
                     _resolve_open_target=lambda t: (kind, payload),
                     _open_allow_exe=lambda: allow_exe,
                     _protected_dirs=lambda: list(prot or [])), \
                patched(_wb23, open=lambda u, *a, **k: calls["web"].append(u) or True), \
                patched(os, startfile=lambda p, *a, **k: calls["start"].append(p)):
            res = A._devctrl_step(NullDaemon(), ("private", "888001"), "open", "任意")
        return res, len(calls["web"]), len(calls["start"])

    _r23, _w23, _s23 = _open23("url", "https://example.com/page")
    chk("23b-1 https 网址放行且只走 webbrowser.open",
        _w23 == 1 and _s23 == 0 and "成功" in _r23, (_r23, _w23, _s23))
    _r23, _w23, _s23 = _open23("url", " http://example.com \t")
    chk("23b-2 http 网址放行（首尾空白剥掉后判断）", _w23 == 1 and _s23 == 0, (_r23, _w23))
    _bad23 = []
    for _u23 in ("file:///C:/Windows/notepad.exe", "javascript:alert(1)",
                 "ms-settings:display", "myapp://open", "\x01javascript:alert(1)"):
        _r23, _w23, _s23 = _open23("url", _u23)
        _bad23.append((_w23, _s23, "只打开 http 和 https 网页" in _r23))
    chk("23b-3 file:/javascript:/ms-settings:/自定义协议/控制字符前缀 → 拒绝且不打开",
        all(w == 0 and s == 0 and ok for w, s, ok in _bad23), _bad23)
    _r23, _w23, _s23 = _open23("path", os.path.join(_tmp23, "f.txt"))
    chk("23b-4 白名单扩展名放行且只走 os.startfile", _w23 == 0 and _s23 == 1 and "成功" in _r23,
        (_r23, _w23, _s23))
    _deny23 = []
    for _ext23 in (".exe", ".bat", ".lnk", ".ps1"):
        _r23, _w23, _s23 = _open23("path", os.path.join(_tmp23, "f" + _ext23))
        _deny23.append((_w23, _s23, "出于安全，不打开这一类文件" in _r23))
    chk("23b-5 .exe/.bat/.lnk/.ps1 默认拒绝且不打开",
        all(w == 0 and s == 0 and ok for w, s, ok in _deny23), _deny23)
    _r23, _w23, _s23 = _open23("path", os.path.join(_tmp23, "f.exe"), allow_exe=True)
    chk("23b-6 device_ctrl_open_allow_exe 开 → .exe 放行",
        _w23 == 0 and _s23 == 1 and "成功" in _r23, (_r23, _w23, _s23))
    _r23, _w23, _s23 = _open23("path", os.path.join(_sub23, "secret.txt"), prot=[_tmp23])
    _r23b, _w23b, _s23b = _open23("path", "\\\\?\\" + os.path.join(_sub23, "secret.txt"),
                                  prot=[_tmp23], allow_exe=True)
    chk("23b-7 保护目录下的文件不打开（设备路径前缀写法、开关开也拦）",
        _w23 == 0 and _s23 == 0 and "在保护目录内" in _r23
        and _w23b == 0 and _s23b == 0 and "在保护目录内" in _r23b, (_r23, _r23b))
    _r23, _w23, _s23 = _open23("path", os.path.join(_tmp23, "no_such_dir"))
    chk("23b-8 不存在的文件夹 → 拒绝且不打开",
        _w23 == 0 and _s23 == 0 and "不存在" in _r23, (_r23, _w23, _s23))
    _r23, _w23, _s23 = _open23("path", _sub23)
    chk("23b-9 存在的文件夹放行（与改前一致）", _w23 == 0 and _s23 == 1 and "成功" in _r23,
        (_r23, _w23, _s23))
    chk("23b-10 解析器本身不会把 file: 等当网址（url 只出自 http/https 前缀）",
        A._resolve_open_target("javascript:alert(1)")[0] != "url"
        and A._resolve_open_target("file:///C:/x")[0] != "url",
        (A._resolve_open_target("javascript:alert(1)")[0],))

    # 23c 设备操控二次验证关闭：主人请求直接授权、不再索码
    def _dev23(text, twofa, sender="888001", ask_open=False):
        sent, loops = [], []
        dm = NullDaemon(history=[])
        dm.send = lambda ct, cid, texts, reply_message_id=None: sent.append(list(texts))
        A._DEVCTRL_AUTH["ok_until"] = 0.0
        A._DEVCTRL_ASK["until"] = time.time() + 300 if ask_open else 0.0
        A._DEVCTRL_CODE.update({"code": "654321", "expires": time.time() + 300})
        A._DEVCTRL_SESSION.update({"msgs": [], "noticed": False})
        msg = group_msg(text, "d23", [], trig="at", sender=sender)
        with patched(A, _device_ctrl_enabled=lambda: True, owner_qq=lambda: "888001",
                     cfg_2fa_on=lambda: twofa,
                     devctrl_agent_loop=lambda d, m, r, f: loops.append(str(m.get("text")))):
            LOG.clear()
            handled = A._handle_devctrl(dm, msg)
        return handled, sent, loops, A._DEVCTRL_AUTH["ok_until"]

    _h23, _sent23, _loops23, _ok23 = _dev23("设备操控", False)
    chk("23c-1 二次验证关：主人请求直接授权（ok_until 已写）、进工具循环、不索码",
        _h23 and _ok23 > time.time() and _loops23 == ["设备操控"] and _sent23
        and all("授权码" not in t for ts in _sent23 for t in ts)
        and any("连线成功" in t for ts in _sent23 for t in ts), (_h23, _sent23, _loops23))
    _h23, _sent23, _loops23, _ok23 = _dev23("设备操控", False, sender="777002")
    chk("23c-2 二次验证关：非主人照样不能用（入口限定主人）",
        not _h23 and not _sent23 and not _loops23 and _ok23 == 0.0,
        (_h23, _sent23, _loops23))
    _h23, _sent23, _loops23, _ok23 = _dev23("设备操控", True)
    chk("23c-3 二次验证开：仍先索授权码（行为与改前一致）",
        _h23 and _ok23 == 0.0 and not _loops23
        and any("授权码" in t for ts in _sent23 for t in ts), (_h23, _sent23, _loops23))
    _h23, _sent23, _loops23, _ok23 = _dev23("654321", True, ask_open=True)
    chk("23c-4 二次验证开：码正确 → 授权窗口打开（行为与改前一致）",
        _h23 and _ok23 > time.time() and not _loops23
        and any("连线成功" in t for ts in _sent23 for t in ts), (_h23, _sent23, _loops23))

    # 23d 记忆入库前的文本净化
    _memo_seq = [0]

    def _memo23(memo, owner=False, sender="10001", history=None):
        recs = []
        _memo_seq[0] += 1
        dm = NullDaemon(history=history or [])
        msg = group_msg("好的", "m23", [], sender=sender)
        with patched(A, MEMORY_STREAM_PATH=os.path.join(_tmp23, "mem_%d.jsonl" % _memo_seq[0]),
                     _append_memory=lambda rec: recs.append(rec),
                     is_owner=lambda m: owner):
            LOG.clear()
            A._emit_memo(dm, msg, memo)
        return recs, [x for x in LOG if x.startswith("MEMO_DROP_FOREIGN")]

    _recs23, _ = _memo23((None, "第一行\n第二行  很多   空白"))
    chk("23d-1 换行转空格、连续空白压成一个",
        _recs23 and _recs23[0]["text"] == "第一行 第二行 很多 空白", _recs23)
    _recs23, _ = _memo23((None, "\"带引号\""))
    chk("23d-2 首尾引号剥掉", _recs23 and _recs23[0]["text"] == "带引号", _recs23)
    _recs23, _ = _memo23((None, "[记忆@10086] 喜欢猫"))
    chk("23d-3 记忆标记连同其后内容剥掉，净化后为空 → 不写", _recs23 == [], _recs23)
    _recs23, _ = _memo23((None, "她养了一只\n 叫咪咪的[STICKER:5]猫"))
    chk("23d-4 表情等控制标记剥掉，入库的是净化后的文本",
        _recs23 and _recs23[0]["text"] == "她养了一只 叫咪咪的猫", _recs23)
    _recs23, _ = _memo23((None, "记" * 150))
    chk("23d-5 超长截到 100 字（现有上限保留）",
        _recs23 and _recs23[0]["text"] == "记" * 100, len(_recs23 and _recs23[0]["text"]))
    _recs23, _ = _memo23((None, "喜欢钓鱼"))
    chk("23d-6 干净的记忆与改前一致", _recs23 and _recs23[0]["text"] == "喜欢钓鱼", _recs23)
    chk("23d-7 _sanitize_memo_text：裸记忆标记/剥净后为空 → 空串",
        A._sanitize_memo_text("[记忆]") == ""
        and A._sanitize_memo_text("[STICKER:3]开心[WAIT][SPLIT]好[SKIP]") == "开心好"
        and len(A._sanitize_memo_text("好" * 150)) == 100,
        (repr(A._sanitize_memo_text("[记忆]")),
         repr(A._sanitize_memo_text("[STICKER:3]开心[WAIT][SPLIT]好[SKIP]"))))

    # 23e 非主人的记忆不许挂到别人名下
    _hist23 = [{"sender_name": "小张", "sender_id": "10001"},
               {"sender_name": "阿强", "sender_id": "20002"}]
    _recs23, _drops23 = _memo23(("20002", "阿强讨厌香菜"), owner=False, sender="10001")
    chk("23e-1 非主人指定别人的 QQ → 不存且有 MEMO_DROP_FOREIGN 日志",
        _recs23 == [] and len(_drops23) == 1, (_recs23, _drops23))
    _recs23, _drops23 = _memo23(("10001", "我爱吃香菜"), owner=False, sender="10001")
    chk("23e-2 非主人指定自己 → 照旧存到自己名下",
        _recs23 and _recs23[0]["scope"] == "contact" and _recs23[0]["id"] == "10001"
        and not _drops23, (_recs23, _drops23))
    _recs23, _drops23 = _memo23((None, "阿强讨厌香菜"), owner=False, sender="10001",
                                history=_hist23)
    chk("23e-3 非主人未指定目标、正文含别人昵称 → 不做昵称推断，落会话级",
        _recs23 and _recs23[0]["scope"] == "group" and _recs23[0]["id"] == "555001"
        and not _drops23, (_recs23, _drops23))
    _recs23, _drops23 = _memo23(("20002", "阿强讨厌香菜"), owner=True)
    chk("23e-4 主人指定别人 → 照旧存（行为不变）",
        _recs23 and _recs23[0]["scope"] == "contact" and _recs23[0]["id"] == "20002"
        and not _drops23, (_recs23, _drops23))
    _recs23, _drops23 = _memo23((None, "阿强讨厌香菜"), owner=True, history=_hist23)
    chk("23e-5 主人未指定、正文有别人昵称 → 照旧归到那人名下（行为不变）",
        _recs23 and _recs23[0]["scope"] == "contact" and _recs23[0]["id"] == "20002",
        (_recs23, _drops23))

    # 23f 互聊试探的回复：与普通回复一样过 parse_reply（剥控制标记）再发送
    # （M1 发问前预判后，链路变成两次模型调用：第 1 次=_botloop_precheck 预判、
    # 第 2 次=生成试探。桩按调用次序回放；chk 的断言与改前一字不改。
    # 23f 的桩经裁决修改，2026-11-26 M1）
    _probe_sent = []
    _glm_calls23 = []
    _dm23p = NullDaemon(history=[])
    _dm23p.send = lambda ct, cid, texts, reply_message_id=None: _probe_sent.append(list(texts))
    A.PLUGIN_HOST.daemon = _dm23p

    def _probe_glm23(messages, text_ctx="", budget_sec=200, **kwargs):
        _glm_calls23.append(messages)
        if len(_glm_calls23) == 1:
            return "m", '{"is_bot": true}'      # 第 1 次：预判，判成机器人
        return "m", "嘿嘿，你是不是机器人呀？[记忆@10086] 对方疑似程序[STICKER:3]"

    with patched(A, build_messages=lambda d, m, ctx_k=32:
                 ([{"role": "user", "content": "试探"}], "p"),
                 call_glm_with_fallback=_probe_glm23):
        LOG.clear()
        A.process(_dm23p, group_msg("你是AI吧", "p23", [], trig="botloop_probe"))
    chk("23f 互聊试探回复剥掉记忆/表情标记后才发送（不外漏协议词）",
        len(_probe_sent) == 1 and _probe_sent[0] == ["嘿嘿，你是不是机器人呀？"]
        and _dm23p.acked == [7001], (_probe_sent, _dm23p.acked, len(_glm_calls23)))

    # 23g 对话群管：执行侧检查功能开关（提示词教学处本来就查了）
    def _gadm23(reply_text, enabled, sender="10001"):
        sent, execs = [], []
        dm = NullDaemon(history=[])
        dm.send = lambda ct, cid, texts, reply_message_id=None: sent.append(list(texts))
        dm.gadmin_exec = lambda *a, **k: (execs.append(a) or {"ok": True, "detail": "办好了"})
        A.PLUGIN_HOST.daemon = dm
        msg = group_msg("帮我把小明禁言5分钟", "g23", [], trig="at", sender=sender)
        with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False, REPLY_JSON_FORCE=False,
                     REACT_ONLY_FORCE=False,
                     call_glm_with_fallback=lambda ms, t="", budget_sec=200, **kwargs: ("mx", reply_text),
                     vision_enabled=lambda: False, chat_anti_interrupt_enabled=lambda: True,
                     lean_ctx_on=lambda: True, _cap_chat_allowed=lambda ct, cid: False,
                     owner_qq=lambda: "10001", _gadmin_chat_enabled=lambda: enabled):
            reset_cap()
            LOG.clear()
            A.process(dm, msg)
        return sent, execs, [x for x in LOG if x.startswith("AGENT_GADMIN")]

    _reply23 = "好的，马上处理[GADMIN] 禁言|群=555001|人=小明|分钟=5"
    _sent23, _execs23, _glog23 = _gadm23(_reply23, False)
    chk("23g-1 对话群管开关关：一个动作不执行，只回「对话群管没有开启」",
        _execs23 == [] and _sent23 == [["对话群管没有开启"]]
        and any("AGENT_GADMIN_OFF" in x for x in _glog23), (_sent23, _execs23, _glog23))
    _sent23, _execs23, _glog23 = _gadm23(_reply23, True)
    chk("23g-2 开关开：照常执行（行为与改前一致）",
        len(_execs23) == 1 and _execs23[0][0] == "ban" and _execs23[0][2] == "小明"
        and any("马上处理" in t for ts in _sent23 for t in ts), (_execs23, _sent23, _glog23))
    _sent23, _execs23, _glog23 = _gadm23(_reply23, True, sender="99999")
    chk("23g-3 非主人带群管标记 → 照旧剥离不执行（行为与改前一致）",
        _execs23 == [] and any("AGENT_GADMIN_DENY" in x for x in _glog23),
        (_execs23, _glog23))

    shutil.rmtree(_tmp23, ignore_errors=True)   # 用完即清

print("\n%d 项失败" % len(fails) + (": " + "; ".join(fails) if fails else "（全部通过）"))

# 一次性临时数据目录：先关日志句柄（Windows 下打开的文件删不掉），再尽量删掉
if _SMOKE_OWN_DATA_DIR:
    try:
        import logging
        logging.shutdown()
        shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
    except Exception:
        pass
sys.exit(1 if fails else 0)
