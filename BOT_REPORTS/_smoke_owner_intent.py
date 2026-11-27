# -*- coding: utf-8 -*-
"""冒烟：C2 主人自然语言指令识别接线（_owner_intent_on / _intent_json /
_intent_prompt / _intent_command + process() 插入点）。

覆盖（对应派单的测试清单）：
  T1  开关关：_intent_command 直接 False、模型桩计数 0；驱动真实 process()
      也不调判定模型、落回主模型（与从前逐字一致的下限）。
  T2  前提不满足（非主人、群聊、带图、201 字、空文本）：模型桩计数 0，
      无发送、无确认；T2b 遥控开关关（与精确命令同门槛）同。
  T3  精确命令「状态」「定时列表」：走 owner_command 精确路径，判定模型
      桩计数 0、主模型 0 次。
  T4  不是指令的种种（is_cmd false、置信度 0.69、name 不在表、name 是未
      开放的写类、模型抛异常、空串、非 JSON、args 不是对象）：全部 False，
      没有确认、没有发送。
  T5  查询类与安静/继续命中（status、quiet 带参/不带参、log 不带参、
      unquiet）：owner_command 桩收到的 text＝渲染文本；发送一次、内容是
      owner_command 返回的文本；确认一次；有 AGENT_INTENT_HIT。
  T6  置信度恰等于门槛（0.7）→ 通过。
  T7  模型输出三种脏格式（嵌套 args 的多行 JSON、```json 围栏、前后夹解释
      文字）都解析正确——嵌套那种是给「不能用取首个花括号块写法」的坑钉的。
  T8  渲染后 owner_command 不命中：发确定性说明、有 AGENT_INTENT_RENDER_MISS、
      确认一次。
  T9  反问路径（把 cron_del/persona_switch/cron_add 的 intent 临时翻 True，
      测完恢复）：ask_missing 文本；带 ask_extra 的空一行附上真实的
      「定时列表」/「人设列表」回复；不带 ask_extra 的不附；T9x/T9y 附列表
      那次调用抛异常或不命中：反问照发、记 AGENT_INTENT_ASK_EXTRA_FAIL。
  T10 validate 不过：reason=validate 且日志只到参数名；日志一律不含主人
      原话、不含参数值。
  T11 端到端（照 _smoke_toolloop_wire.py 驱动真实 process()）：开关开、
      主人私聊命中后 process() 返回且没进主模型；非主人说同样的话进主模型。
  T12 提示词：默认（closed="mark"）含「暂不支持」标注与两条新规矩（未开放
      不许代替、参数不许编）；换 hide 文档时无标注。
  T13 端到端：主人说「恢复回复」/「暂停」而桩判成 resume/pause（未开放）→
      closed 挡下、不执行任何动作、落回主模型。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_commands.py 的做法）；模型调用全部打在 call_glm_with_fallback 这一层
（_intent_json 的解析被真实执行），owner_command 的执行体全打桩，零联网、
零真实数据读写、不发消息。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_owner_intent.py
"""
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_ownerintent_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402
import qq_commands as K   # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:220] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


ALOG = []
INTENT_LOG_ALL = []          # 全部 AGENT_INTENT 日志（跨用例累积，查泄漏用）
A.out = lambda line: (ALOG.append(str(line)),
                      INTENT_LOG_ALL.append(str(line))
                      if str(line).startswith("AGENT_INTENT") else None)
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None

OWNER_QQ = "10086"

RC_PATH = os.path.join(A.DATA_DIR, "reply_config.json")
_RC_PAD = [0]


def write_reply_cfg(obj):
    """写 reply_config（追加数量递增的换行保证 mtime+size 签名必变，热加载才生效）。"""
    _RC_PAD[0] += 1
    pathlib.Path(RC_PATH).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + ("\n" * _RC_PAD[0]),
        encoding="utf-8")


def set_intent(on):
    write_reply_cfg({"owner_intent_enabled": bool(on)})


def intent_logs():
    return list(INTENT_LOG_ALL)


def alog_of(tag):
    return [l for l in ALOG if tag in l]


# ---- 模型桩：判定调用（提示词带「主人的这句话：」标记）与主模型分开记账 ----

