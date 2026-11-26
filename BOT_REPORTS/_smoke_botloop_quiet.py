# -*- coding: utf-8 -*-
"""离线冒烟：AI 互聊「悄悄降温」开关（默认关，2026-11-26）。

Q1 daemon：botloop_report(quiet=True) 进 quiet 相、闸门时限内完全静默、
   不落任何判定文件、日志带 quiet_min；到期后闸门删状态、恢复正常走后半段。
Q2 daemon：quiet_min 传 "abc"/None/0/999 → 分别按 15/15/1/120 处理。
Q3 daemon：quiet=False 三条老路径不变（confirmed 落盘 bot；否认落盘 human；
   precheck 只清 1 小时不落盘）。
Q4 agent：开关关 + 预判像机器人 → 试探照发、payload 不带 quiet 字段。
Q5 agent：开关开 + 预判像机器人 → 群里零发送、quiet=True/quiet_min=15 回报、
   主人通知 1 次（带群名与名字）、1 小时限频、拨时间后又发。
Q6 agent：开关开 + 预判像真人 → 现有行为（precheck=True 回报 + base 还原）。
Q7 agent：主人私聊「真人/机器人 名字」→ 参数正确、固定文案、模型零调用、
   条目摘除；查不到的名字不拦截（落回正常处理）。
Q8 agent：非主人发同样的私聊 → 不拦截。
Q9 预判期间对方又发一条：闸门静默、没有 verify、quiet 相不被冲掉、不落盘。
Q10 未 asked 时对方消息被静默；asked 后第一条进 verify；判定期间第二条静默；
   300 秒没发问 → cleared + botloop_probe_nosend。
Q11 没发出试探（空回复/[SKIP]）→ cleared、不落盘、未 asked。
Q12 verify 判不出 AI → cleared 带 human_pending、不落盘；观察期满落盘 human；
   期内撞满两次升级线 → confirmed、不落 human。
Q13 asked=True 在非 probe 相是空操作。
Q7h-k 主人先「机器人 X」再「真人 X」两次都拦截且回报参数正确、条目保留。
Q7l-m 回报抛异常 → 回「没改成」、条目保留、日志带 ok=False。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_botloop_quiet.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_botloop_quiet_iso_")
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
    """临时替换对象属性，退出时恢复（照 _smoke_botloop.py 的做法）。"""
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
    """新 Daemon 实例（不连 ws、不监听端口）。"""
    return D.Daemon(ws_url="", token="", cfg=cfg or {}, ctl_port=0)


def NOW():
    return int(time.time())


def push_hits(d, chat_id, sid, n_partner=13, n_mine=6, ts=None):
    """灌入硬量线命中的历史（照 _smoke_botloop.push_hits）。"""
    ts = ts or NOW()
    key = "group_%s" % chat_id
    t = ts
    for i in range(n_partner):
        d.mem_history[key].append({
            "ts": t, "message_id": "m%d" % i, "sender_id": str(sid),
            "sender_name": "昵称", "is_bot": False, "text": "话%d" % i,
            "n_images": 0, "image_urls": []})
        t -= 1
    for i in range(n_mine):
        d.mem_history[key].append({
            "ts": ts - 5 - i * 3, "message_id": "b%d" % i, "sender_id": "bot",
            "sender_name": "BOT", "is_bot": True, "text": "回%d" % i,
            "n_images": 0, "image_urls": []})


VPATH = D.BOTLOOP_VERDICTS_PATH
RCPATH = A.REPLY_CONFIG_PATH
_VPAD = [0]
_RCPAD = [0]


def write_verdicts(obj):
    _VPAD[0] += 1    # 追加不同数量换行：保证 mtime+size 签名必变（热加载才生效）
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


def write_reply_cfg(obj):
    _RCPAD[0] += 1
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


def group_msg(text, mid, trig="chat_random", sender="777", msg_id=7001, **kw):
    m = {"id": msg_id, "chat_type": "group", "chat_id": "555001",
         "chat_name": "冒烟群", "sender_id": sender, "sender_name": "小张",
         "text": text, "trigger_reason": trig, "message_id": mid,
         "image_urls": [], "at_me": False, "at_users": [], "has_reply": False,
         "reply_to": None, "reply_text": "", "face_count": 0}
    m.update(kw)
    return m


def priv_msg(text, msg_id=8001, **kw):
    m = {"id": msg_id, "chat_type": "private", "chat_id": "10086",
         "chat_name": "", "sender_id": "10086", "sender_name": "主人",
         "text": text, "trigger_reason": "private", "message_id": "p%d"
         % msg_id, "image_urls": [], "at_me": False, "at_users": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0}
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


# 正常处理路径的最小补丁集（照 _smoke_botloop.py 的 _NP）
_NP = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
           REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
           vision_enabled=lambda: False,
           chat_anti_interrupt_enabled=lambda: True,
           lean_ctx_on=lambda: True, reply_v2_on=lambda: False,
           _cap_chat_allowed=lambda ct, cid: False,
           owner_qq=lambda: "10086",
           owner_command=lambda m, d: (False, ""))

_hist = []
_ts0 = NOW()
for i in range(1, 15):
    _hist.append({"ts": _ts0 - 50 + i, "message_id": "s%d" % i,
                  "sender_id": "777", "sender_name": "小张", "is_bot": False,
                  "text": "话%02d" % i, "n_images": 0, "image_urls": []})

# 落盘路径必须只在数据目录内（不变量）
chk("I0 判定落盘路径在数据目录内", os.path.dirname(VPATH) == D.DATA_ROOT,
    "%s vs %s" % (os.path.dirname(VPATH), D.DATA_ROOT))
rm_verdicts()
rm_reply_cfg()

# ================= Q1 daemon：quiet 相的基本行为 =================

d1 = fresh()
_r1 = d1.botloop_report("group", "555001", "777", False, 60, quiet=True)
chk("Q1a quiet 回报返回不变（{'ok': True}）", _r1 == {"ok": True}, _r1)
_st1 = d1._botloop.get("group|555001|777")
chk("Q1b 状态进 quiet 相、时长为默认 15 分钟",
    _st1 and _st1.get("phase") == "quiet"
    and abs(_st1.get("until", 0) - (time.time() + 15 * 60)) < 5, _st1)
chk("Q1c 时限内闸门完全静默（与 confirmed 同形）",
    d1._botloop_gate("group", "555001", "777", "x") == (False, "botloop"))
chk("Q1d 没写任何判定文件", verdicts_on_disk() is None
    and not os.path.exists(VPATH), verdicts_on_disk())
_qlog = [l for l in dlog_of("botloop_quiet") if "777" in l]
chk("Q1e 日志 AGENT_LOG botloop_quiet 带 chat/sender/quiet_min=15",
    len(_qlog) == 1 and '"quiet_min": 15' in _qlog[0]
    and '"sender": "777"' in _qlog[0], _qlog)
_st1["until"] = time.time() - 1    # 拨到到期之后
_g1 = d1._botloop_gate("group", "555001", "777", "x")
chk("Q1f 到期后不再静默且状态已删除（不转 cleared）",
    _g1 is None and "group|555001|777" not in d1._botloop, (_g1, d1._botloop))

# ================= Q2 daemon：quiet_min 的钳制 =================

for _i, (_val, _want) in enumerate((("abc", 15), (None, 15), (0, 1),
                                    (999, 120))):
    dq = fresh()
    dq.botloop_report("group", "56%d" % _i, "70%d" % _i, False, 60,
                      quiet=True, quiet_min=_val)
    _stq = dq._botloop.get("group|56%d|70%d" % (_i, _i))
    chk("Q2-%d quiet_min=%r → %d 分钟" % (_i + 1, _val, _want),
        _stq and _stq.get("phase") == "quiet"
        and abs(_stq.get("until", 0) - (time.time() + _want * 60)) < 5, _stq)

# ================= Q3 daemon：quiet=False 三条老路径不变 =================

rm_verdicts()
d3 = fresh()
_r3 = d3.botloop_report("group", "555001", "999", True, 30)   # 老式位置调用
chk("Q3a 老调用返回不变", _r3 == {"ok": True}, _r3)
chk("Q3b confirmed=True 进 confirmed 冷却（30 分钟）",
    d3._botloop["group|555001|999"]["phase"] == "confirmed"
    and abs(d3._botloop["group|555001|999"]["until"]
            - (time.time() + 30 * 60)) < 5)
chk("Q3c confirmed=True 落盘 bot",
    (verdicts_on_disk() or {}).get("999", {}).get("kind") == "bot",
    verdicts_on_disk())
d3.botloop_report("group", "555001", "998", False)
chk("Q3d confirmed=False 进 cleared 并落盘 human",
    d3._botloop["group|555001|998"]["phase"] == "cleared"
    and (verdicts_on_disk() or {}).get("998", {}).get("kind") == "human")
d3.botloop_report("group", "555001", "997", False, 60, precheck=True)
chk("Q3e precheck=True 只进 cleared、不落盘",
    d3._botloop["group|555001|997"]["phase"] == "cleared"
    and "997" not in (verdicts_on_disk() or {}), verdicts_on_disk())

# ===== Q10（②）未 asked 静默；asked 后第一条进 verify；判定期间静默 =====

rm_verdicts()
d10 = fresh()
push_hits(d10, "555001", "777")
chk("Q10a 命中进 probe", d10._botloop_gate("group", "555001", "777", "x")
    == (True, "botloop_probe"))
chk("Q10b 未 asked：对方再发被静默（不是 verify）",
    d10._botloop_gate("group", "555001", "777", "x") == (False, "botloop"))
_r10 = d10.botloop_report("group", "555001", "777", False, 60, asked=True)
_st10 = d10._botloop["group|555001|777"]
chk("Q10c asked=True 只补 asked 时间戳（不改相、无 verifying、不落盘）",
    _r10 == {"ok": True} and _st10.get("phase") == "probe"
    and isinstance(_st10.get("asked"), float) and "verifying" not in _st10
    and verdicts_on_disk() is None, (_r10, _st10, verdicts_on_disk()))
chk("Q10d asked 后第一条进 verify（置 verifying）",
    d10._botloop_gate("group", "555001", "777", "x") == (True, "botloop_verify")
    and d10._botloop["group|555001|777"].get("verifying") is True)
chk("Q10e 判定期间第二条静默",
    d10._botloop_gate("group", "555001", "777", "x") == (False, "botloop"))
# 未 asked 超 300 秒＝agent 一直没发问 → cleared + botloop_probe_nosend 日志
d10b = fresh()
push_hits(d10b, "556", "801")
d10b._botloop_gate("group", "556", "801", "x")
d10b._botloop["group|556|801"]["ts"] = time.time() - 301
DLOG.clear()
chk("Q10f 300 秒没发问：转 cleared、记 botloop_probe_nosend、放行",
    d10b._botloop_gate("group", "556", "801", "x") is None
    and d10b._botloop["group|556|801"]["phase"] == "cleared"
    and len(dlog_of("botloop_probe_nosend")) == 1,
    dlog_of("botloop_probe_nosend"))

# ===== Q12（④）probation：verify 判「像真人」缓期落盘 =====

rm_verdicts()
d12 = fresh()
push_hits(d12, "555001", "777")
d12._botloop_gate("group", "555001", "777", "x")     # probe
d12.botloop_report("group", "555001", "777", False, 60, asked=True)
d12._botloop_gate("group", "555001", "777", "x")     # verify（置 verifying）
d12.botloop_report("group", "555001", "777", False, 60, probation=True)
_st12 = d12._botloop["group|555001|777"]
chk("Q12a verify 判不出 AI：cleared 带 human_pending、不落盘",
    _st12.get("phase") == "cleared" and _st12.get("human_pending") is True
    and verdicts_on_disk() is None, (_st12, verdicts_on_disk()))
chk("Q12b 观察期内闸门放行",
    d12._botloop_gate("group", "555001", "777", "x") is None)
d12.mem_history.clear()     # 拨到期时不再重新命中，避免闸门尾部再进 probe
_st12["ts"] = time.time() - 3601
DLOG.clear()
chk("Q12c 观察期满：先落盘 human（带 botloop_verdict_human 日志）再删",
    d12._botloop_gate("group", "555001", "777", "x") is None
    and (verdicts_on_disk() or {}).get("777", {}).get("kind") == "human"
    and "group|555001|777" not in d12._botloop
    and len(dlog_of("botloop_verdict_human")) == 1,
    (verdicts_on_disk(), dlog_of("botloop_verdict_human")))
# 观察期内撞满两次高门槛线 → 按不配合转 confirmed、不落 human
rm_verdicts()
d12b = fresh()
push_hits(d12b, "555001", "777")
d12b._botloop_gate("group", "555001", "777", "x")
d12b.botloop_report("group", "555001", "777", False, 60, asked=True)
d12b._botloop_gate("group", "555001", "777", "x")
d12b.botloop_report("group", "555001", "777", False, 60, probation=True)
push_hits(d12b, "555001", "777")    # 对方还在高频说话
chk("Q12d 观察期第一次撞线：先放行（记 hits=1）",
    d12b._botloop_gate("group", "555001", "777", "x") is None
    and d12b._botloop["group|555001|777"].get("hits") == 1)
d12b._botloop["group|555001|777"]["last_hit"] = time.time() - 121
chk("Q12e 观察期内撞满两次：confirmed 静默、不落 human",
    d12b._botloop_gate("group", "555001", "777", "x") == (False, "botloop")
    and d12b._botloop["group|555001|777"]["phase"] == "confirmed"
    and "777" not in (verdicts_on_disk() or {}),
    (d12b._botloop["group|555001|777"], verdicts_on_disk()))

# ===== Q13（⑤）asked=True 在非 probe 相是空操作 =====

d13 = fresh()
_stA = {"phase": "cleared", "ts": time.time()}
d13._botloop["group|581|901"] = dict(_stA)
_r13 = d13.botloop_report("group", "581", "901", False, 60, asked=True)
chk("Q13a cleared 相上 asked=True：条目原样、返回不变",
    _r13 == {"ok": True} and d13._botloop["group|581|901"] == _stA,
    (d13._botloop.get("group|581|901"), _r13))
_stB = {"phase": "confirmed", "until": time.time() + 600}
d13._botloop["group|581|902"] = dict(_stB)
d13.botloop_report("group", "581", "902", False, 60, asked=True)
chk("Q13b confirmed 相上 asked=True：条目原样",
    d13._botloop["group|581|902"] == _stB)
d13.botloop_report("group", "581", "903", False, 60, asked=True)
chk("Q13c 无条目时 asked=True：不建条目",
    "group|581|903" not in d13._botloop, d13._botloop.get("group|581|903"))

_V3 = verdicts_on_disk()    # 快照：此后（agent 侧降温/改判）不得再动判定落盘

# ================= DaemonClient payload：关时不带 quiet 字段 =================

_cap = []
with patched(A.DaemonClient,
             _request=lambda self, method, path, body=None, timeout=15:
             (_cap.append((method, path, body)) or {"ok": True})):
    _c = A.DaemonClient(39763, "tok")
    _c.botloop_report("group", "555", "777", False, 60)
    chk("P1 开关关：请求体与从前逐字一致（无 quiet 字段）",
        _cap[-1] == ("POST", "/botloop",
                     {"chat_type": "group", "chat_id": "555",
                      "sender_id": "777", "confirmed": False,
                      "cooldown_min": 60, "precheck": False}), _cap[-1])
    _c.botloop_report("group", "555", "777", False, 60, quiet=True,
                      quiet_min=9)
    chk("P2 开关开：请求体只多 quiet/quiet_min 两个字段",
        _cap[-1][2] == {"chat_type": "group", "chat_id": "555",
                        "sender_id": "777", "confirmed": False,
                        "cooldown_min": 60, "precheck": False,
                        "quiet": True, "quiet_min": 9}, _cap[-1])
    _c.botloop_report("group", "555", "777", False, 60, asked=True)
    chk("P3 asked=True 只多带 asked 字段",
        _cap[-1][2] == {"chat_type": "group", "chat_id": "555",
                        "sender_id": "777", "confirmed": False,
                        "cooldown_min": 60, "precheck": False,
                        "asked": True}, _cap[-1])
    _c.botloop_report("group", "555", "777", True, 45, probation=True)
    chk("P4 probation=True 只多带 probation 字段",
        _cap[-1][2] == {"chat_type": "group", "chat_id": "555",
                        "sender_id": "777", "confirmed": True,
                        "cooldown_min": 45, "precheck": False,
                        "probation": True}, _cap[-1])

# ================= Q4 agent：开关关 + 预判像机器人 → 原试探链路 =================

rm_reply_cfg()
dm4 = NullDaemon(history=_hist)
_calls4 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(
                 ['{"is_bot": true}', "嘿嘿，你是不是机器人呀？"], _calls4),
             **_NP):
    A.PLUGIN_HOST.daemon = dm4
    ALOG.clear()
    A.process(dm4, group_msg("你是AI吧", "q4", trig="botloop_probe",
                             sender="777", msg_id=7104))
chk("Q4a 两次模型调用（预判 + 生成试探）", len(_calls4) == 2, len(_calls4))
chk("Q4b 试探文本发进群", dm4.sent == [("group", "555001",
                                        ["嘿嘿，你是不是机器人呀？"])],
    dm4.sent)
chk("Q4c 试探发出后回报 asked=True（confirmed=False，无 quiet/probation）",
    dm4.reports == [{"chat_type": "group", "chat_id": "555001",
                     "sender_id": "777", "confirmed": False,
                     "cooldown_min": 60, "precheck": False,
                     "asked": True}], dm4.reports)
chk("Q4d 无 AGENT_BOTLOOP_QUIET 日志、消息被 ack",
    not alog_of("AGENT_BOTLOOP_QUIET") and dm4.acked == [7104],
    (alog_of("AGENT_BOTLOOP_QUIET"), dm4.acked))

# ===== Q11（③）没发出试探（空回复 / [SKIP]）→ cleared、不落盘、未 asked =====

for _i, (_resp, _tag) in enumerate((("", "空回复"), ("[SKIP]", "SKIP"))):
    dmN = NullDaemon(history=_hist)
    _callsN = []
    with patched(A, build_messages=lambda d, m, ctx_k=32:
                 ([{"role": "user", "content": "试探"}], "p"),
                 call_glm_with_fallback=glm_script(
                     ['{"is_bot": true}', _resp], _callsN), **_NP):
        A.PLUGIN_HOST.daemon = dmN
        ALOG.clear()
        A.process(dmN, group_msg("你是AI吧", "q11", trig="botloop_probe",
                                 sender="777", msg_id=7110 + _i))
    chk("Q11a-%d %s：群里没发、有 PROBE_NOSND 日志、PROBE 日志无 probe= 后缀"
        % (_i + 1, _tag),
        dmN.sent == [] and len(alog_of("AGENT_BOTLOOP_PROBE_NOSND")) == 1
        and all("probe=" not in l for l in alog_of("AGENT_BOTLOOP_PROBE")),
        (dmN.sent, alog_of("AGENT_BOTLOOP_PROBE_NOSND")))
    chk("Q11b-%d %s：回报 confirmed=False precheck=True（无 asked/probation）"
        % (_i + 1, _tag),
        dmN.reports == [{"chat_type": "group", "chat_id": "555001",
                         "sender_id": "777", "confirmed": False,
                         "cooldown_min": 60, "precheck": True}], dmN.reports)
    chk("Q11c-%d %s：消息被 ack" % (_i + 1, _tag),
        dmN.acked == [7110 + _i], dmN.acked)

# 把同一形状的回报（precheck=True）回放到真 daemon：cleared、未 asked、不落盘
rm_verdicts()
d11 = fresh()
push_hits(d11, "555001", "777")
chk("Q11d 命中进 probe", d11._botloop_gate("group", "555001", "777", "x")
    == (True, "botloop_probe"))
_r11 = dmN.reports[0]
d11.botloop_report(_r11["chat_type"], _r11["chat_id"], _r11["sender_id"],
                   _r11["confirmed"], _r11["cooldown_min"], precheck=True)
_st11 = d11._botloop.get("group|555001|777")
chk("Q11e NOSND 回报后：cleared、未 asked、不落盘、闸门放行",
    _st11 and _st11.get("phase") == "cleared" and "asked" not in _st11
    and not os.path.exists(VPATH)
    and d11._botloop_gate("group", "555001", "777", "x") is None,
    (_st11, os.path.exists(VPATH)))
rm_verdicts()

# ================= Q5 agent：开关开 + 预判像机器人 → 悄悄降温 =================

write_reply_cfg({"botloop_quiet_mode": True, "botloop_cooldown_min": 77})
A._BOTLOOP_QUIET_PENDING.clear()
A._BOTLOOP_QUIET_NOTIFY_TS.clear()
dm5 = NullDaemon(history=_hist, group_names={"555001": "冒烟群"})
_calls5 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": true}'], _calls5),
             **_NP):
    A.PLUGIN_HOST.daemon = dm5
    ALOG.clear()
    A.process(dm5, group_msg("你是AI吧", "q5", trig="botloop_probe",
                             sender="777", msg_id=7105))
chk("Q5a 群里一条都没发（试探没构造、没发；唯一发送是私聊主人）",
    not any(ct == "group" for ct, _cid, _t in dm5.sent), dm5.sent)
chk("Q5b 只发生预判这一次模型调用", len(_calls5) == 1, len(_calls5))
chk("Q5c botloop_report 以 quiet=True、quiet_min=15、confirmed=False 调用",
    len(dm5.reports) == 1 and dm5.reports[0]["quiet"] is True
    and dm5.reports[0]["quiet_min"] == 15
    and dm5.reports[0]["confirmed"] is False
    and dm5.reports[0]["precheck"] is False
    and dm5.reports[0]["cooldown_min"] == 77, dm5.reports)
_notice5 = ("「冒烟群」里「小张」的说话节奏像机器人，我先不接 TA 的话了，"
            "约 15 分钟后自动恢复。")
_notice5b = "认错了就回我「真人 小张」，确认是机器人就回我「机器人 小张」。"
chk("Q5d 主人通知发出且为两条消息（私聊 10086，带群名与名字）",
    dm5.sent == [("private", "10086", [_notice5, _notice5b])], dm5.sent)
_ql5 = alog_of("AGENT_BOTLOOP_QUIET")
chk("Q5e 日志 AGENT_BOTLOOP_QUIET 完整（quiet_min=15 notice=yes）",
    len(_ql5) == 1 and _ql5[0].startswith(
        "AGENT_BOTLOOP_QUIET msg_id=7105 chat=group/555001 sender=777 ")
    and "quiet_min=15 notice=yes" in _ql5[0], _ql5)
chk("Q5f 消息被 ack", dm5.acked == [7105], dm5.acked)
_ts5 = A._BOTLOOP_QUIET_NOTIFY_TS.get("777")
_ent5 = A._botloop_quiet_find("小张")
chk("Q5g 待改判表已登记（按名字可查，含 chat/sender/时间戳）",
    _ent5 and _ent5.get("chat_id") == "555001"
    and _ent5.get("sender_id") == "777" and _ent5.get("name") == "小张"
    and isinstance(_ent5.get("ts"), float), _ent5)
# 一小时内再来一次 → 限频，不再发
dm5b = NullDaemon(history=_hist, group_names={"555001": "冒烟群"})
_calls5b = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": true}'], _calls5b),
             **_NP):
    A.PLUGIN_HOST.daemon = dm5b
    ALOG.clear()
    A.process(dm5b, group_msg("你是AI吧", "q5b", trig="botloop_probe",
                              sender="777", msg_id=7106))
chk("Q5h 一小时内第二条：不再私聊主人（notice=no-rate），降温照做",
    dm5b.sent == [] and len(dm5b.reports) == 1
    and "notice=no-rate" in (alog_of("AGENT_BOTLOOP_QUIET") or [""])[0],
    (dm5b.sent, alog_of("AGENT_BOTLOOP_QUIET")))
chk("Q5i 限频跳过时不更新时间戳（仍指向上一条发出时刻）",
    A._BOTLOOP_QUIET_NOTIFY_TS.get("777") == _ts5, A._BOTLOOP_QUIET_NOTIFY_TS)
# 拨到 2 小时前 → 又发
A._BOTLOOP_QUIET_NOTIFY_TS["777"] = time.time() - 7200
dm5c = NullDaemon(history=_hist, group_names={"555001": "冒烟群"})
_calls5c = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": true}'], _calls5c),
             **_NP):
    A.PLUGIN_HOST.daemon = dm5c
    ALOG.clear()
    A.process(dm5c, group_msg("你是AI吧", "q5c", trig="botloop_probe",
                              sender="777", msg_id=7107))
chk("Q5j 2 小时后再进降温 → 通知又发（notice=yes）",
    len(dm5c.sent) == 1 and dm5c.sent[0][0] == "private"
    and "notice=yes" in (alog_of("AGENT_BOTLOOP_QUIET") or [""])[0],
    (dm5c.sent, alog_of("AGENT_BOTLOOP_QUIET")))

# ===== Q9（①）预判期间对方又发一条：闸门静默、没有 verify、quiet 相不被冲掉 =====

dQ9 = fresh()
push_hits(dQ9, "555001", "888")
chk("Q9a 命中进 probe",
    dQ9._botloop_gate("group", "555001", "888", "x") == (True, "botloop_probe"))
chk("Q9b 预判期间对方再发 → (False,'botloop')，不是 botloop_verify",
    dQ9._botloop_gate("group", "555001", "888", "x") == (False, "botloop"))
dmQ9 = NullDaemon(history=_hist)
dmQ9.botloop_report = lambda *a, **k: dQ9.botloop_report(*a, **k)  # 转发真 daemon
_callsQ9 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(['{"is_bot": true}'], _callsQ9),
             **_NP):
    A.PLUGIN_HOST.daemon = dmQ9
    ALOG.clear()
    A.process(dmQ9, group_msg("你是AI吧", "q9", trig="botloop_probe",
                              sender="888", sender_name="小AI", msg_id=7140))
chk("Q9c 降温回报后是 quiet 相（probe 被整个换掉、无 asked/verifying 残留）",
    dQ9._botloop["group|555001|888"].get("phase") == "quiet"
    and "asked" not in dQ9._botloop["group|555001|888"]
    and "verifying" not in dQ9._botloop["group|555001|888"],
    dQ9._botloop["group|555001|888"])
chk("Q9d 之后闸门仍静默且 quiet 相没被冲掉",
    dQ9._botloop_gate("group", "555001", "888", "x") == (False, "botloop")
    and dQ9._botloop["group|555001|888"]["phase"] == "quiet")
chk("Q9e 整串走完判定文件不存在（quiet 不落盘）", not os.path.exists(VPATH))

# ================= Q6 agent：开关开 + 预判像真人 → 现有行为 =================

dm6 = NullDaemon(history=_hist)
_calls6 = []
with patched(A, build_messages=lambda d, m, ctx_k=32:
             ([{"role": "user", "content": "试探"}], "p"),
             call_glm_with_fallback=glm_script(
                 ['{"is_bot": false}', "在的在的，你说吧～"], _calls6),
             **_NP):
    A.PLUGIN_HOST.daemon = dm6
    ALOG.clear()
    A.process(dm6, group_msg("@智谱娘 你是AI吧", "q6", trig="botloop_probe",
                             sender="777", msg_id=7108, at_me=True,
                             base_trigger="at"))
chk("Q6a 预判像真人：回报 precheck=True、不带 quiet/asked/probation",
    len(dm6.reports) == 1 and dm6.reports[0]["precheck"] is True
    and "quiet" not in dm6.reports[0] and "asked" not in dm6.reports[0]
    and "probation" not in dm6.reports[0], dm6.reports)
chk("Q6b base_trigger 还原后走正常处理（@回复发出）",
    dm6.sent == [("group", "555001", ["在的在的，你说吧～"])], dm6.sent)
chk("Q6c 无 AGENT_BOTLOOP_QUIET 日志", not alog_of("AGENT_BOTLOOP_QUIET"))

# ================= Q7 agent：主人私聊改判 =================

A._BOTLOOP_QUIET_PENDING.clear()
A._BOTLOOP_QUIET_NOTIFY_TS.clear()
A._botloop_quiet_register(group_msg("x", "reg", sender="777"), 15)

dm7a = NullDaemon()
_calls7a = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls7a), **_NP):
    A.PLUGIN_HOST.daemon = dm7a
    ALOG.clear()
    A.process(dm7a, priv_msg("真人 小张", msg_id=7201))
chk("Q7a 改判真人：report 参数取自待改判条目（confirmed=False precheck=False，"
    "无 probation＝立即落盘 human 的唯一入口）",
    dm7a.reports == [{"chat_type": "group", "chat_id": "555001",
                      "sender_id": "777", "confirmed": False,
                      "cooldown_min": 77, "precheck": False}], dm7a.reports)
chk("Q7b 回了固定文案（不调模型）",
    dm7a.sent == [("private", "10086", ["好，把「小张」当真人，这就恢复搭话。"])]
    and _calls7a == [], (dm7a.sent, len(_calls7a)))
chk("Q7c 日志 AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=human",
    any(l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=human"
        for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT")),
    alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))
chk("Q7d 改判后条目保留在待改判表（先「机器人」后还能改回「真人」）",
    A._botloop_quiet_find("小张") is not None
    and A._botloop_quiet_find("777") is not None)
chk("Q7e 消息被 ack", dm7a.acked == [7201], dm7a.acked)

# 「机器人 名字」→ confirmed=True
A._botloop_quiet_register(group_msg("x", "reg2", sender="777"), 15)
dm7b = NullDaemon()
_calls7b = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls7b), **_NP):
    A.PLUGIN_HOST.daemon = dm7b
    ALOG.clear()
    A.process(dm7b, priv_msg("机器人：小张", msg_id=7202))
chk("Q7f 改判机器人：confirmed=True、固定文案",
    dm7b.reports and dm7b.reports[0]["confirmed"] is True
    and dm7b.reports[0]["precheck"] is False
    and dm7b.sent == [("private", "10086",
                       ["好，「小张」按机器人处理，这段时间不再搭话。"])],
    (dm7b.reports, dm7b.sent))
chk("Q7g kind=bot 日志、模型零调用",
    any(l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=bot"
        for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))
    and _calls7b == [], (alog_of("AGENT_BOTLOOP_OWNER_VERDICT"), _calls7b))

# ===== Q7h-k（⑥）主人先「机器人 X」再「真人 X」：条目保留，两次都拦截 =====

A._BOTLOOP_QUIET_PENDING.clear()
A._BOTLOOP_QUIET_NOTIFY_TS.clear()
A._botloop_quiet_register(group_msg("x", "reg6", sender="777"), 15)
dm7m = NullDaemon()
_calls7m = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls7m), **_NP):
    A.PLUGIN_HOST.daemon = dm7m
    ALOG.clear()
    A.process(dm7m, priv_msg("机器人 小张", msg_id=7210))
chk("Q7h 先「机器人」：confirmed=True 回报、bot 固定文案、零模型调用",
    dm7m.reports == [{"chat_type": "group", "chat_id": "555001",
                      "sender_id": "777", "confirmed": True,
                      "cooldown_min": 77, "precheck": False}]
    and dm7m.sent == [("private", "10086",
                       ["好，「小张」按机器人处理，这段时间不再搭话。"])]
    and _calls7m == [], (dm7m.reports, dm7m.sent, _calls7m))
chk("Q7i 条目还在（未摘除）", A._botloop_quiet_find("小张") is not None)
dm7n = NullDaemon()
_calls7n = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls7n), **_NP):
    A.PLUGIN_HOST.daemon = dm7n
    ALOG.clear()
    A.process(dm7n, priv_msg("真人 小张", msg_id=7211))
chk("Q7j 再「真人」：confirmed=False 回报、human 固定文案",
    dm7n.reports == [{"chat_type": "group", "chat_id": "555001",
                      "sender_id": "777", "confirmed": False,
                      "cooldown_min": 77, "precheck": False}]
    and dm7n.sent == [("private", "10086",
                       ["好，把「小张」当真人，这就恢复搭话。"])],
    (dm7n.reports, dm7n.sent))
chk("Q7k 两次改判后条目仍在（24 小时过期才清）",
    A._botloop_quiet_find("小张") is not None)

# ===== Q7l-m（⑦）回报抛异常：回「没改成」、条目保留、日志带 ok=False =====

A._BOTLOOP_QUIET_PENDING.clear()
A._botloop_quiet_register(group_msg("x", "reg7", sender="777"), 15)
dm7p = NullDaemon()


def _boom(*a, **k):
    raise RuntimeError("daemon down")


dm7p.botloop_report = _boom
_calls7p = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _calls7p), **_NP):
    A.PLUGIN_HOST.daemon = dm7p
    ALOG.clear()
    A.process(dm7p, priv_msg("真人 小张", msg_id=7212))
chk("Q7l 回报抛异常：回「没改成，稍后再回我一次。」、零模型调用",
    dm7p.sent == [("private", "10086", ["没改成，稍后再回我一次。"])]
    and _calls7p == [], (dm7p.sent, _calls7p))
chk("Q7m 条目保留、OWNER_VERDICT 日志带 ok=False、有 REPORT_FAIL",
    A._botloop_quiet_find("小张") is not None
    and any(l == "AGENT_BOTLOOP_OWNER_VERDICT sender=777 kind=human ok=False"
            for l in alog_of("AGENT_BOTLOOP_OWNER_VERDICT"))
    and len(alog_of("AGENT_BOTLOOP_REPORT_FAIL")) >= 1,
    (alog_of("AGENT_BOTLOOP_OWNER_VERDICT"), alog_of("AGENT_BOTLOOP_REPORT_FAIL")))

# 查不到的名字 → 不拦截，原样落回正常处理
A._botloop_quiet_register(group_msg("x", "reg3", sender="777"), 15)
dm7c = NullDaemon()
_calls7c = []
with patched(A, call_glm_with_fallback=glm_script(["好哒"], _calls7c), **_NP):
    A.PLUGIN_HOST.daemon = dm7c
    ALOG.clear()
    A.process(dm7c, priv_msg("真人 老李", msg_id=7203))
chk("Q7h 查不到的名字：无回报、无固定文案、无改判日志",
    dm7c.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not any("老李" in t for _c, _i, ts in dm7c.sent for t in ts),
    (dm7c.reports, dm7c.sent, alog_of("AGENT_BOTLOOP_OWNER_VERDICT")))
chk("Q7i 查不到的名字：消息落回正常处理（模型被调了、条目仍在表里）",
    len(_calls7c) >= 1 and A._botloop_quiet_find("小张") is not None,
    (len(_calls7c),))

# ================= Q8 agent：非主人发同样的私聊 → 不拦截 =================

A._BOTLOOP_QUIET_PENDING.clear()
A._BOTLOOP_QUIET_NOTIFY_TS.clear()
A._botloop_quiet_register(group_msg("x", "reg4", sender="777"), 15)
dm8 = NullDaemon()
_calls8 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯哼"], _calls8), **_NP):
    A.PLUGIN_HOST.daemon = dm8
    ALOG.clear()
    A.process(dm8, priv_msg("真人 小张", msg_id=7301, sender_id="99999",
                            chat_id="99999"))
chk("Q8a 非主人：无回报、无固定文案、无改判日志",
    dm8.reports == [] and not alog_of("AGENT_BOTLOOP_OWNER_VERDICT")
    and not any("小张" in t for _c, _i, ts in dm8.sent for t in ts),
    (dm8.reports, dm8.sent))
chk("Q8b 非主人：条目没被动过、消息走正常处理",
    A._botloop_quiet_find("小张") is not None and len(_calls8) >= 1,
    (len(_calls8),))

# ================= 收尾不变量 =================

chk("I1 降温与改判全程未新增任何判定落盘（与 Q3 后快照一致）",
    verdicts_on_disk() == _V3, (verdicts_on_disk(), _V3))
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
