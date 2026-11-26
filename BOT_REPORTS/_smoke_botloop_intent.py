# -*- coding: utf-8 -*-
"""离线冒烟：互聊改判的自然语言化试点（WP-01，botloop_verdict_intent，默认关）。

P1 精确写法在开关关、P2 开关开两种状态下都照常生效（零模型调用、via=exact）。
P3 开关开时五类日常说法判定正确、回报参数取自待改判条目、回了固定文案：
   「不对，小张是真人，你搞错了」→human；「对小张是机器人」→bot；
   「我刚才说错了，小张是机器人」→bot；「小张其实是真人」→human；
   「刚刚那个是真人」（恰好一条最近登记）→human；指代 + 模型 target=null 也认。
P4 预筛按 QQ 号也能锁定候选。P5 「刚刚那个」有两条候选时不拦截、不调模型。
P6 消息不含任何候选名字也没有指代词时不调模型（桩零调用）。
P7 verdict=null / P8 confidence=0.5 / P9 模型抛异常 → 都不拦截（落回普通聊天）。
P10 非主人、P11 非私聊、P12 悄悄降温总开关关着 → 都不进这段。
P13 模型给的 target 和预筛候选对不上 → 不拦截。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_botloop_intent.py
"""
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_botloop_intent_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402

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
    """临时替换对象属性，退出时恢复（照 _smoke_botloop_quiet.py 的做法）。"""
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
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None


def alog_of(tag):
    return [l for l in ALOG if tag in l]


RCPATH = A.REPLY_CONFIG_PATH
_RCPAD = [0]


def write_reply_cfg(obj):
    _RCPAD[0] += 1    # 追加不同数量换行：保证 mtime+size 签名必变
    pathlib.Path(RCPATH).write_text(json.dumps(obj, ensure_ascii=False)
                                    + ("\n" * _RCPAD[0]), encoding="utf-8")


def rm_reply_cfg():
    try:
        os.remove(RCPATH)
    except OSError:
        pass


class NullDaemon(object):
    """agent 侧桩：只显式提供 process 会用到的方法。"""

    def __init__(self, history=None, group_names=None):
        self._history = list(history or [])
        self._group_names = dict(group_names or {})
        self.acked = []
        self.sent = []      # (chat_type, chat_id, texts)
        self.reports = []

    def history(self, chat_type, chat_id, limit=30):
        return self._history[-limit:] if limit else list(self._history)

    def group_names(self):
        return dict(self._group_names)

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append((chat_type, chat_id, list(texts)))

    def botloop_report(self, chat_type, chat_id, sender_id, confirmed,
                       cooldown_min=60, precheck=False, quiet=False,
                       quiet_min=15, asked=False, probation=False):
        rec = {"chat_type": chat_type, "chat_id": chat_id,
               "sender_id": sender_id, "confirmed": confirmed,
               "cooldown_min": cooldown_min, "precheck": precheck}
        if quiet:
            rec["quiet"] = True
            rec["quiet_min"] = quiet_min
        if asked:
            rec["asked"] = True
        if probation:
            rec["probation"] = True
        self.reports.append(rec)
        return {"ok": True}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


def priv_msg(text, msg_id=8001, **kw):
    m = {"id": msg_id, "chat_type": "private", "chat_id": "10086",
         "chat_name": "", "sender_id": "10086", "sender_name": "主人",
         "text": text, "trigger_reason": "private", "message_id": "p%d"
         % msg_id, "image_urls": [], "at_me": False, "at_users": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0}
    m.update(kw)
    return m


def group_msg(text, mid, trig="chat_random", sender="777", msg_id=7001, **kw):
    m = {"id": msg_id, "chat_type": "group", "chat_id": "555001",
         "chat_name": "冒烟群", "sender_id": sender, "sender_name": "小张",
         "text": text, "trigger_reason": trig, "message_id": mid,
         "image_urls": [], "at_me": False, "at_users": [], "has_reply": False,
         "reply_to": None, "reply_text": "", "face_count": 0}
    m.update(kw)
    return m


def glm_script(resps, calls):
    """按调用次序回放的 glm 桩；str→(模型名, 文本)；Exception→原样抛出。"""
    def _glm(messages, text_ctx="", budget_sec=200):
        calls.append(messages)
        r = resps[min(len(calls) - 1, len(resps) - 1)]
        if isinstance(r, BaseException):
            raise r
        return ("m", r) if isinstance(r, str) else r
    return _glm