JUDGE_RAW = [None]    # 判定模型返回的原文；Exception 实例＝抛异常
INTENT_CALLS = []
MAIN_CALLS = []
MAIN_JSON = json.dumps({
    "want": 2, "say": ["好呀"], "emoji": None, "memory": [], "wait": False,
    "voice": False, "sticker": None, "quiet_minutes": None, "actions": []},
    ensure_ascii=False)


def _fake_model(messages, text_ctx="", budget_sec=200, **kw):
    prompt = str((messages or [{}])[-1].get("content") or "")
    if "主人的这句话：" in prompt:
        INTENT_CALLS.append(prompt)
        raw = JUDGE_RAW[0]
        if isinstance(raw, Exception):
            raise raw
        return "mx", ("" if raw is None else str(raw))
    MAIN_CALLS.append(prompt)
    return "mx", MAIN_JSON


MODEL_STUB = dict(call_glm_with_fallback=_fake_model)

# ---- owner_command 的执行体桩（照 _smoke_commands.py 的手法） ----

UI_CALLS = []
TAIL_CALLS = []
CRON_NOW = [{"id": 1, "time": "08:30", "text": "早报"},
            {"id": 3, "time": "21:05", "text": "晚安"}]


def fake_ui_api(path, method="GET", body=None, timeout=6):
    UI_CALLS.append((path, method, body))
    return {"ok": True, "names": ["小雪", "毒舌", "助理"], "active": "小雪",
            "groups": ["555001"], "enabled": True, "allow_all": False,
            "paused": False, "today_tokens": 1, "today_calls": 1,
            "d7_tokens": 2, "d7_calls": 2, "total_tokens": 3,
            "current_model": "链路桩", "processed": 1}


BASE_STUBS = dict(
    _ui_api=fake_ui_api,
    daemon_status_local=lambda: {"ok": True, "ws_connected": True,
                                 "login": {"nickname": "测试号",
                                           "user_id": OWNER_QQ},
                                 "stats": {"received": 1, "triggered": 2,
                                           "sent": 3}},
    _tail_log_lines=lambda name, n: (TAIL_CALLS.append((name, n))
                                     or ["%s 日志行 %d" % (name, i + 1)
                                         for i in range(max(int(n), 1))]),
    _cron_tasks=lambda: [dict(x) for x in CRON_NOW],
    _cron_save=lambda tasks: None,
    owner_qq=lambda: OWNER_QQ,
)

OWNCALLS = []     # owner_command 桩收到的 text（stub_owner=True 时记）


class OwnerDaemon(object):
    """daemon 桩：set_quiet / send / ack 全部只记录。"""

    def __init__(self):
        self.quiet_calls = []
        self.sent = []
        self.acked = []

    def set_quiet(self, minutes):
        self.quiet_calls.append(minutes)

    def group_names(self):
        return {"555001": "冒烟群"}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))


def base_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "private", "chat_id": OWNER_QQ,
         "chat_name": "主人", "message_id": "m1", "sender_id": OWNER_QQ,
         "sender_name": "主人", "triggered": True, "trigger_reason": "chat",
         "text": "在吗", "at_me": False, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


def run_intent(msg, judge_raw, stub_owner=False, owner_reply=(True, "桩回复"),
               owner_fn=None):
    """打桩跑一遍 _intent_command，返回 (handled, dm, owner_received)。
    owner_received＝owner_command 桩收到的 text 列表（stub_owner=False 时
    owner_command 走真实实现，执行体桩在 BASE_STUBS；owner_fn 直接给桩
    函数，供「让它抛异常/不命中」的用例用）。"""
    del INTENT_CALLS[:], MAIN_CALLS[:], OWNCALLS[:]
    ALOG.clear()
    JUDGE_RAW[0] = judge_raw
    dm = OwnerDaemon()
    patches = dict(MODEL_STUB)
    patches.update(BASE_STUBS)
    if owner_fn is not None:
        patches["owner_command"] = owner_fn
    elif stub_owner:
        def _own(m, daemon=None):
            OWNCALLS.append(str(m.get("text") or ""))
            return owner_reply
        patches["owner_command"] = _own
    with patched(A, **patches):
        handled = A._intent_command(dm, msg)
    return handled, dm, list(OWNCALLS)


