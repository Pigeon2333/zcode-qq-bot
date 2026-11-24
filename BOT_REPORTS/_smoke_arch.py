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

        def _chain(messages, text_ctx="", budget_sec=200):
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

    t0 = time.time()
    r = run_process(group_msg("[图片]", "p2", [U1]), vprob=0.0)
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

    r = run_process(group_msg("今天天气不错", "p6", [U1]), vcap=False, vprob=0.0)
    chk("10j 识图开关关着 → 整个后台识图分支不参与（等都不等，直接走旧路径）",
        not any("VCAP" in x for x in r["logs"]) and r["vis"] == []
        and any("VISION_ZERO_SKIP" in x for x in r["logs"]), r["logs"][:4])
    r = run_process(group_msg("哈哈哈", "p7", []), preloaded=None)
    chk("10k 不带图的消息 → 不涉及识图，照常判定", r["vis"] == [] and len(r["gates"]) == 1)
    # 设置页的「识图概率」在后台识图下照样管用（和旧路径同一个骰子、同一个例外）
    r = run_process(group_msg("[图片]", "p10", [U1]), preloaded=["一张梗图"], vprob=0.0)
    chk("10o 后台识图开着 + 识图概率为 0：描述照样拿到，但不评论这张图",
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
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: c14.append(ms) or (
            "m", '{"nudge": true, "say": "楼下的事办完了吗"}')):
        t14 = A._nudge_compose_v2(ndm, "private", "20002")
    txt14 = c14[0][0]["content"] + "\n" + c14[0][1]["content"]
    chk("14h 追聊 v2：要追时返回那句话", t14 == "楼下的事办完了吗", t14)
    chk("14i 提示词：只问「话说到一半断了 / 有新进展」，没有分类框架，不找新话头",
        "说到一半" in txt14 and "新进展" in txt14 and "距现在多久" in txt14
        and "先判断对话停在哪一种" not in txt14 and "新话头" not in txt14
        and '"nudge": false' in txt14)
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: ("m", '{"nudge": false, "say": ""}')):
        chk("14j 判断不追 → 空串", A._nudge_compose_v2(ndm, "private", "20002") == "")
    LOG.clear()
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: ("m", "要问一下吗")):
        chk("14k 输出不是 JSON → 不追并记日志",
            A._nudge_compose_v2(ndm, "private", "20002") == ""
            and any(x.startswith("AGENT_NUDGE_JSON_FAIL") for x in LOG), LOG[-2:])
    g14 = []
    with patched(A, call_glm_with_fallback=lambda ms, t, budget_sec=200: g14.append(ms) or (
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
                     call_glm_with_fallback=lambda ms, t, budget_sec=200: used.append(ms[0]["content"][:12]) or ("m", reply),
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
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: ("m", "E")):
        chk("17h 表情回应关着时模型仍答了 E → 按接话处理，不白白沉默",
            A._reply_gate_v2(NullDaemon(), _chat17, list(_SU), "智谱娘") == "Y")
    # 默认下旁观回复只挂了表情 → 请它说一句（重试一次）；重试仍是表情就保持原样，不硬凑
    c17 = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}', '{"say": ["哈哈这个好玩"]}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        LOG.clear()
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="Y")
    chk("17h' 默认：只挂了表情 → 重试一次并采用文字回复",
        A.parse_reply(_r)[0] == ["哈哈这个好玩"] and len(c17) == 2
        and any(x.startswith("AGENT_REACT_ONLY_RETRY") for x in LOG), (len(c17), LOG[-1:]))
    c17[:] = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}', '{"want": 1, "say": [], "emoji": 63}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="Y")
    chk("17h'' 重试后还是只挂表情 → 不再重试（保持原样，由后面的流程处理）",
        len(c17) == 2 and A._REACT_MARK_RE.search(_r) is not None, (len(c17), _r))
    c17[:] = []
    _seq = iter(['{"want": 1, "say": [], "emoji": 63}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=True,
                 reaction_allowed=lambda: (True, ["63"]),
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU], gate="E")
    chk("17h''' 允许只挂表情时 → 不重试", len(c17) == 1, len(c17))

    # 超长兜底：设置页的上限在 v2 下照样管用（按结构化输出重试，不走旧的文本协议重试）
    _LONG = '{"say": ["' + "很长" * 60 + '"]}'
    c17 = []
    _seq = iter([_LONG, '{"say": ["短的了"]}'])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 direct_must_reply=lambda: True,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        LOG.clear()
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU])
    chk("17i 闲聊回复超过上限 → 带上限要求重试一次，采用压缩后的",
        A.parse_reply(_r)[0] == ["短的了"] and len(c17) == 2 and "40 字" in c17[1][1]["content"]
        and any(x.startswith("AGENT_CHAT_LEN_SQUEEZE") for x in LOG), (len(c17), LOG[-1:]))
    c17[:] = []
    _seq = iter([_LONG])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _at17, [dict(x) for x in _SU])
    chk("17j 直接@的长回复不触发压缩重试", len(c17) == 1 and "很长" in _r, len(c17))
    c17[:] = []
    _seq = iter([_LONG])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 0,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
        _m, _r = A._generate_reply_v2(NullDaemon(), _chat17, [dict(x) for x in _SU])
    chk("17k 上限为 0 → 再长也不重试", len(c17) == 1 and "很长" in _r, len(c17))
    c17[:] = []
    _seq = iter([_LONG, "```json\n{\"say\": [], \"want\": 0}\n```"])
    with patched(A, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True, chat_max_len=lambda: 40,
                 call_glm_with_fallback=lambda ms, t, budget_sec=200: c17.append(ms) or ("m", next(_seq))):
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
    with patched(A, PROMPT_V2_FORCE=True, VCAP_FORCE=False,
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
        chk("18e 模型名留空 → 回到默认", _saved().get("vision_caption_model") == "glm-4.6v-flash")
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

    # ---------- 19. 识图速写不外泄（2026-11-24 实测：整段「（图：…）」发进了群） ----------
    for src_txt, want in [
        ("(图：东方灵梦Q版表情包：黑发大红蝴蝶结女孩半睁红眼啃西瓜，一脸不爽)", []),
        ("（图：一只猫）", []),
        ("哈哈这个好玩(图：柴犬表情包)", ["哈哈这个好玩"]),
        ("[Attached image/png: image.png]", []),
        ("笑死 [Attached image/jpeg: photo.jpg] 太逗了", ["笑死  太逗了"]),
        ("这小拳头举起来是在给自己打气还是在憋着要揍人啊",
         ["这小拳头举起来是在给自己打气还是在憋着要揍人啊"]),
    ]:
        _p19 = A.parse_reply(src_txt)[0]
        chk("19a 识图速写/附件回显不外泄：%r" % src_txt[:22], _p19 == want, _p19)
    chk("19b 提示词里的速写说明写了「别把这个写法带到回复里」",
        "别把「（图：…）」这几个字或它的写法带到回复里" in A._VCAP_NOTE)
    # 视觉模型不能被选作文本回复模型（它被这样调用会照抄速写、对纯文字臆造图片）
    chk("19c 视觉模型识别：4v/4.6v/4.5v/4.1v 都算，文本模型不算",
        A._looks_vision_model("glm-4v-flash") and A._looks_vision_model("glm-4.6v-flash")
        and A._looks_vision_model("glm-4.1v-thinking-flash")
        and not A._looks_vision_model("glm-5.4")
        and not A._looks_vision_model("deepseek-chat"))
    _calls19 = []
    with patched(A, _chain_candidates=lambda: [("k1", {"key": "x"}, "glm-4v-flash", 4, 0),
                                               ("k3", {"key": "y"}, "deepseek-chat", 4, 0)],
                 glm_chat=lambda model, msgs, key=None, extra_body=None: (
                     _calls19.append(model) or (200, {"choices": [{"message": {"content": "ok"}}]}))):
        A.call_glm_with_fallback([{"role": "system", "content": "s"},
                                  {"role": "user", "content": "u"}])
    chk("19d 文本回复跳过视觉模型，只用文本模型", _calls19 == ["deepseek-chat"], _calls19)

print("\n%d 项失败" % len(fails) + (": " + "; ".join(fails) if fails else "（全部通过）"))
sys.exit(1 if fails else 0)
