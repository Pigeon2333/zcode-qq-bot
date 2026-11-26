# -*- coding: utf-8 -*-
"""离线冒烟：反复撞线的硬信号（WP-02，botloop_quiet_mode 开关下，默认关）。

R1 开关关：不计数（rounds 表全空）、cleared 不配合升级照旧 confirmed 30 分钟。
R2 开关开：三次上升沿（中间有 False）后第三次触发——进 quiet 相、闸门返回
   (True,"botloop_quiet")、info 带 src=repeat、日志 botloop_repeat 带
   rounds/window_min、列表清零、quiet 期间静默。
R3 连续为 True 只计一轮。R4 判定文件里是 human 的账号不计数不触发。
R5 三次上升沿在 0/10/45 分钟（窗口 30）：45 分钟那次不触发（前两轮已出窗）。
R6 开关开时 cleared 不配合升级改为 quiet + (True,"botloop_quiet")、src=uncooperative。
R7 收件箱集成：闸门触发后 item 带 trigger_reason=botloop_quiet 与
   botloop_quiet_src=repeat、无 base_trigger。
A1 agent 收到 botloop_quiet 项：登记待改判、私聊主人一次、ack、模型零调用、
   群里零发送、日志 AGENT_BOTLOOP_QUIET 带 quiet_min 与 source=repeat；
A2 一小时内第二次不再发（notice=no-rate）；A3 source=uncooperative 透传。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_botloop_repeat.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_botloop_repeat_iso_")
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


def clear_hist(d, chat_id="555001"):
    d.mem_history["group_%s" % chat_id].clear()


VPATH = D.BOTLOOP_VERDICTS_PATH
_VPAD = [0]


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


RCPATH = A.REPLY_CONFIG_PATH
_RCPAD = [0]


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
    """agent 侧桩：只显式提供 process(botloop_quiet 分支) 会用到的方法。"""

    def __init__(self, group_names=None):
        self._group_names = dict(group_names or {})
        self.acked = []
        self.sent = []      # (chat_type, chat_id, texts)

    def group_names(self):
        return dict(self._group_names)

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append((chat_type, chat_id, list(texts)))

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
    def _glm(messages, text_ctx="", budget_sec=200):
        calls.append(messages)
        r = resps[min(len(calls) - 1, len(resps) - 1)]
        if isinstance(r, BaseException):
            raise r
        return ("m", r) if isinstance(r, str) else r
    return _glm


_NP = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
           REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
           vision_enabled=lambda: False,
           chat_anti_interrupt_enabled=lambda: True,
           lean_ctx_on=lambda: True, reply_v2_on=lambda: False,
           _cap_chat_allowed=lambda ct, cid: False,
           owner_qq=lambda: "10086",
           owner_command=lambda m, d: (False, ""))


def rc_on(hits=3, win=30, qmin=9):
    """开关开的闸门配置桩（guard 打开＋悄悄降温＋撞线参数）。"""
    return {"reply_mode": "keyword", "keywords": ["你是AI"],
            "text_img_merge_enabled": False, "burst_merge_enabled": False,
            "botloop_guard_enabled": True, "botloop_quiet_mode": True,
            "botloop_quiet_minutes": qmin, "botloop_repeat_hits": hits,
            "botloop_repeat_window_min": win}


def rc_off():
    """开关关的闸门配置桩（其余与 rc_on 一致，逐字对照开关差异）。"""
    rc = rc_on()
    rc["botloop_quiet_mode"] = False
    return rc


def _info(at_me=False):
    return {"text": "你是AI吧", "image_urls": [], "at_me": at_me,
            "at_users": [], "has_reply": False, "reply_id": "",
            "reply_text": "", "face_count": 0, "record": False,
            "record_file": "", "record_url": "", "forward_lines": []}


BK = "group|555001|777"
rm_verdicts()

# ================= R1 开关关：不计数、老路径不变 =================

d1 = fresh()
d1._reply_config = rc_off
push_hits(d1, "555001", "777")
chk("R1a 开关关：命中照常进 probe", d1._botloop_gate(
    "group", "555001", "777", "x") == (True, "botloop_probe"))
push_hits(d1, "555001", "777")
chk("R1b 开关关：再命中不计数（rounds 表全空）",
    d1._botloop_gate("group", "555001", "777", "x") == (False, "botloop")
    and d1._botloop_rounds == {}, d1._botloop_rounds)
d1.botloop_report("group", "555001", "777", False, 60, precheck=True)
chk("R1c 开关关：回报后进 cleared", d1._botloop[BK]["phase"] == "cleared")
push_hits(d1, "555001", "777")
d1._botloop[BK]["last_hit"] = time.time() - 121
chk("R1d 开关关：不配合第 1 轮放行（hits=1）",
    d1._botloop_gate("group", "555001", "777", "x") is None
    and d1._botloop[BK].get("hits") == 1)
d1._botloop[BK]["last_hit"] = time.time() - 121
DLOG.clear()
_g1 = d1._botloop_gate("group", "555001", "777", "x")
chk("R1e 开关关：不配合第 2 轮照旧 confirmed 30 分钟 + (False,'botloop')",
    _g1 == (False, "botloop")
    and d1._botloop[BK]["phase"] == "confirmed"
    and abs(d1._botloop[BK]["until"] - (time.time() + 1800)) < 5
    and len(dlog_of("botloop_uncooperative")) == 1
    and dlog_of("botloop_repeat") == [],
    (_g1, d1._botloop[BK], dlog_of("botloop_repeat")))
chk("R1f 开关关：全程 rounds 表保持为空", d1._botloop_rounds == {},
    d1._botloop_rounds)

# ================= R2 开关开：三次上升沿第三次触发 =================

d2 = fresh()
d2._reply_config = lambda: rc_on(hits=3, win=30, qmin=9)
_info2 = {}
push_hits(d2, "555001", "777")
chk("R2a 第 1 次上升沿：进 probe（不触发）", d2._botloop_gate(
    "group", "555001", "777", "x", info=_info2) == (True, "botloop_probe")
    and len(d2._botloop_rounds[BK]["times"]) == 1)
clear_hist(d2)
chk("R2b 命中转 False：不新增轮次",
    d2._botloop_gate("group", "555001", "777", "x") == (False, "botloop")
    and len(d2._botloop_rounds[BK]["times"]) == 1
    and d2._botloop_rounds[BK]["prev"] is False)
push_hits(d2, "555001", "777")
chk("R2c 第 2 次上升沿：计 2 轮、仍不触发",
    d2._botloop_gate("group", "555001", "777", "x") == (False, "botloop")
    and len(d2._botloop_rounds[BK]["times"]) == 2)
clear_hist(d2)
d2._botloop_gate("group", "555001", "777", "x")
push_hits(d2, "555001", "777")
DLOG.clear()
_g2 = d2._botloop_gate("group", "555001", "777", "x", info=_info2)
_st2 = d2._botloop.get(BK)
chk("R2d 第 3 次上升沿：触发 quiet、闸门返回 (True,'botloop_quiet')",
    _g2 == (True, "botloop_quiet") and _st2.get("phase") == "quiet"
    and abs(_st2.get("until", 0) - (time.time() + 9 * 60)) < 5,
    (_g2, _st2))
chk("R2e 触发后该发送者的轮次列表清零",
    d2._botloop_rounds[BK]["times"] == [], d2._botloop_rounds[BK])
chk("R2f info 带 src=repeat（收件箱项据此记 source）",
    _info2.get("botloop_quiet_src") == "repeat", _info2)
chk("R2g 日志 botloop_repeat 带 rounds=3 window_min=30",
    any('"msg": "botloop_repeat"' in l and '"rounds": 3' in l
        and '"window_min": 30' in l for l in dlog_of("botloop_repeat")),
    dlog_of("botloop_repeat"))
chk("R2h quiet 相内该账号消息被静默（与 confirmed 同形）",
    d2._botloop_gate("group", "555001", "777", "x") == (False, "botloop")
    and _st2.get("phase") == "quiet", _st2)

# ================= R3 连续为 True 只计一轮 =================

DLOG.clear()
d3 = fresh()
d3._reply_config = lambda: rc_on()
push_hits(d3, "555001", "777")
d3._botloop_gate("group", "555001", "777", "x")   # 上升沿 1 → probe
push_hits(d3, "555001", "777")
d3._botloop_gate("group", "555001", "777", "x")
push_hits(d3, "555001", "777")
d3._botloop_gate("group", "555001", "777", "x")
chk("R3 连续命中不新增轮次（仍 1 轮、未触发）",
    len(d3._botloop_rounds[BK]["times"]) == 1
    and dlog_of("botloop_repeat") == [],
    (d3._botloop_rounds[BK], dlog_of("botloop_repeat")))

# ================= R4 判定文件 human 不计数不触发 =================

DLOG.clear()
rm_verdicts()
write_verdicts({"777": {"kind": "human", "ts": NOW()}})
d4 = fresh()
d4._reply_config = lambda: rc_on()
push_hits(d4, "555001", "777")
chk("R4a human 判定：hit 恒 False、不进任何相",
    d4._botloop_gate("group", "555001", "777", "x") is None
    and d4._botloop == {} and d4._botloop_rounds[BK]["times"] == [],
    (d4._botloop, d4._botloop_rounds))
push_hits(d4, "555001", "777")
chk("R4b human 判定：反复命中也不触发、无 repeat 日志",
    d4._botloop_gate("group", "555001", "777", "x") is None
    and dlog_of("botloop_repeat") == [], dlog_of("botloop_repeat"))
rm_verdicts()

# ===== R5 上升沿在 0/10/45 分钟（窗口 30）：45 分钟那次不触发 =====

d5 = fresh()
d5._reply_config = lambda: rc_on(hits=3, win=30)
d5._botloop_rounds[BK] = {"times": [time.time() - 45 * 60,
                                    time.time() - 35 * 60],
                          "prev": False}
push_hits(d5, "555001", "777")
DLOG.clear()
chk("R5 出窗后只剩 1 轮：45 分钟那次不触发（重新进 probe）",
    d5._botloop_gate("group", "555001", "777", "x") == (True, "botloop_probe")
    and len(d5._botloop_rounds[BK]["times"]) == 1
    and dlog_of("botloop_repeat") == [],
    (d5._botloop_rounds[BK], dlog_of("botloop_repeat")))

# ===== R6 开关开：cleared 不配合升级改走 quiet =====

d6 = fresh()
d6._reply_config = lambda: rc_on(hits=3, win=30, qmin=6)
push_hits(d6, "555001", "777")
d6._botloop_gate("group", "555001", "777", "x")            # probe
d6.botloop_report("group", "555001", "777", False, 60, precheck=True)
chk("R6a 开关开：回报后 cleared（预判放行的拟人化机器人）",
    d6._botloop[BK]["phase"] == "cleared")
push_hits(d6, "555001", "777")
d6._botloop[BK]["last_hit"] = time.time() - 121
_info6 = {}
chk("R6b 不配合第 1 轮仍放行", d6._botloop_gate(
    "group", "555001", "777", "x", info=_info6) is None
    and d6._botloop[BK].get("hits") == 1
    and "botloop_quiet_src" not in _info6)
d6._botloop[BK]["last_hit"] = time.time() - 121
_g6 = d6._botloop_gate("group", "555001", "777", "x", info=_info6)
_st6 = d6._botloop.get(BK)
chk("R6c 不配合第 2 轮：quiet 相（不再 confirmed）、(True,'botloop_quiet')",
    _g6 == (True, "botloop_quiet") and _st6.get("phase") == "quiet"
    and abs(_st6.get("until", 0) - (time.time() + 6 * 60)) < 5
    and _info6.get("botloop_quiet_src") == "uncooperative",
    (_g6, _st6, _info6))
chk("R6d 该路径无 uncooperative/repeat 日志（来源靠 item 标记）",
    dlog_of("botloop_uncooperative") == [] and dlog_of("botloop_repeat") == [],
    (dlog_of("botloop_uncooperative"), dlog_of("botloop_repeat")))

# ================= R7 收件箱集成 =================

d7 = fresh()
d7._reply_config = lambda: rc_on()
d7._botloop_rounds[BK] = {"times": [time.time() - 60, time.time() - 50],
                          "prev": False}
push_hits(d7, "555001", "777", ts=NOW() - 60)
d7._ingest_message({}, "group", "555001", "冒烟群", "777", "小张", _info(True))
it7 = d7.inbox[-1]
chk("R7a 闸门触发后 item 带 trigger_reason=botloop_quiet",
    it7["trigger_reason"] == "botloop_quiet", it7["trigger_reason"])
chk("R7b item 带 botloop_quiet_src=repeat、无 base_trigger（不发群消息的纯通知项）",
    it7.get("botloop_quiet_src") == "repeat"
    and "base_trigger" not in it7,
    (it7.get("botloop_quiet_src"), it7.get("base_trigger")))
chk("R7c daemon 状态进 quiet 相", d7._botloop[BK]["phase"] == "quiet")

# ================= A1-A3 agent 收 botloop_quiet 项 =================

rm_reply_cfg()
write_reply_cfg({"botloop_quiet_mode": True, "botloop_quiet_minutes": 7})
A._BOTLOOP_QUIET_PENDING.clear()
A._BOTLOOP_QUIET_NOTIFY_TS.clear()
dmA = NullDaemon(group_names={"555001": "冒烟群"})
_callsA = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _callsA), **_NP):
    A.PLUGIN_HOST.daemon = dmA
    ALOG.clear()
    A.process(dmA, group_msg("你是AI吧", "qa1", trig="botloop_quiet",
                             sender="777", msg_id=7501))
_noticeA = ("「冒烟群」里「小张」的说话节奏像机器人，我先不接 TA 的话了，"
            "约 7 分钟后自动恢复。")
_noticeAb = "认错了就回我「真人 小张」，确认是机器人就回我「机器人 小张」。"
chk("A1a 登记待改判条目（quiet_min 取 _botloop_quiet_cfg()[1]=7）",
    (A._botloop_quiet_find("小张") or {}).get("quiet_min") == 7
    and (A._botloop_quiet_find("777") or {}).get("chat_id") == "555001",
    A._botloop_quiet_find("小张"))
chk("A1b 只私聊主人一声、群里零发送、模型零调用",
    dmA.sent == [("private", "10086", [_noticeA, _noticeAb])]
    and _callsA == [], (dmA.sent, _callsA))
chk("A1c 消息被 ack、日志 AGENT_BOTLOOP_QUIET 带 source=repeat",
    dmA.acked == [7501]
    and any(l == "AGENT_BOTLOOP_QUIET msg_id=7501 chat=group/555001 "
            "sender=777 quiet_min=7 notice=yes source=repeat"
            for l in alog_of("AGENT_BOTLOOP_QUIET")),
    (dmA.acked, alog_of("AGENT_BOTLOOP_QUIET")))

dmA2 = NullDaemon(group_names={"555001": "冒烟群"})
_callsA2 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _callsA2), **_NP):
    A.PLUGIN_HOST.daemon = dmA2
    ALOG.clear()
    A.process(dmA2, group_msg("你是AI吧", "qa2", trig="botloop_quiet",
                              sender="777", msg_id=7502))
chk("A2 一小时内第二次：登记照做、不再私聊（notice=no-rate）",
    (A._botloop_quiet_find("小张") or {}).get("quiet_min") == 7
    and dmA2.sent == [] and _callsA2 == []
    and "notice=no-rate" in (alog_of("AGENT_BOTLOOP_QUIET") or [""])[0],
    (dmA2.sent, alog_of("AGENT_BOTLOOP_QUIET")))

dmA3 = NullDaemon(group_names={"555001": "冒烟群"})
_callsA3 = []
with patched(A, call_glm_with_fallback=glm_script(["嗯"], _callsA3), **_NP):
    A.PLUGIN_HOST.daemon = dmA3
    ALOG.clear()
    A.process(dmA3, group_msg("你是AI吧", "qa3", trig="botloop_quiet",
                              sender="888", sender_name="小AI", msg_id=7503,
                              botloop_quiet_src="uncooperative"))
chk("A3 source=uncooperative 透传到日志",
    any(l.startswith("AGENT_BOTLOOP_QUIET msg_id=7503")
        and l.endswith("source=uncooperative")
        for l in alog_of("AGENT_BOTLOOP_QUIET")),
    alog_of("AGENT_BOTLOOP_QUIET"))

# ================= 不变量 =================

chk("I1 全程未写判定落盘（quiet 路径不落任何判定）",
    verdicts_on_disk() is None and not os.path.exists(VPATH),
    verdicts_on_disk())
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