def jcmd(name, args=None, conf=0.9, **over):
    d = {"is_cmd": True, "name": name, "args": ({} if args is None else args),
         "confidence": conf}
    d.update(over)
    return json.dumps(d, ensure_ascii=False)


# ============ T1 开关关 ============

set_intent(False)
JUDGE_RAW[0] = jcmd("status")
handled, dm, own = run_intent(base_msg(text="给我报个状态"), JUDGE_RAW[0])
chk("T1a 开关关：_intent_command 直接 False、判定模型桩计数 0、无发送无确认",
    handled is False and INTENT_CALLS == [] and dm.sent == [] and dm.acked == [],
    (handled, INTENT_CALLS))

set_intent(False)
_dm = OwnerDaemon()
A.PLUGIN_HOST.daemon = _dm
del INTENT_CALLS[:], MAIN_CALLS[:]
ALOG.clear()
try:
    with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=True,
                 REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 VCAP_FORCE=False, vision_enabled=lambda: False,
                 chat_anti_interrupt_enabled=lambda: True,
                 lean_ctx_on=lambda: True,
                 _cap_chat_allowed=lambda ct, cid: False,
                 **MODEL_STUB, **BASE_STUBS):
        A.process(_dm, base_msg(text="给我报个状态", id="t1off"))
    _exc = ""
except Exception as exc:
    _exc = repr(exc)
chk("T1b 开关关：真实 process() 不调判定模型（桩计数 0）、落回主模型、"
    "无 AGENT_INTENT 日志、不抛异常",
    INTENT_CALLS == [] and len(MAIN_CALLS) >= 1 and not intent_logs()
    and not _exc, (_exc, len(MAIN_CALLS)))

# ============ T2 前提不满足：模型桩计数 0 ============

set_intent(True)
BAD_PRE = [
    ("非主人", base_msg(sender_id="99999", chat_id="99999", chat_name="路人",
                        text="给我报个状态", id="p1")),
    ("群聊", base_msg(chat_type="group", chat_id="555001", chat_name="冒烟群",
                      text="给我报个状态", id="p2")),
    ("带图", base_msg(text="给我报个状态", image_urls=["https://x/1.jpg"],
                      id="p3")),
    ("201 字", base_msg(text="你" * 201, id="p4")),
    ("空文本", base_msg(text="   ", id="p5")),
]
for label, m in BAD_PRE:
    handled, dm, own = run_intent(m, jcmd("status"))
    chk("T2 前提不满足（%s）：False、模型桩计数 0、无发送无确认" % label,
        handled is False and INTENT_CALLS == [] and dm.sent == []
        and dm.acked == [], (handled, INTENT_CALLS))

write_reply_cfg({"owner_intent_enabled": True, "remote_control_enabled": False})
handled, dm, own = run_intent(base_msg(text="给我报个状态", id="p6"),
                              jcmd("status"))
chk("T2b 遥控开关关（与精确命令同门槛）：False、模型桩计数 0、无发送无确认",
    handled is False and INTENT_CALLS == [] and dm.sent == []
    and dm.acked == [], (handled, INTENT_CALLS))
set_intent(True)   # 恢复：后续用例的 reply_config 只带 owner_intent_enabled

# ============ T3 精确命令走精确路径：判定模型桩计数 0 ============

for text in ("状态", "定时列表"):
    _dm = OwnerDaemon()
    A.PLUGIN_HOST.daemon = _dm
    del INTENT_CALLS[:], MAIN_CALLS[:]
    ALOG.clear()
    set_intent(True)
    try:
        with patched(A, PROMPT_V2_FORCE=True, REPLY_V2_FORCE=True,
                     REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                     VCAP_FORCE=False, vision_enabled=lambda: False,
                     chat_anti_interrupt_enabled=lambda: True,
                     lean_ctx_on=lambda: True,
                     _cap_chat_allowed=lambda ct, cid: False,
                     **MODEL_STUB, **BASE_STUBS):
            A.process(_dm, base_msg(text=text, id="t3"))
        _exc = ""
    except Exception as exc:
        _exc = repr(exc)
    joined = "".join(t for ts in _dm.sent for t in ts)
    chk("T3 精确命令「%s」：owner_command 命中、判定模型与主模型都是 0 次、"
        "无 AGENT_INTENT 日志" % text,
        INTENT_CALLS == [] and MAIN_CALLS == [] and _dm.acked == ["t3"]
        and joined and not intent_logs() and not _exc,
        (_exc, joined[:60], INTENT_CALLS, MAIN_CALLS))