def register_one():
    """清空待改判表后登记一条「小张/777」（群 555001），并清通知限频表。"""
    A._BOTLOOP_QUIET_PENDING.clear()
    A._BOTLOOP_QUIET_NOTIFY_TS.clear()
    A._botloop_quiet_register(group_msg("x", "reg", sender="777"), 15)


# 正常处理路径的最小补丁集（照 _smoke_botloop_quiet.py 的 _NP）
_NP = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
           REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
           vision_enabled=lambda: False,
           chat_anti_interrupt_enabled=lambda: True,
           lean_ctx_on=lambda: True, reply_v2_on=lambda: False,
           _cap_chat_allowed=lambda ct, cid: False,
           owner_qq=lambda: "10086",
           owner_command=lambda m, d: (False, ""))

rm_reply_cfg()

# ===== P1 精确写法：开关关（默认）照常生效 =====

register_one()
write_reply_cfg({"botloop_quiet_mode": True, "botloop_cooldown_min": 77})
dm1 = NullDaemon()
_calls1 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls1), **_NP):
    A.PLUGIN_HOST.daemon = dm1
    ALOG.clear()
    A.process(dm1, priv_msg("真人 小张", msg_id=8101))
chk("P1a 开关关：精确写法照常改判（回报参数取自条目、fixed 文案、零模型调用）",
    dm1.reports == [{"chat_type": "group", "chat_id": "555001",
                     "sender_id": "777", "confirmed": False,
                     "cooldown_min": 77, "precheck": False}]
    and dm1.sent == [("private", "10086",
                      ["好，把「小张」当真人，这就恢复搭话。"])]
    and _calls1 == [], (dm1.reports, dm1.sent, _calls1))
chk("P1b 日志带 via=exact", any(
    l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=human via=exact"
    for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT")),
    alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))

# ===== P2 精确写法：开关开也不受影响（精确优先，不走模型） =====

A._botloop_quiet_register(group_msg("x", "reg2", sender="777"), 15)
write_reply_cfg({"botloop_quiet_mode": True, "botloop_cooldown_min": 77,
                 "botloop_verdict_intent": True})
dm2 = NullDaemon()
_calls2 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls2), **_NP):
    A.PLUGIN_HOST.daemon = dm2
    ALOG.clear()
    A.process(dm2, priv_msg("机器人 小张", msg_id=8102))
chk("P2a 开关开：精确写法照常改判机器人（confirmed=True、零模型调用）",
    dm2.reports and dm2.reports[0]["confirmed"] is True
    and dm2.reports[0]["precheck"] is False
    and dm2.sent == [("private", "10086",
                      ["好，「小张」按机器人处理，这段时间不再搭话。"])]
    and _calls2 == [], (dm2.reports, dm2.sent, _calls2))
chk("P2b 日志带 via=exact、无 INTENT 日志", any(
    l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=bot via=exact"
    for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))
    and not alog_of("AGENT_BOTLOOP_INTENT"),
    (alog_of("AGENT_BOTLOOP_OWNER_VERDICT"), alog_of("AGENT_BOTLOOP_INTENT")))

# ===== P3 开关开：五类日常说法 =====

_HUMAN_JSON = '{"verdict": "human", "target": "小张", "confidence": 0.9}'
_BOT_JSON = '{"verdict": "bot", "target": "小张", "confidence": 0.92}'
_HUMAN_REPLY = "好，把「小张」当真人，这就恢复搭话。"
_BOT_REPLY = "好，「小张」按机器人处理，这段时间不再搭话。"


def intent_case(text, resp, msg_id, want_bot):
    """登记一条 → 主人私聊说 text → 返回 (daemon, 调用列表)。"""
    A._BOTLOOP_QUIET_PENDING.clear()
    A._botloop_quiet_register(group_msg("x", "reg", sender="777"), 15)
    dm = NullDaemon()
    calls = []
    with patched(A, call_glm_with_fallback=glm_script(
            [resp, "嗯嗯"], calls), **_NP):
        A.PLUGIN_HOST.daemon = dm
        ALOG.clear()
        A.process(dm, priv_msg(text, msg_id=msg_id))
    return dm, calls


