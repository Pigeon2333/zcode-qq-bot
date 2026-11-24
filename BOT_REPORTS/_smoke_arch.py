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
import sys
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
        def _f(messages, text_ctx="", budget_sec=200):
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
                calls[0][0]["content"].startswith("S") and A.JSON_RULE_AGENT in calls[0][0]["content"]
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

        def _next2(ms, t, budget_sec=200):
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
                and A._GATE_Y_NOTE not in sys_e and sys_e.index(A.JSON_RULE_AGENT) < sys_e.index(A._GATE_E_NOTE_JSON)
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
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: ("m", " Y。")):
        chk("3g 判定返回 Y", A._reply_gate_v2(dm, group_msg("x", "m", []), [
            {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "智谱娘") == "Y")
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: ("m", "不知道")):
        chk("3h 判定输出不合法 → 按沉默", A._reply_gate_v2(dm, group_msg("x", "m", []), [
            {"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "智谱娘") == "N")

    def _raise(ms, t, budget_sec=200):
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
    def run_process(msg, gate_verdict="N", preloaded=None, vcap=True):
        """走真实 process()，记录：同步识图有没有被调、判定看到的内容、确认了几次。"""
        vis, gates = [], []
        dm = NullDaemon(history=hist)
        A.PLUGIN_HOST.daemon = dm

        def _vision(ms, urls, daemon=None):
            vis.append(1)
            raise _Stop()

        def _chain(messages, text_ctx="", budget_sec=200):
            if len(messages) == 2 and _gate_head in messages[0]["content"]:
                gates.append(messages[1]["content"])
                return "m", gate_verdict
            return "m", '{"say": ["在呢"]}'

        _gate_head = "的插话判定器"     # 判定提示词的固定片段（人设名在它前面）

        with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True,
                     VCAP_FORCE=vcap,
                     vision_enabled=lambda: True, vision_mode=lambda: "all_prob",
                     vision_prob=lambda: 0.0, call_vision=_vision,
                     call_glm_with_fallback=_chain, chat_anti_interrupt_enabled=lambda: True,
                     _cap_chat_allowed=lambda ct, cid: True):
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
            return dict(vis=vis, gates=gates, acked=dm.acked, logs=list(LOG), where=where)

    r = run_process(group_msg("[图片]", "p1", [U1]), preloaded=["一张猫咪表情包"])
    chk("10a 闲聊触发+图已有描述 → 不再同步识图，交给统一判定",
        r["vis"] == [] and len(r["gates"]) == 1 and r["where"] == "returned"
        and r["acked"] == [7001], (r["vis"], len(r["gates"]), r["acked"]))
    chk("10b 判定看到的是图的描述，不是方括号占位",
        r["gates"] and "（图：一张猫咪表情包）" in r["gates"][0] and "[图片]" not in r["gates"][0])
    chk("10c 记日志：命中描述", any(x.startswith("AGENT_VCAP_HIT") for x in r["logs"]))
    chk("10d 判定前已先登记这条消息（供以后的历史行用）",
        A._caption_get("group", "555001", "p1") is not None)

    t0 = time.time()
    r = run_process(group_msg("[图片]", "p2", [U1]))
    chk("10e 4 秒内没等到描述 → 退回旧路径：识图概率为 0 时不调模型、直接沉默",
        r["vis"] == [] and r["gates"] == [] and r["acked"] == [7001]
        and any(x.startswith("AGENT_VCAP_MISS") for x in r["logs"])
        and any("VISION_ZERO_SKIP" in x for x in r["logs"]), (r["vis"], len(r["gates"])))
    chk("10f 等待有上限（不超过 5 秒）", time.time() - t0 < 5.5, round(time.time() - t0, 1))

    r = run_process(group_msg("看看这张图", "p3", [U1]))
    chk("10g 没描述但话里明确在问这张图 → 仍同步识图（旧路径保留）",
        r["vis"] == [1] and r["where"] == "vision", r["vis"])

    r = run_process(group_msg("这是什么", "p4", [U1]), gate_verdict="Y")
    chk("10h 没描述、话没提图、判定放行 → 判定不跑，按旧路径同步识图",
        r["gates"] == [] and r["vis"] == [1], (len(r["gates"]), r["vis"]))

    r = run_process(group_msg("这是什么", "p5", [U1], trig="at"), preloaded=["一张菜单照片"])
    chk("10i 直接@+图 → 即使有描述也让模型亲眼看图（同步识图）",
        r["vis"] == [1] and r["where"] == "vision", r["vis"])

    r = run_process(group_msg("今天天气不错", "p6", [U1]), vcap=False)
    chk("10j 识图开关关着 → 整个后台识图分支不参与（等都不等，直接走旧路径）",
        not any("VCAP" in x for x in r["logs"]) and r["vis"] == []
        and any("VISION_ZERO_SKIP" in x for x in r["logs"]), r["logs"][:4])
    r = run_process(group_msg("哈哈哈", "p7", []), preloaded=None)
    chk("10k 不带图的消息 → 不涉及识图，照常判定", r["vis"] == [] and len(r["gates"]) == 1)

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
    # 显式模型失败 → 落到链式降级（不再直接抛）
    asks.clear()
    rt = _RT()
    base11["RT"] = rt
    base11["glm_chat"] = _scripted((429, "该模型当前访问量过大，请您稍后再试", None),
                                   (200, "链上模型看到", "stop"))
    base11["_chain_candidates"] = lambda: [("k2", {"key": "k"}, "vm", 8, 0), ("k9", {"key": "z"}, "m9", 8, 0)]
    with patched(A, **base11):
        m, text = A.call_vision([{"content": "S"}, {"content": "U"}], ["http://img/1.jpg"])
    chk("11e 显式模型限流 → 落到链上下一个模型（链上不再重试它）",
        m == "m9" and text == "链上模型看到" and len(asks) == 2, (m, text, len(asks)))

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

print("\n%d 项失败" % len(fails) + (": " + "; ".join(fails) if fails else "（全部通过）"))
sys.exit(1 if fails else 0)