# ============ T4 不是指令的种种：全部 False，没有确认、没有发送 ============

NOT_CMDS = [
    ("is_cmd false", '{"is_cmd": false, "name": "", "args": {}, "confidence": 0.9}',
     "今天状态不错", ()),
    ("置信度 0.69（查询类）", jcmd("status", conf=0.69), "给我报个状态",
     ("reason=low_conf",)),
    ("name 不在表", '{"is_cmd": true, "name": "重启", "args": {}, "confidence": 0.95}',
     "重启一下你自己", ("reason=closed",)),
    ("name 是未开放的写类", jcmd("pause", conf=0.95), "先暂停，别回复任何人",
     ("reason=closed",)),
    ("模型抛异常", RuntimeError("smoke boom"), "给我报个状态",
     ("reason=no_json",)),
    ("返回空串", "", "在吗", ("reason=no_json",)),
    ("返回不是 JSON", "今天天气不错，不适合干活", "在吗", ("reason=no_json",)),
    ("args 不是对象", '{"is_cmd": true, "name": "status", "args": [1, 2], "confidence": 0.9}',
     "给我报个状态", ("reason=bad_args",)),
]
for label, raw, utter, want_log in NOT_CMDS:
    handled, dm, own = run_intent(base_msg(text=utter, id="t4"), raw)
    logs = [l for l in ALOG if l.startswith("AGENT_INTENT")]
    log_ok = all(any(w in l for l in logs) for w in want_log)
    chk("T4 不是指令（%s）：False、无确认无发送" % label,
        handled is False and dm.sent == [] and dm.acked == []
        and len(INTENT_CALLS) == 1 and log_ok,
        (handled, INTENT_CALLS, logs))

chk("T4x is_cmd false 是最常见情况：不记日志",
    run_intent(base_msg(text="今天状态不错"), NOT_CMDS[0][1])[0] is False
    and not [l for l in ALOG if l.startswith("AGENT_INTENT")], "")

# ============ T5/T6/T7 命中：渲染文本、发送、确认、AGENT_INTENT_HIT ============

HITS = [
    ("status", "给我报个状态", jcmd("status"), "状态", "read"),
    ("quiet 带参数", "先安静个五分钟吧", jcmd("quiet", {"minutes": 5}),
     "安静 5 分钟", "soft"),
    ("quiet 不带参数", "安静一下吧", jcmd("quiet"), "安静 10 分钟", "soft"),
    ("log 不带参数", "看看最近的日志", jcmd("log"), "日志", "read"),
    ("unquiet", "好了，可以说话了", jcmd("unquiet"), "继续", "soft"),
]
for label, utter, raw, rendered, risk in HITS:
    handled, dm, own = run_intent(base_msg(text=utter, id="t5"), raw,
                                  stub_owner=True)
    hits = [l for l in ALOG if l.startswith("AGENT_INTENT_HIT")]
    cname = raw and json.loads(raw).get("name")
    chk("T5 命中（%s）：owner_command 桩收到渲染文本「%s」、发送一次且内容是"
        "桩回复、确认一次、有 AGENT_INTENT_HIT（name/risk=%s）"
        % (label, rendered, risk),
        handled is True and own == [rendered] and dm.sent == [["桩回复"]]
        and dm.acked == ["t5"] and len(hits) == 1
        and ("name=%s" % cname) in hits[0] and ("risk=%s" % risk) in hits[0],
        (handled, own, dm.sent, dm.acked, hits))

handled, dm, own = run_intent(base_msg(text="给我报个状态", id="t6"),
                              jcmd("status", conf=0.7), stub_owner=True)
chk("T6 置信度恰等于门槛 0.7 → 通过",
    handled is True and own == ["状态"] and dm.sent == [["桩回复"]],
    (handled, own, dm.sent))