for _i, (_text, _resp, _want_bot, _tag) in enumerate((
        ("不对，小张是真人，你搞错了", _HUMAN_JSON, False, "否定上一句"),
        ("对小张是机器人", _BOT_JSON, True, "对…是机器人"),
        ("我刚才说错了，小张是机器人", _BOT_JSON, True, "承认说错"),
        ("小张其实是真人", _HUMAN_JSON, False, "其实是真人"))):
    dm, calls = intent_case(_text, _resp, 8110 + _i, _want_bot)
    chk("P3%s %s：回报参数取自待改判条目（confirmed=%s）"
        % ("abcde"[_i], _tag, _want_bot),
        dm.reports == [{"chat_type": "group", "chat_id": "555001",
                        "sender_id": "777", "confirmed": _want_bot,
                        "cooldown_min": 77, "precheck": False}], dm.reports)
    chk("P3%s %s：回固定文案、恰好一次模型调用（意图识别）"
        % ("abcde"[_i], _tag),
        dm.sent == [("private", "10086",
                     [_BOT_REPLY if _want_bot else _HUMAN_REPLY])]
        and len(calls) == 1
        and str(calls[0][0]["content"]).startswith("主人在私聊里对机器人说"),
        (dm.sent, len(calls)))
    chk("P3%s %s：OWNER_VERDICT 带 via=intent、INTENT 日志带打码 target"
        % ("abcde"[_i], _tag),
        any(l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=%s via=intent"
            % ("bot" if _want_bot else "human")
            for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))
        and len(alog_of("AGENT_BOTLOOP_INTENT")) == 1
        and alog_of("AGENT_BOTLOOP_INTENT")[0].startswith(
            "AGENT_BOTLOOP_INTENT kind=%s target=**77 conf=0.9"
            % ("bot" if _want_bot else "human")),
        (alog_of("AGENT_BOTLOOP_OWNER_VERDICT"), alog_of("AGENT_BOTLOOP_INTENT")))
    chk("P3%s %s：消息被 ack、待改判条目保留" % ("abcde"[_i], _tag),
        dm.acked == [8110 + _i]
        and A._botloop_quiet_find("小张") is not None,
        (dm.acked,))

# P3e/P3f 指代（恰好一条最近登记）：名字命中 → target 取那条；target=null 也认
register_one()
dm3e = NullDaemon()
_calls3e = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "human", "target": "小张", "confidence": 0.85}', "嗯嗯"],
        _calls3e), **_NP):
    A.PLUGIN_HOST.daemon = dm3e
    ALOG.clear()
    A.process(dm3e, priv_msg("刚刚那个是真人", msg_id=8120))
chk("P3e 「刚刚那个是真人」（恰好一条登记）：human、回报正确、固定文案",
    dm3e.reports == [{"chat_type": "group", "chat_id": "555001",
                      "sender_id": "777", "confirmed": False,
                      "cooldown_min": 77, "precheck": False}]
    and dm3e.sent == [("private", "10086", [_HUMAN_REPLY])]
    and dm3e.acked == [8120], (dm3e.reports, dm3e.sent, dm3e.acked))

dm3f = NullDaemon()
_calls3f = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "human", "target": null, "confidence": 0.86}', "嗯嗯"],
        _calls3f), **_NP):
    A.PLUGIN_HOST.daemon = dm3f
    ALOG.clear()
    A.process(dm3f, priv_msg("刚才那个是真人", msg_id=8121))
chk("P3f 指代 + 模型 target=null：认预筛出的那条、human 改判",
    dm3f.reports and dm3f.reports[0]["confirmed"] is False
    and dm3f.sent == [("private", "10086", [_HUMAN_REPLY])]
    and len(_calls3f) == 1, (dm3f.reports, dm3f.sent, len(_calls3f)))

dm3g = NullDaemon()
_calls3g = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "bot", "target": null, "confidence": 0.88}', "嗯嗯"],
        _calls3g), **_NP):
    A.PLUGIN_HOST.daemon = dm3g
    ALOG.clear()
    A.process(dm3g, priv_msg("小张是机器人，没错", msg_id=8123))
chk("P3g 直接点名 + 模型 target=null：认预筛出的那条、bot 改判",
    dm3g.reports and dm3g.reports[0]["confirmed"] is True
    and dm3g.sent == [("private", "10086", [_BOT_REPLY])]
    and len(_calls3g) == 1, (dm3g.reports, dm3g.sent, len(_calls3g)))

# ===== P4 预筛按 QQ 号锁定候选 =====

