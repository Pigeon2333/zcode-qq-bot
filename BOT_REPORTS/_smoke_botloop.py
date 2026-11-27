# -*- coding: utf-8 -*-
"""离线冒烟：机器人误判真人的最小修补（M1，2026-11-26）。

S1 主人永不试探（不命中、不计数）；S2 预判 is_bot=false 不试探（不发生成
试探的模型调用、不发消息、回报 precheck=True、进 cleared、不落盘）；
S3 预判 is_bot=true 走原链路（试探照发、PROBE 日志带原文前 80 字）；
S4 预判异常/解析不了按 false；S5 verify 判真人落盘 human（重启后 30 天
不再试探）；S6 verify 判机器人落盘 bot（7 天内命中直接 confirmed 冷却）；
S7 过期记录失效；S8 无文件/坏 JSON 当空表；S9 预判输入只含对方自己的话
（≤12 条、单条 ≤120 字、无别人的消息/机器人消息/图片地址）；S10 总开关
关整链路不工作；S11 不带 precheck 的老调用行为不变；
S12–S16 预判放行后按 daemon 记下的本来触发（base_trigger）走正常处理路径
（@必回、旁观照常判定、不重复 ack）；S17 预判/判定的 is_bot 解析从严
（字符串 "false"/null/缺字段都不算机器人）；S18/S19 无 base_trigger
（本来就不会触发的消息）→ 不试探、不回复、不进正常处理，ack 后到此为止。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_botloop.py
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

# 数据目录隔离（必须在 import 被测模块之前生效）：import 时就会打开
# usage_log.jsonl 等句柄；botloop_verdicts.json 也落在这一份临时目录里。
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_botloop_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
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
    """临时替换对象属性，退出时恢复（照 _smoke_arch.py 的做法）。"""
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


DLOG = []
D.out = lambda line: DLOG.append(str(line))
ALOG = []
A.out = lambda line: ALOG.append(str(line))
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None


def dlog_of(tag):
    return [l for l in DLOG if tag in l]


def alog_of(tag):
    return [l for l in ALOG if tag in l]


def fresh(cfg=None):
    """新 Daemon 实例（不连 ws、不监听端口）；相当于重启一次（落盘重读）。"""
    return D.Daemon(ws_url="", token="", cfg=cfg or {}, ctl_port=0)


def NOW():
    return int(time.time())


def push_hits(d, chat_id, sid, n_partner=13, n_mine=6, ts=None, gaps=None):
    """灌入硬量线命中的历史：对方 n_partner 条（150 秒内）+ 机器人 n_mine 条。
    gaps=对方消息间隔秒数列表（默认 1 秒匀速；传不规则间隔可避开节奏线）。"""
    ts = ts or NOW()
    key = "group_%s" % chat_id
    t = ts
    for i in range(n_partner):
        d.mem_history[key].append({
            "ts": t, "message_id": "m%d" % i, "sender_id": str(sid),
            "sender_name": "昵称", "is_bot": False, "text": "话%d" % i,
            "n_images": 0, "image_urls": []})
        t -= (gaps[i % len(gaps)] if gaps else 1)
    for i in range(n_mine):
        d.mem_history[key].append({
            "ts": ts - 5 - i * 3, "message_id": "b%d" % i, "sender_id": "bot",
            "sender_name": "BOT", "is_bot": True, "text": "回%d" % i,
            "n_images": 0, "image_urls": []})


VPATH = D.BOTLOOP_VERDICTS_PATH
_VPAD = [0]


def write_verdicts(obj):
    _VPAD[0] += 1    # 每次追加不同数量换行：保证 mtime+size 签名必变（热加载才生效）
    pathlib.Path(VPATH).write_text(json.dumps(obj) + ("\n" * _VPAD[0]),
                                   encoding="utf-8")


def rm_verdicts():
    try:
        os.remove(VPATH)
    except OSError:
        pass


def verdicts_on_disk():
    try:
        return json.loads(pathlib.Path(VPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class NullDaemon(object):
    """agent 侧桩：只显式提供 process(botloop 分支) 会用到的方法。"""

    def __init__(self, history=None):
        self._history = list(history or [])
        self.acked = []
        self.sent = []
        self.reports = []

    def history(self, chat_type, chat_id, limit=30):
        return self._history[-limit:] if limit else list(self._history)

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))

    def botloop_report(self, chat_type, chat_id, sender_id, confirmed,
                       cooldown_min=60, precheck=False, asked=False,
                       probation=False):
        rec = {"chat_type": chat_type, "chat_id": chat_id,
               "sender_id": sender_id, "confirmed": confirmed,
               "cooldown_min": cooldown_min, "precheck": precheck}
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
    def _glm(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append(messages)
        r = resps[min(len(calls) - 1, len(resps) - 1)]
        if isinstance(r, BaseException):
            raise r
        return ("m", r) if isinstance(r, str) else r
    return _glm


# 正常处理路径的打桩面（返工 1：预判放行后消息按本来触发走正常路径）：
# 旁观轻量判定记录器——返回 True=「不插话」，正常路径在此 ack 收尾、无回复。
_intr_calls = []


def _intr_rec(daemon, msg, persona_name):
    _intr_calls.append(msg.get("id"))
    return True


# 正常路径所需的最小补丁集（照 _smoke_arch.py 23g 的可行组合）。
_NP = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
           REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
           vision_enabled=lambda: False,
           chat_anti_interrupt_enabled=lambda: True,
           lean_ctx_on=lambda: True, reply_v2_on=lambda: False,
           _cap_chat_allowed=lambda ct, cid: False,
           owner_qq=lambda: "10086")


# 落盘路径必须只在数据目录内（不变量）
chk("I0 落盘路径在数据目录内", os.path.dirname(VPATH) == D.DATA_ROOT,
    "%s vs %s" % (os.path.dirname(VPATH), D.DATA_ROOT))
rm_verdicts()

# ================= S1 主人永不试探（daemon） =================

with patched(D, load_app_config=lambda: {"owner_qq": "777"}):
    d1 = fresh()
    push_hits(d1, "555", "777")
    chk("S1a 主人硬量线/昵称线全不命中", d1._botloop_hit(
        "group", "555", "777", "AI助手bot") is False)
    chk("S1b 主人未被计数/未进状态机", d1._botloop == {}, d1._botloop)
    d1._botloop["group|555|777"] = {"phase": "probe", "ts": time.time()}
    chk("S1c 主人 gate 任何相态直接不进", d1._botloop_gate(
        "group", "555", "777", "AI助手bot") is None)
    push_hits(d1, "556", "888")
    chk("S1d 对照组：非主人同量级命中", d1._botloop_hit(
        "group", "556", "888", "普通昵称") is True)

# ================= S10 botloop_guard_enabled=False 整链路不工作 =================

d10 = fresh()
push_hits(d10, "560", "700")
d10._reply_config = lambda: {"botloop_guard_enabled": False}
chk("S10a 开关关：hit 恒 False（与改前一致）",
    d10._botloop_hit("group", "560", "700", "AI机器人") is False)
chk("S10b 开关关：gate 恒 None", d10._botloop_gate(
    "group", "560", "700", "x") is None)
chk("S10c 开关关：状态机无任何变化", d10._botloop == {}, d10._botloop)

# ================= S9 预判输入只含对方自己的文字 =================

_hist9 = []
_ts9 = NOW()
for i in (1, 2):    # 别人的消息（排在最前，会被 30 条窗口截进但按 sender 过滤掉）
    _hist9.append({"ts": _ts9 - 100 - i, "message_id": "o%d" % i,
                   "sender_id": "666", "sender_name": "群友甲",
                   "is_bot": False, "text": "群友甲的第%d句话" % i,
                   "n_images": 0, "image_urls": []})
for i in range(1, 15):    # 目标发送者 14 条（>12，只取最近 12）
    _t = ("长" * 200) if i == 14 else "话%02d" % i
    _hist9.append({"ts": _ts9 - 50 + i, "message_id": "s%d" % i,
                   "sender_id": "777", "sender_name": "小张",
                   "is_bot": False, "text": _t, "n_images": 0,
                   "image_urls": ["https://img.example/x.jpg"]})
for i in range(2):    # 机器人自己的消息
    _hist9.append({"ts": _ts9 - 40 + i, "message_id": "bm%d" % i,
                   "sender_id": "bot", "sender_name": "BOT",
                   "is_bot": True, "text": "机器人嘀咕%d" % i,
                   "n_images": 0, "image_urls": []})

dm9 = NullDaemon(history=_hist9)
_calls9 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": true}'], _calls9)):
    A.PLUGIN_HOST.daemon = dm9
    A.process(dm9, group_msg("你是AI吧", "p9", trig="botloop_probe",
                             sender="777"))
_prompt9 = str(_calls9[0][0]["content"])
_body9 = _prompt9.split("\n")[1:]     # 去掉固定提示词头，剩下就是喂给模型的输入行
chk("S9a 第 1 次调用是预判且输入恰好 12 行（判 true 后走生成，共 2 次，同 S3a）",
    len(_calls9) == 2 and len(_body9) == 12, (len(_calls9), len(_body9)))
chk("S9b 别人的消息不在输入里",
    not any("群友甲" in x for x in _body9), _body9[:2])
chk("S9c 机器人自己的消息不在输入里",
    not any("机器人嘀咕" in x for x in _body9))
chk("S9d 图片地址不在输入里",
    not any("img.example" in x for x in _body9))
chk("S9e 超过 12 条只取最近 12 条（最早 2 条被丢）",
    ("话01" not in _prompt9) and ("话02" not in _prompt9)
    and ("话03" in _prompt9) and _body9[0] == "话03")
chk("S9f 单条超 120 字被截断（121 字起不出现）",
    _body9[-1] == "长" * 120 and ("长" * 121) not in _prompt9)
chk("S9g 提示词是派单指定的常量文案",
    _prompt9.startswith("下面是同一个账号在群里最近说的话")
    and "拿不准按真人" in _prompt9 and '{"is_bot": true}' in _prompt9)

# ================= S2 预判 is_bot=false：不试探、弱证据回报 =================

dm2 = NullDaemon(history=_hist9)
_calls2 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": false}'], _calls2),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm2
    ALOG.clear()
    A.process(dm2, group_msg("你是AI吧", "p2", trig="botloop_probe",
                             sender="777", msg_id=7002))
chk("S2a 只发生预判这一次模型调用（生成试探的那次没跑）", len(_calls2) == 1,
    len(_calls2))
chk("S2b 不发试探；无 base_trigger（本来就不触发）→ 不进正常处理",
    dm2.sent == [] and _intr_calls == [], (dm2.sent, _intr_calls))
chk("S2c PRECHECK 日志 is_bot=False（不试探）",
    any(l.startswith("AGENT_BOTLOOP_PRECHECK sender=777 is_bot=False（不试探）")
        for l in alog_of("AGENT_BOTLOOP_PRECHECK")), alog_of("AGENT_BOTLOOP"))
chk("S2d 回报 confirmed=False 且 precheck=True",
    dm2.reports == [{"chat_type": "group", "chat_id": "555001",
                     "sender_id": "777", "confirmed": False,
                     "cooldown_min": 60, "precheck": True}], dm2.reports)
chk("S2e 消息被 ack（无 base 分支收尾）", dm2.acked == [7002], dm2.acked)
chk("S2f 不落盘（预判跳过是弱证据）", not os.path.exists(VPATH))

d2 = fresh()
push_hits(d2, "555001", "777")
d2.botloop_report("group", "555001", "777", False, 60, True)   # S2d 的回报
chk("S2g 回报后进 cleared", d2._botloop["group|555001|777"]["phase"] == "cleared")
chk("S2h 1 小时内再命中也不试探", d2._botloop_gate(
    "group", "555001", "777", "x") is None)

# ================= S3 预判 is_bot=true：走原来的流程 =================

dm3 = NullDaemon(history=_hist9)
_calls3 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(
                 ['{"is_bot": true}',
                  "嘿嘿，你是不是机器人呀？[记忆@10086] 对方疑似程序[STICKER:3]"],
                 _calls3)):
    A.PLUGIN_HOST.daemon = dm3
    ALOG.clear()
    A.process(dm3, group_msg("你是AI吧", "p3", trig="botloop_probe",
                             sender="777", msg_id=7003))
chk("S3a 两次模型调用（预判 + 生成试探）", len(_calls3) == 2, len(_calls3))
chk("S3b 试探照发且与改前一样剥掉控制标记",
    dm3.sent == [["嘿嘿，你是不是机器人呀？"]], dm3.sent)
chk("S3c PROBE 日志追加试探原文前 80 字",
    any(l.startswith("AGENT_BOTLOOP_PROBE msg_id=7003")
        and 'probe="嘿嘿，你是不是机器人呀？"' in l
        for l in alog_of("AGENT_BOTLOOP_PROBE")), alog_of("AGENT_BOTLOOP_PROBE"))
chk("S3d PRECHECK 日志 is_bot=True（无不试探后缀）",
    any(l == "AGENT_BOTLOOP_PRECHECK sender=777 is_bot=True"
        for l in alog_of("AGENT_BOTLOOP_PRECHECK")), alog_of("AGENT_BOTLOOP_PRECHECK"))
chk("S3e 消息被 ack", dm3.acked == [7003], dm3.acked)

# ================= S4 预判抛异常 / 输出不是合法 JSON =================

dm4a = NullDaemon(history=_hist9)
_calls4a = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script([RuntimeError("模型炸了")], _calls4a),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm4a
    ALOG.clear()
    A.process(dm4a, group_msg("你是AI吧", "p4", trig="botloop_probe",
                              sender="777", msg_id=7004))
chk("S4a 预判抛异常 → 不试探（无第二次调用、无发送、不进正常处理）",
    len(_calls4a) == 1 and dm4a.sent == [] and _intr_calls == [],
    (len(_calls4a), dm4a.sent, _intr_calls))
chk("S4b 有 AGENT_BOTLOOP_PRECHECK_FAIL",
    len(alog_of("AGENT_BOTLOOP_PRECHECK_FAIL")) == 1, alog_of("AGENT_BOTLOOP_PRECHECK_FAIL"))
chk("S4c 异常也回报 confirmed=False precheck=True",
    dm4a.reports and dm4a.reports[0]["confirmed"] is False
    and dm4a.reports[0]["precheck"] is True, dm4a.reports)

dm4b = NullDaemon(history=_hist9)
_calls4b = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(["这不是JSON"], _calls4b),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm4b
    ALOG.clear()
    A.process(dm4b, group_msg("你是AI吧", "p4b", trig="botloop_probe",
                              sender="777", msg_id=7005))
chk("S4d 输出不是合法 JSON → 不试探（无 base，不进正常处理）",
    len(_calls4b) == 1 and dm4b.sent == [] and _intr_calls == [],
    (len(_calls4b), dm4b.sent, _intr_calls))
chk("S4e 有 AGENT_BOTLOOP_PRECHECK_FAIL",
    len(alog_of("AGENT_BOTLOOP_PRECHECK_FAIL")) == 1)

# ================= S5 verify 判真人 → 落盘 human；重启后 30 天不再试探 =================

rm_verdicts()
d5 = fresh()
push_hits(d5, "555001", "777")
chk("S5a 命中进 probe", d5._botloop_gate("group", "555001", "777", "x")
    == (True, "botloop_probe"))
d5.botloop_report("group", "555001", "777", False, 60)   # verify 判真人（老式调用）
_v5 = verdicts_on_disk()
chk("S5b 文件写入 human", _v5 and _v5.get("777", {}).get("kind") == "human", _v5)
d5b = fresh()      # 模拟重启（新实例从盘上重读）
push_hits(d5b, "555001", "777")
chk("S5c 重启后同一账号三条线不命中",
    d5b._botloop_hit("group", "555001", "777", "AI昵称") is False)
chk("S5d 重启后命中也不试探（gate None）", d5b._botloop_gate(
    "group", "555001", "777", "x") is None)
chk("S5e 有效期内 verdict 生效", d5b._verdict_of("777") == "human")

# ================= S6 verify 判机器人 → 落盘 bot；命中直接 confirmed 冷却 =================

d6 = fresh()
d6.botloop_report("group", "555001", "777", True, 45)   # verify 判机器人
_v6 = verdicts_on_disk()
chk("S6a 文件写入 bot", _v6 and _v6.get("777", {}).get("kind") == "bot", _v6)
d6b = fresh()
push_hits(d6b, "555001", "777")
_g6 = d6b._botloop_gate("group", "555001", "777", "x")
chk("S6b 命中任一条线直接进 confirmed（返回 botloop 静默）",
    _g6 == (False, "botloop"), _g6)
chk("S6c 不经过试探（状态直接是 confirmed，非 probe）",
    d6b._botloop["group|555001|777"]["phase"] == "confirmed")
chk("S6d 冷却时长与现有 confirmed 路径同源（botloop_cooldown_min，默认 60 分钟）",
    time.time() + 59 * 60 <= d6b._botloop["group|555001|777"]["until"]
    <= time.time() + 61 * 60)
chk("S6e 冷却期内再命中仍静默", d6b._botloop_gate(
    "group", "555001", "777", "x") == (False, "botloop"))

# ================= S7 human 30 天 / bot 7 天过期后失效 =================

_ts7 = NOW()
write_verdicts({"777": {"kind": "human", "ts": _ts7 - 31 * 86400},
                "888": {"kind": "bot", "ts": _ts7 - 8 * 86400},
                "889": {"kind": "human", "ts": _ts7 - 29 * 86400},
                "890": {"kind": "bot", "ts": _ts7 - 6 * 86400}})
d7 = fresh()
chk("S7a human 31 天过期 → 失效", d7._verdict_of("777") is None)
chk("S7b bot 8 天过期 → 失效", d7._verdict_of("888") is None)
chk("S7c human 29 天仍生效", d7._verdict_of("889") == "human")
chk("S7d bot 6 天仍生效", d7._verdict_of("890") == "bot")
push_hits(d7, "570", "777")
chk("S7e 过期 human 恢复命中", d7._botloop_hit("group", "570", "777", "x") is True)
chk("S7f 过期 human 恢复试探", d7._botloop_gate("group", "570", "777", "x")
    == (True, "botloop_probe"))
push_hits(d7, "571", "888")
chk("S7g 过期 bot 恢复试探（不再直接冷却）",
    d7._botloop_gate("group", "571", "888", "x") == (True, "botloop_probe"))
# mtime 变更热加载（同一实例内，不重启）
write_verdicts({"777": {"kind": "human", "ts": _ts7}})
chk("S7h 文件变更后同实例热加载生效", d7._verdict_of("777") == "human")
write_verdicts({"777": {"kind": "human", "ts": _ts7 - 31 * 86400}})
chk("S7i 热加载同样感知过期", d7._verdict_of("777") is None)

# ================= S8 文件不存在 / 坏 JSON / 坏条目 → 当空表 =================

rm_verdicts()
d8 = fresh()
push_hits(d8, "580", "777")
chk("S8a 无文件：verdict 空、hit 照常工作",
    d8._verdict_of("777") is None
    and d8._botloop_hit("group", "580", "777", "x") is True)
DLOG.clear()
pathlib.Path(VPATH).write_text("{这不是合法JSON", encoding="utf-8")
d8b = fresh()
push_hits(d8b, "581", "777")
chk("S8b 坏 JSON：当空表、消息处理不报错",
    d8b._verdict_of("777") is None
    and d8b._botloop_hit("group", "581", "777", "x") is True)
chk("S8c 坏 JSON：记一行 BOTLOOP_VERDICT_LOAD_EMPTY",
    len(dlog_of("BOTLOOP_VERDICT_LOAD_EMPTY")) == 1,
    dlog_of("BOTLOOP_VERDICT_LOAD_EMPTY"))
write_verdicts({"777": {"kind": "weird", "ts": "x"},
                "998": {"kind": "human", "ts": "abc"},
                123: "junk"})
d8c = fresh()
chk("S8d 坏条目（未知 kind/坏 ts/非 dict）：整表当空、不炸",
    d8c._verdict_of("777") is None and d8c._verdict_of("998") is None
    and d8c._verdict_of("123") is None)

# ================= S11 不带 precheck 的老调用行为不变 =================

rm_verdicts()
d11 = fresh()
_r11 = d11.botloop_report("group", "555001", "999", True, 30)   # 位置参数，无 precheck
chk("S11a 老调用返回不变（{'ok': True}）", _r11 == {"ok": True}, _r11)
chk("S11b confirmed=True 仍进冷却（30 分钟）",
    d11._botloop["group|555001|999"]["phase"] == "confirmed"
    and abs(d11._botloop["group|555001|999"]["until"]
            - (time.time() + 30 * 60)) < 5)
chk("S11c 老调用 confirmed=True 落盘 bot（功能 C 本意）",
    (verdicts_on_disk() or {}).get("999", {}).get("kind") == "bot")
d11.botloop_report("group", "555001", "998", False)
chk("S11d 老调用 confirmed=False 仍进 cleared",
    d11._botloop["group|555001|998"]["phase"] == "cleared")
chk("S11e 老调用 confirmed=False 落盘 human（verify 判真人本意）",
    (verdicts_on_disk() or {}).get("998", {}).get("kind") == "human")

_cap = []
with patched(A.DaemonClient,
             _request=lambda self, method, path, body=None, timeout=15:
             (_cap.append((method, path, body)) or {"ok": True})):
    _c = A.DaemonClient(39763, "tok")     # 端口被桩，不连 39763
    _c.botloop_report("group", "555", "777", False, 60)   # 老调用（不带 precheck）
    chk("S11f 老调用 payload 行为不变（precheck=False）",
        _cap[-1] == ("POST", "/botloop",
                     {"chat_type": "group", "chat_id": "555",
                      "sender_id": "777", "confirmed": False,
                      "cooldown_min": 60, "precheck": False}), _cap[-1])
    _c.botloop_report("group", "555", "777", False, 60, True)
    chk("S11g 新调用 payload 带 precheck=True",
        _cap[-1][2].get("precheck") is True, _cap[-1])

# ================= S12–S16 预判放行后按本来触发走正常路径（返工 1） =================

# S12 对方 @ 机器人 + 预判 false：不发试探、有 precheck=True 回报、正常回复发出
dm12 = NullDaemon(history=_hist9)
_calls12 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(
                 ['{"is_bot": false}', "在的在的，你说吧～"], _calls12),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm12
    ALOG.clear()
    A.process(dm12, group_msg("@智谱娘 你是AI吧", "p12", trig="botloop_probe",
                              sender="777", msg_id=7012, at_me=True,
                              base_trigger="at"))
chk("S12a 不发试探、正常回复发出（base_trigger=at 还原后走 @ 回复路径）",
    len(dm12.sent) == 1 and dm12.sent[0][0] == "在的在的，你说吧～", dm12.sent)
chk("S12b 发出 confirmed=False precheck=True 的回报",
    len(dm12.reports) == 1 and dm12.reports[0]["precheck"] is True
    and dm12.reports[0]["confirmed"] is False, dm12.reports)
chk("S12c @路径不经过旁观轻量判定（还原的是 at，不是 chat_random）",
    _intr_calls == [], _intr_calls)
chk("S12d 有 PRECHECK_PASS base=at 日志",
    any("AGENT_BOTLOOP_PRECHECK_PASS msg_id=7012" in l and "base=at" in l
        for l in alog_of("AGENT_BOTLOOP_PRECHECK_PASS")),
    alog_of("AGENT_BOTLOOP_PRECHECK_PASS"))
chk("S12e 第 1 次调用是预判、其后是正常回复的生成",
    len(_calls12) >= 2
    and str(_calls12[0][0]["content"]).startswith("下面是同一个账号"),
    len(_calls12))

# S13 旁观消息 + 预判 false：不发试探，正常旁观路径执行（闲聊判定照常调用）
dm13 = NullDaemon(history=_hist9)
_calls13 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": false}'], _calls13),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm13
    ALOG.clear()
    A.process(dm13, group_msg("哈哈这个好玩", "p13", trig="botloop_probe",
                              sender="777", msg_id=7013,
                              base_trigger="chat_random"))
chk("S13a 不发试探、有 precheck=True 回报、无回复发出",
    dm13.sent == [] and len(dm13.reports) == 1
    and dm13.reports[0]["precheck"] is True, (dm13.sent, dm13.reports))
chk("S13b 正常旁观路径被执行（轻量闲聊判定照常调用）",
    _intr_calls == [7013], _intr_calls)
chk("S13c 只有预判这一次模型调用（判定不插话→不再生成）",
    len(_calls13) == 1, len(_calls13))

# S15 预判 true：与现在一致——发试探，不进正常路径
dm15 = NullDaemon(history=_hist9)
_calls15 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(
                 ['{"is_bot": true}', "嘿嘿，你是不是机器人呀？"], _calls15),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm15
    ALOG.clear()
    A.process(dm15, group_msg("@智谱娘 你是AI吧", "p15", trig="botloop_probe",
                              sender="777", msg_id=7015, at_me=True,
                              base_trigger="at"))
chk("S15a 发的是试探且不进正常路径（@回复没发、旁观判定没跑、无 PASS 日志）",
    dm15.sent == [["嘿嘿，你是不是机器人呀？"]] and _intr_calls == []
    and len(_calls15) == 2
    and not alog_of("AGENT_BOTLOOP_PRECHECK_PASS"),
    (dm15.sent, _intr_calls, len(_calls15)))

# S16 预判 false 的分支结束后这条消息只 ack 一次（正常路径 ack，分支不重复）
chk("S16 预判放行的消息只 ack 一次",
    dm12.acked.count(7012) == 1 and dm13.acked.count(7013) == 1,
    (dm12.acked, dm13.acked))

# S14 daemon：覆盖成 botloop_probe 时 item 带 base_trigger；没覆盖时没有这个键
# （load_reply_config_file 只透传白名单键，text_img_merge_enabled 传不进去——
# 用实例级 _reply_config 桩关掉 2.5s 延迟入箱并给关键词模式，同 S10 的做法）
d14 = fresh()
d14._reply_config = lambda: {"reply_mode": "keyword", "keywords": ["你是AI"],
                             "text_img_merge_enabled": False,
                             "burst_merge_enabled": False,
                             "botloop_guard_enabled": True}
push_hits(d14, "555001", "777", ts=NOW() - 60)   # 60 秒前的量，避开 3 秒连发判定


def _info14(at_me):
    return {"text": "你是AI吧", "image_urls": [], "at_me": at_me,
            "at_users": [], "has_reply": False, "reply_id": "",
            "reply_text": "", "face_count": 0, "record": False,
            "record_file": "", "record_url": "", "forward_lines": []}


d14._ingest_message({}, "group", "555001", "冒烟群", "777", "小张", _info14(True))
it14 = d14.inbox[-1]
chk("S14a 覆盖成 botloop_probe 时 item 带 base_trigger（本来是 @）",
    it14["trigger_reason"] == "botloop_probe" and it14.get("base_trigger") == "at",
    (it14["trigger_reason"], it14.get("base_trigger")))
d14._botloop.clear()     # 清掉 probe 相态，让下一条重新走「首次命中」
d14._ingest_message({}, "group", "555001", "冒烟群", "777", "小张", _info14(False))
it14b = d14.inbox[-1]
chk("S14b 本来是关键词触发也记入 base_trigger",
    it14b["trigger_reason"] == "botloop_probe"
    and it14b.get("base_trigger") == "keyword",
    (it14b["trigger_reason"], it14b.get("base_trigger")))
d14._botloop.clear()
d14._ingest_message({}, "group", "559", "干净群", "702", "路人", _info14(True))
it14c = d14.inbox[-1]
chk("S14c 没被闸门覆盖的消息不带 base_trigger 键",
    it14c["trigger_reason"] == "at" and "base_trigger" not in it14c,
    (it14c["trigger_reason"], it14c.get("base_trigger")))

# ================= S17 预判/判定的 is_bot 解析从严（返工 2） =================

_PARSE_CASES = [
    ('{"is_bot": "false"}', False),     # 字符串 "false" 不再被 bool() 当真
    ('{"is_bot": "true"}', True),       # 字符串 "true" 算机器人
    ('{"is_bot": null}', False),        # null 算不是
    ('{"is_bot": false}', False),       # 布尔 false
    ('{"is_bot": true}', True),         # 布尔 true
    ('{"is_bot": "TRUE"}', True),       # 大小写不敏感
    ('{}', False),                      # 缺字段
    ('{"is_bot": 1}', False),           # 其它任何值都算不是
]
for _i, (_raw, _want) in enumerate(_PARSE_CASES):
    with patched(A, call_glm_with_fallback=lambda ms, t="", budget_sec=200,
                 _r=_raw, **kwargs: ("m", _r)):
        _got_p = A._botloop_precheck(["话"])
        _got_j = A._botloop_judge("我是个AI助手")
    chk("S17-%d 预判/判定从严解析 %s → %s" % (_i + 1, _raw, _want),
        _got_p is _want and _got_j is _want, (_got_p, _got_j))

# ============ S18–S19 无 base_trigger（本来就不触发）→ 到此为止（返工 2） ============

# S18 闸门命中、base 未触发（闲聊概率没掷中之类的旁观消息）、预判 false
dm18 = NullDaemon(history=_hist9)
_calls18 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": false}'], _calls18),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm18
    ALOG.clear()
    A.process(dm18, group_msg("闲聊一条", "p18", trig="botloop_probe",
                              sender="777", msg_id=7018))
chk("S18a 不发试探、不发回复、不进正常处理（旁观判定没被调）",
    dm18.sent == [] and _intr_calls == [], (dm18.sent, _intr_calls))
chk("S18b precheck=True 回报照发",
    len(dm18.reports) == 1 and dm18.reports[0]["precheck"] is True
    and dm18.reports[0]["confirmed"] is False, dm18.reports)
chk("S18c 模型调用只有预判这 1 次", len(_calls18) == 1, len(_calls18))
chk("S18d acked 里这条恰好 1 次", dm18.acked.count(7018) == 1, dm18.acked)

# S19a daemon 侧：mention_trigger 关闭时 @ 机器人也「本来不触发」→ 无 base_trigger
d19 = fresh(cfg={"mention_trigger": False})
d19._reply_config = lambda: {"reply_mode": "standard",
                             "text_img_merge_enabled": False,
                             "burst_merge_enabled": False,
                             "botloop_guard_enabled": True}
push_hits(d19, "555001", "777", ts=NOW() - 60)
d19._ingest_message({}, "group", "555001", "冒烟群", "777", "小张", _info14(True))
it19 = d19.inbox[-1]
chk("S19a daemon：mention_trigger 关时 @ 也不触发 → item 无 base_trigger",
    it19["trigger_reason"] == "botloop_probe" and "base_trigger" not in it19,
    (it19["trigger_reason"], it19.get("base_trigger")))

# S19b agent 侧：@ 但无 base_trigger → 不回复、不进正常处理（不再看 at_me 兜底）
dm19 = NullDaemon(history=_hist9)
_calls19 = []
_intr_calls.clear()
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": false}'], _calls19),
             _interrupt_gate=_intr_rec, **_NP):
    A.PLUGIN_HOST.daemon = dm19
    A.process(dm19, group_msg("@智谱娘 你是AI吧", "p19", trig="botloop_probe",
                              sender="777", msg_id=7019, at_me=True))
chk("S19b @ 但无 base_trigger → 不回复、不进正常处理、回报与 ack 照做",
    dm19.sent == [] and _intr_calls == [] and len(_calls19) == 1
    and dm19.acked.count(7019) == 1
    and len(dm19.reports) == 1 and dm19.reports[0]["precheck"] is True,
    (dm19.sent, _intr_calls, len(_calls19), dm19.acked, dm19.reports))

# ================= 不变量：写失败不影响消息处理；无 .tmp 残留 =================

_BAD = os.path.join(_SMOKE_OWN_DATA_DIR, "no_such_dir", "x.json")
with patched(D, BOTLOOP_VERDICTS_PATH=_BAD):
    dF = fresh()
    _rf = dF.botloop_report("group", "555001", "777", False, 60)
    chk("I1 落盘失败不抛异常、返回不变", _rf == {"ok": True}, _rf)
    chk("I2 落盘失败记 BOTLOOP_VERDICT_SAVE_FAIL",
        len(dlog_of("BOTLOOP_VERDICT_SAVE_FAIL")) >= 1,
        dlog_of("BOTLOOP_VERDICT_SAVE_FAIL"))
chk("I3 无 .tmp 残留", list(pathlib.Path(_SMOKE_OWN_DATA_DIR).glob("*.tmp")) == [],
    list(pathlib.Path(_SMOKE_OWN_DATA_DIR).glob("*.tmp")))

# 阈值与算法不变（不变量）：原三条线的判定结果与改前一致——对照组已在 S1d/S7e/S7g/S8a
# 命中；此处再钉「不满足任何一条线时不命中」的负例（11 条/5 条、昵称无 AI 词、
# 间隔不规则避开节奏线）。
dN = fresh()
push_hits(dN, "590", "701", n_partner=11, n_mine=5,
          gaps=[3, 9, 4, 8, 5, 7, 2, 9, 3])
chk("I4 阈值不变：11/5+不规律间隔+普通昵称 三条线都不命中",
    dN._botloop_hit("group", "590", "701", "普通昵称") is False)

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