DIRTY = [
    ("嵌套 args 的多行 JSON",
     '{\n  "is_cmd": true,\n  "name": "quiet",\n  "args": {"minutes": 5},\n'
     '  "confidence": 0.9\n}', "安静 5 分钟"),
    ("```json 围栏",
     '```json\n{"is_cmd": true, "name": "log", "args": {"n": 30}, '
     '"confidence": 0.9}\n```', "日志 30"),
    ("前后夹解释文字",
     '我看了一下，这句话是在看日志：\n'
     '{"is_cmd": true, "name": "log", "args": {}, "confidence": 0.85}\n'
     '以上。', "日志"),
]
for label, raw, rendered in DIRTY:
    handled, dm, own = run_intent(base_msg(text="看看最近的日志", id="t7"),
                                  raw, stub_owner=True)
    chk("T7 脏格式解析（%s）：owner_command 收到「%s」" % (label, rendered),
        handled is True and own == [rendered] and dm.sent == [["桩回复"]],
        (handled, own, dm.sent))

# ============ T8 渲染后 owner_command 不命中 ============

def _own_miss(m, daemon=None):
    OWNCALLS.append(str(m.get("text") or ""))
    return False, ""

del INTENT_CALLS[:], OWNCALLS[:]
ALOG.clear()
JUDGE_RAW[0] = jcmd("status")
dm = OwnerDaemon()
with patched(A, **MODEL_STUB, **BASE_STUBS, owner_command=_own_miss):
    handled = A._intent_command(dm, base_msg(text="给我报个状态", id="t8"))
chk("T8 owner_command 不命中：发确定性说明、有 RENDER_MISS、无 AGENT_INTENT_HIT、"
    "确认一次",
    handled is True and dm.sent == [["这条没有执行，请直接发：状态"]]
    and dm.acked == ["t8"] and alog_of("AGENT_INTENT_RENDER_MISS")
    and not alog_of("AGENT_INTENT_HIT")
    and OWNCALLS == ["状态"],
    (handled, dm.sent, dm.acked, OWNCALLS, ALOG))

# ============ T9 反问路径（intent 临时翻 True，测完恢复） ============

_cd = K.by_name("cron_del")
_ps = K.by_name("persona_switch")
_ca = K.by_name("cron_add")
_ga = K.by_name("group_add")
for _e in (_cd, _ps, _ca, _ga):
    _e["intent"] = True