register_one()
dm4 = NullDaemon()
_calls4 = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "human", "target": "777", "confidence": 0.91}', "嗯嗯"],
        _calls4), **_NP):
    A.PLUGIN_HOST.daemon = dm4
    ALOG.clear()
    A.process(dm4, priv_msg("把777当真人吧", msg_id=8122))
chk("P4 按 QQ 号指认：human 改判、固定文案",
    dm4.reports and dm4.reports[0]["confirmed"] is False
    and dm4.sent == [("private", "10086", [_HUMAN_REPLY])]
    and len(_calls4) == 1, (dm4.reports, dm4.sent, len(_calls4)))

# ===== P5 「刚刚那个」有两条候选 → 不拦截、不调模型 =====

A._BOTLOOP_QUIET_PENDING.clear()
A._botloop_quiet_register(group_msg("x", "reg", sender="777"), 15)
A._botloop_quiet_register(group_msg("x", "reg2", sender="888",
                                    sender_name="小AI"), 15)
dm5 = NullDaemon()
_calls5 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯嗯"], _calls5), **_NP):
    A.PLUGIN_HOST.daemon = dm5
    ALOG.clear()
    A.process(dm5, priv_msg("刚刚那个是真人", msg_id=8123))
chk("P5a 两条候选时指代不命中：无回报、无固定文案、无改判日志",
    dm5.reports == []
    and all("当真人" not in t for _c, _i, ts in dm5.sent for t in ts)
    and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not any(l.startswith("AGENT_BOTLOOP_INTENT kind=") for l in ALOG),
    (dm5.reports, dm5.sent, alog_of("AGENT_BOTLOOP_INTENT")))
chk("P5b 预筛失败不调模型：第一条模型调用就是正常聊天（不是意图提示词）",
    len(_calls5) >= 1
    and not str(_calls5[0][0]["content"]).startswith("主人在私聊里对机器人说"),
    (len(_calls5), str(_calls5[0][0]["content"])[:40]))

# ===== P6 不含候选名字也没有指代词 → 意图函数不调模型 =====

_calls6 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls6)):
    _r6 = A._intent_botloop_verdict("今天天气怎么样",
                                    A._botloop_quiet_candidates())
chk("P6a 预筛不中直接返回 None（零模型调用）",
    _r6 is None and _calls6 == [], (_r6, _calls6))
register_one()
dm6 = NullDaemon()
_calls6b = []
with patched(A, call_glm_with_fallback=glm_script(["嗯嗯"], _calls6b), **_NP):
    A.PLUGIN_HOST.daemon = dm6
    ALOG.clear()
    A.process(dm6, priv_msg("帮我查个东西", msg_id=8124))
chk("P6b 不含名字也无指代词：消息落回普通聊天、无改判日志",
    dm6.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT") and len(_calls6b) >= 1
    and not str(_calls6b[0][0]["content"]).startswith("主人在私聊里对机器人说"),
    (dm6.reports, len(_calls6b), alog_of("AGENT_BOTLOOP_INTENT")))

# ===== P7/P8/P9 verdict=null / confidence 0.5 / 模型抛异常 → 不拦截 =====

register_one()
dm7 = NullDaemon()
_calls7 = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": null, "target": null, "confidence": 0.9}', "嗯嗯"],
        _calls7), **_NP):
    A.PLUGIN_HOST.daemon = dm7
    ALOG.clear()
    A.process(dm7, priv_msg("小张挺有意思的", msg_id=8125))
chk("P7 verdict=null：不拦截、落回普通聊天",
    dm7.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT")
    and not any("挺有意思" in t for _c, _i, ts in dm7.sent for t in ts)
    and len(_calls7) >= 1
    and str(_calls7[0][0]["content"]).startswith("主人在私聊里对机器人说"),
    (dm7.reports, len(_calls7), alog_of("AGENT_BOTLOOP_INTENT")))

register_one()
dm8 = NullDaemon()
_calls8 = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "human", "target": "小张", "confidence": 0.5}', "嗯嗯"],
        _calls8), **_NP):
    A.PLUGIN_HOST.daemon = dm8
    ALOG.clear()
    A.process(dm8, priv_msg("小张其实是真人", msg_id=8126))
chk("P8 confidence=0.5：不拦截、落回普通聊天",
    dm8.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT") and len(_calls8) >= 1,
    (dm8.reports, alog_of("AGENT_BOTLOOP_INTENT")))