try:
    ASKS = [
        ("cron_del 缺参：ask_missing + 空一行附定时列表",
         "把那个不要的定时删了吧", jcmd("cron_del"),
         "删哪个？报个编号（定时列表里的 #号）", "定时代码任务："),
        ("persona_switch 缺参：ask_missing + 空一行附人设列表",
         "切换到那个新的人设吧", jcmd("persona_switch"),
         "切换到哪个人设？报个名字", "人设：小雪、毒舌、助理"),
        ("cron_add 只给了时间：只有 ask_missing，不附列表",
         "每天八点半提醒我给花浇水", jcmd("cron_add", {"time": "08:30"}),
         "定几点、做什么？一句话说全，比如「每天八点半提醒我喝水」", None),
    ]
    for label, utter, raw, ask, extra in ASKS:
        handled, dm, own = run_intent(base_msg(text=utter, id="t9"), raw)
        reply = dm.sent[0][0] if dm.sent else ""
        ok = (handled is True and len(dm.sent) == 1
              and reply.startswith(ask) and dm.acked == ["t9"]
              and alog_of("AGENT_INTENT_ASK name="))
        if extra is not None:
            ok = ok and ("\n\n" + extra) in reply and len(reply) > len(ask) + 2
        else:
            ok = ok and reply == ask
        chk("T9 反问（%s）" % label, ok, (handled, reply[:120], dm.acked))

    # T9x 附列表那次调用抛异常：反问照常发出（不附列表）、有 ASK_EXTRA_FAIL 日志
    def _own_boom(m, daemon=None):
        raise RuntimeError("smoke: 附列表调用炸了")

    handled, dm, own = run_intent(base_msg(text="把那个不要的定时删了吧",
                                           id="t9x"),
                                  jcmd("cron_del"), owner_fn=_own_boom)
    _fail = alog_of("AGENT_INTENT_ASK_EXTRA_FAIL")
    chk("T9x 附列表调用抛异常：反问照发（不附列表）、一条 ASK_EXTRA_FAIL（带异常）、"
        "有 AGENT_INTENT_ASK、确认一次",
        handled is True and len(dm.sent) == 1
        and dm.sent[0][0].startswith("删哪个？报个编号（定时列表里的 #号）")
        and "\n\n" not in dm.sent[0][0]
        and len(_fail) == 1 and "smoke: 附列表调用炸了" in _fail[0]
        and alog_of("AGENT_INTENT_ASK name=cron_del") and dm.acked == ["t9x"],
        (handled, dm.sent, _fail, dm.acked))

    # T9y 附列表不命中：同样不静默，记 reason=no_hit
    def _own_miss(m, daemon=None):
        return False, ""

    handled, dm, own = run_intent(base_msg(text="把那个不要的定时删了吧",
                                           id="t9y"),
                                  jcmd("cron_del"), owner_fn=_own_miss)
    _fail = alog_of("AGENT_INTENT_ASK_EXTRA_FAIL")
    chk("T9y 附列表不命中：反问照发、一条 ASK_EXTRA_FAIL reason=no_hit、确认一次",
        handled is True and len(dm.sent) == 1
        and dm.sent[0][0].startswith("删哪个？报个编号")
        and "\n\n" not in dm.sent[0][0]
        and len(_fail) == 1 and "reason=no_hit" in _fail[0]
        and dm.acked == ["t9y"], (handled, dm.sent, _fail, dm.acked))

    # T10 validate 不过：日志只到类别与参数名，不带参数值
    handled, dm, own = run_intent(base_msg(text="把 3 号定时删了", id="t10"),
                                  jcmd("cron_del", {"id": 99}))
    vlogs = alog_of("reason=validate")
    chk("T10a validate 不过：False、无发送无确认、日志带 param=id",
        handled is False and dm.sent == [] and dm.acked == []
        and len(vlogs) == 1 and "param=id" in vlogs[0], (handled, vlogs))
    handled, dm, own = run_intent(
        base_msg(text="把 555321 这个群拉进来", id="t10b"),
        jcmd("group_add", {"gid": "555999"}))
    vlogs = alog_of("reason=validate")
    chk("T10b validate 不过（gid 非原话子串）：False、日志 param=gid",
        handled is False and dm.sent == [] and len(vlogs) == 1
        and "param=gid" in vlogs[0], (handled, vlogs))
finally:
    for _e in (_cd, _ps, _ca, _ga):
        _e["intent"] = False

# ============ T10c 日志不泄漏主人原话与参数值 ============

LEAK_CHECK = [
    "给我报个状态", "先安静个五分钟吧", "安静一下吧", "看看最近的日志",
    "好了，可以说话了", "把那个不要的定时删了吧", "切换到那个新的人设吧",
    "每天八点半提醒我给花浇水", "把 3 号定时删了", "把 555321 这个群拉进来",
    "先暂停，别回复任何人", "重启一下你自己", "今天状态不错",
]
leaked = [w for w in LEAK_CHECK
          if any(w in l for l in INTENT_LOG_ALL)]
leaked += [v for v in ("555999", "555321", "99")
           if any(v in l for l in INTENT_LOG_ALL)]
chk("T10c 全部 AGENT_INTENT 日志不含主人原话、不含参数值",
    not leaked, leaked[:6])

# ============ T11 端到端：驱动真实 process() ============


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


E2E_FORCE = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=True,
                 REPLY_JSON_FORCE=True, REACT_ONLY_FORCE=False,
                 VCAP_FORCE=False, vision_enabled=lambda: False,
                 chat_anti_interrupt_enabled=lambda: True,
                 lean_ctx_on=lambda: True,
                 _cap_chat_allowed=lambda ct, cid: False)


def run_process(msg, judge_raw):
    dm = OwnerDaemon()
    A.PLUGIN_HOST.daemon = dm
    del INTENT_CALLS[:], MAIN_CALLS[:], UI_CALLS[:]
    ALOG.clear()
    del INTENT_LOG_ALL[:]      # T10c 的泄漏检查在前面已做完，端到端重开账本
    JUDGE_RAW[0] = judge_raw
    exc = ""
    try:
        with patched(A, **E2E_FORCE, **MODEL_STUB, **BASE_STUBS):
            reset_cap()
            A.process(dm, msg)
    except Exception as e:
        exc = repr(e)
    return dm, exc


# 反问路径结束后把开关文件保持在开
set_intent(True)
dm, exc = run_process(base_msg(text="给我报个状态", id="e1"), jcmd("status"))
joined = "".join(t for ts in dm.sent for t in ts)
chk("T11a 端到端：开关开+主人私聊，命中后 process() 返回、没进主模型、"
    "发出状态回复、确认一次、有 AGENT_INTENT_HIT",
    exc == "" and len(INTENT_CALLS) == 1 and MAIN_CALLS == []
    and "管道：运行" in joined and "测试号" in joined
    and dm.acked == ["e1"] and alog_of("AGENT_INTENT_HIT name=status"),
    (exc, len(INTENT_CALLS), len(MAIN_CALLS), joined[:80]))

dm, exc = run_process(base_msg(sender_id="99999", chat_id="99999",
                               chat_name="路人", text="给我报个状态", id="e2"),
                      jcmd("status"))
chk("T11b 端到端：非主人说同样的话 → 不调判定模型、进主模型、无 AGENT_INTENT 日志",
    exc == "" and INTENT_CALLS == [] and len(MAIN_CALLS) >= 1
    and not intent_logs(), (exc, len(INTENT_CALLS), len(MAIN_CALLS)))

# ============ T12 提示词：默认 mark 标注与新规矩 ============

_p = A._intent_prompt("在吗")
_p_hide = A._intent_prompt("在吗", K.intent_doc(closed="hide"))
chk("T12a _intent_prompt 默认含「暂不支持」标注（未开放条目在清单里）与新规矩两条",
    "暂不支持" in _p
    and "不要换成清单里的其它条目" in _p          # 清单标注原文
    and "不要用清单里的其它条目代替" in _p        # 规矩①
    and "主人没说的参数不要填" in _p              # 规矩②
    and "pause" in _p and "resume" in _p, "")
chk("T12b 换 hide 文档：无标注行（标注原文不在）、无未开放命令名（规矩文字仍在）",
    "不要换成清单里的其它条目" not in _p_hide and "pause" not in _p_hide
    and "不要用清单里的其它条目代替" in _p_hide, "")

# ============ T13 端到端：主人要的正是未开放指令 → closed 挡下，不执行、落回主模型 ============

dm, exc = run_process(base_msg(text="好了，恢复回复吧", id="e3"), jcmd("resume"))
joined = "".join(t for ts in dm.sent for t in ts)
chk("T13a 端到端：桩判 resume（未开放）→ reason=closed、没执行「继续」"
    "（set_quiet 未调）、回复里没有「恢复正常聊天」、落回主模型、无 HIT",
    exc == "" and len(INTENT_CALLS) == 1 and len(MAIN_CALLS) >= 1
    and alog_of("reason=closed") and not alog_of("AGENT_INTENT_HIT")
    and dm.quiet_calls == [] and "恢复正常聊天" not in joined,
    (exc, len(MAIN_CALLS), [l for l in ALOG if l.startswith("AGENT_INTENT")],
     dm.quiet_calls))

dm, exc = run_process(base_msg(text="先暂停，别回复任何人", id="e4"), jcmd("pause"))
chk("T13b 端到端：桩判 pause（未开放）→ reason=closed、没动管理台"
    "（无 /api/pause）、落回主模型、无 HIT",
    exc == "" and len(INTENT_CALLS) == 1 and len(MAIN_CALLS) >= 1
    and alog_of("reason=closed") and not alog_of("AGENT_INTENT_HIT")
    and not [u for u in UI_CALLS if u[0] == "/api/pause"]
    and dm.quiet_calls == [],
    (exc, len(MAIN_CALLS), [l for l in ALOG if l.startswith("AGENT_INTENT")],
     UI_CALLS))

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
set_intent(False)
# 收尾：先关掉 import 时打开的常驻文件句柄（usage 的 FileHandler 锁着
# usage_log.jsonl，不关的话 Windows 下 rmtree 删不掉隔离目录）
for _h in list(A._usage_log.handlers):
    try:
        _h.close()
    except Exception:
        pass
    A._usage_log.removeHandler(_h)
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