register_one()
dm9 = NullDaemon()
_calls9 = []
with patched(A, call_glm_with_fallback=glm_script(
        [RuntimeError("模型炸了"), "嗯嗯"], _calls9), **_NP):
    A.PLUGIN_HOST.daemon = dm9
    ALOG.clear()
    A.process(dm9, priv_msg("小张其实是真人", msg_id=8127))
chk("P9 模型抛异常：不拦截、有 INTENT_FAIL、落回普通聊天",
    dm9.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not any(l.startswith("AGENT_BOTLOOP_INTENT kind=") for l in ALOG)
    and len(alog_of("AGENT_BOTLOOP_INTENT_FAIL")) == 1
    and len(_calls9) >= 1, (dm9.reports, alog_of("AGENT_BOTLOOP_INTENT_FAIL")))

# ===== P13 模型 target 和预筛候选对不上 → 不拦截 =====

register_one()
dm13 = NullDaemon()
_calls13 = []
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "human", "target": "老李", "confidence": 0.95}', "嗯嗯"],
        _calls13), **_NP):
    A.PLUGIN_HOST.daemon = dm13
    ALOG.clear()
    A.process(dm13, priv_msg("不对，老李是真人，你搞错了", msg_id=8128))
chk("P13 target 对不上：不拦截、无 INTENT 日志、落回普通聊天",
    dm13.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT") and len(_calls13) >= 1,
    (dm13.reports, alog_of("AGENT_BOTLOOP_INTENT")))

# ===== P10/P11/P12 非主人 / 非私聊 / 总开关关 → 都不进这段 =====

register_one()
dm10 = NullDaemon()
_calls10 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯嗯"], _calls10), **_NP):
    A.PLUGIN_HOST.daemon = dm10
    ALOG.clear()
    A.process(dm10, priv_msg("不对，小张是真人，你搞错了", msg_id=8129,
                             sender_id="99999", chat_id="99999"))
chk("P10 非主人：无回报、无改判/意图日志",
    dm10.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT"), (dm10.reports,))

register_one()
dm11 = NullDaemon()
_calls11 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯嗯"], _calls11), **_NP):
    A.PLUGIN_HOST.daemon = dm11
    ALOG.clear()
    A.process(dm11, group_msg("不对，小张是真人，你搞错了", "g11",
                              trig="chat_random", msg_id=8130))
chk("P11 非私聊（群消息）：无回报、无改判/意图日志",
    dm11.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT"), (dm11.reports,))

register_one()
rm_reply_cfg()    # 悄悄降温总开关关（botloop_quiet_mode 缺键＝关）
dm12 = NullDaemon()
_calls12 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯嗯"], _calls12), **_NP):
    A.PLUGIN_HOST.daemon = dm12
    ALOG.clear()
    A.process(dm12, priv_msg("不对，小张是真人，你搞错了", msg_id=8131))
chk("P12 悄悄降温总开关关：整个改判段不进（连精确写法也不拦）",
    dm12.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not alog_of("AGENT_BOTLOOP_INTENT")
    and not any("当真人" in t for _c, _i, ts in dm12.sent for t in ts),
    (dm12.reports, dm12.sent))

# ===== 单元级：意图函数返回形状与开关读取 =====

register_one()
with patched(A, call_glm_with_fallback=glm_script(["嗯"], [])):
    _r13b = A._intent_botloop_verdict("没有候选的话", [])
chk("U1 无候选（空表）→ None 且不调模型", _r13b is None)
_cands = A._botloop_quiet_candidates()
with patched(A, call_glm_with_fallback=glm_script(
        ['{"verdict": "bot", "target": "小张", "confidence": 1}'], [])):
    _r13c = A._intent_botloop_verdict("对小张是机器人", _cands)
chk("U2 返回形状 (kind, target条目)",
    isinstance(_r13c, tuple) and _r13c[0] == "bot"
    and _r13c[1].get("sender_id") == "777", _r13c)
chk("U3 开关默认关", A._botloop_verdict_intent_on() is False)
write_reply_cfg({"botloop_verdict_intent": True})
chk("U4 开关打开可读到", A._botloop_verdict_intent_on() is True)

rm_reply_cfg()

print("\n%d 项通过，%d 项失败" % (n_pass, len(fails))
      + (": " + "; ".join(fails) if fails else "（全部通过）"))

# 一次性临时数据目录：先关日志句柄（Windows 下打开的文件删不掉），再尽量删掉
try:
    import logging
    logging.shutdown()
    shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
except Exception:
    pass
sys.exit(1 if fails else 0)
