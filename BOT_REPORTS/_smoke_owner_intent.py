# -*- coding: utf-8 -*-
"""冒烟：C2/C3 主人自然语言指令识别接线（_owner_intent_on / _intent_json /
_intent_prompt / _intent_command + process() 插入点；C3 起 write 九条开放，
带复述确认与续跑）。

覆盖（对应派单的测试清单）：
  T1  开关关：_intent_command 直接 False、模型桩计数 0；驱动真实 process()
      也不调判定模型、落回主模型（与从前逐字一致的下限）。
  T2  前提不满足（非主人、群聊、带图、201 字、空文本）：模型桩计数 0，
      无发送、无确认；T2b 遥控开关关（与精确命令同门槛）同；T2c 带合并
      转发内容（forward_lines 非空）不调模型（转发记录里的话不是主人说的）。
  T3  精确命令「状态」「定时列表」：走 owner_command 精确路径，判定模型
      桩计数 0、主模型 0 次。
  T4  不是指令的种种（is_cmd false、查询类置信度 0.69、name 不在表、write
      置信度 0.84 低于门槛 0.85、模型抛异常、空串、非 JSON、args 不是对象）：
      全部 False，没有确认、没有发送。
  T5  查询类与安静/继续命中（status、quiet 带参/不带参、log 不带参、
      unquiet）：owner_command 桩收到的 text＝渲染文本；发送一次、内容是
      owner_command 返回的文本；确认一次；有 AGENT_INTENT_HIT。
  T6  置信度恰等于门槛（0.7）→ 通过。
  T7  模型输出三种脏格式（嵌套 args 的多行 JSON、```json 围栏、前后夹解释
      文字）都解析正确——嵌套那种是给「不能用取首个花括号块写法」的坑钉的。
  T8  渲染后 owner_command 不命中：发确定性说明、有 AGENT_INTENT_RENDER_MISS、
      确认一次。
  T9  反问路径（缺必填参数）：ask_missing 文本；带 ask_extra 的空一行附上
      真实的「定时列表」/「人设列表」回复；不带 ask_extra 的不附；T9x/T9y
      附列表那次调用抛异常或不命中：反问照发、记 AGENT_INTENT_ASK_EXTRA_FAIL。
  T10 validate 不过（C3 起不再悄悄掉回聊天路径）：回「没能当成指令来执行」
      的说明（含 usage 精确写法）、不执行、记 AGENT_INTENT_MISS（只到参数
      名）；日志一律不含主人原话、不含参数值。
  T11 端到端（照 _smoke_toolloop_wire.py 驱动真实 process()）：开关开、
      主人私聊命中后 process() 返回且没进主模型；非主人说同样的话进主模型。
  T12 提示词：C3 起全部开放——默认文档＝hide 文档、无「暂不支持」标注；
      默认提示词不含标注说明与「不许代替」规矩两句（清单没有标注就不说）；
      传带标注的 doc 进去时两句都在；判定规矩仍在。
  T13 端到端（write 直接执行，tool_confirm_write 关）：主人说「恢复回复」
      桩判 resume → 真实执行（管理台 POST /api/pause paused=False）、主模型
      桩零调用（同一轮两种闸：意图命中即 return，不进工具循环）、无待确认。
  T14 write 九条各一条口语例句（tool_confirm_write 关）：直接执行，owner
      桩收到的 text＝渲染文本，回复＝桩回复，有 AGENT_INTENT_HIT。
  T15 write 置信度门槛：0.84 → 走聊天不执行；0.85 → 通过。
  T16 标识型参数不在原话（群号/定时编号/人设名/任务原文各一条）→ 回说明
      （含精确命令写法）、不执行、有 AGENT_INTENT_MISS。
  T17 枚举「开 / 关」别名归一后执行；归一后仍不在选项的 → MISS 说明。
  T18 复述确认与续跑（tool_confirm_write 开，端到端驱动真实 process()）：
      命中 → 回复是复述、未执行、主回复发送成功后待确认表里有 kind="cmd"
      一格；发送失败不落表；主人回「确认」→ 执行并发回复（CONFIRM_DONE）、
      判定模型桩零调用（续跑不许被判定模型抢走）；「取消」→ 作废走聊天；
      过期 → 作废；非主人/别的会话「确认」→ 不触发、待确认原样保留；已有
      待确认时再一条 write 指令 → 回「上一条还在等确认」、旧待确认原样不变
      （在 _intent_command 层测：进程级路径上任何主人消息都会先被拦截区
      消费旧待确认，照 _smoke_toolloop_confirm.py C14c 的手法）。
  T19 带合并转发内容的消息端到端：判定模型桩零调用、落回主模型。

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
import time

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
    """daemon 桩：set_quiet / send / ack 全部只记录；send 返回 {"ok": …}
    （复述确认落表只认发送成功的 ok，send_ok=False 模拟发送失败）。"""

    def __init__(self):
        self.quiet_calls = []
        self.sent = []
        self.acked = []
        self.send_ok = True

    def set_quiet(self, minutes):
        self.quiet_calls.append(minutes)

    def group_names(self):
        return {"555001": "冒烟群"}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))
        return {"ok": bool(self.send_ok)}


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

# T2c 带合并转发内容（forward_lines 非空）：转发记录里的话不是主人说的，
# 不调判定模型
handled, dm, own = run_intent(base_msg(text="转发过来的记录帮我看看",
                                       forward_lines=["小明: 你好"], id="p7"),
                              jcmd("status"))
chk("T2c 带合并转发内容：False、判定模型桩计数 0、无发送无确认",
    handled is False and INTENT_CALLS == [] and dm.sent == []
    and dm.acked == [], (handled, INTENT_CALLS))

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
    ("write 置信度 0.84（门槛 0.85，C3 设计变更：原「未开放写类」用例随"
     "pause 开放改成低置信度负例）", jcmd("pause", conf=0.84),
     "先暂停，别回复任何人", ("reason=low_conf",)),
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

# ============ T9 反问路径（C3 起这四条 intent 本来就开着，不再临时翻） ============

_cd = K.by_name("cron_del")
_ps = K.by_name("persona_switch")
_ca = K.by_name("cron_add")
_ga = K.by_name("group_add")
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

    # T10 validate 不过（C3 起不再悄悄掉回聊天路径）：回「没能当成指令来
    # 执行」的说明（含 usage 精确写法）、不执行、记 AGENT_INTENT_MISS；
    # 日志只到参数名，不带参数值
    handled, dm, own = run_intent(base_msg(text="把 3 号定时删了", id="t10"),
                                  jcmd("cron_del", {"id": 99}), stub_owner=True)
    vlogs = alog_of("AGENT_INTENT_MISS")
    chk("T10a validate 不过：回说明（含「删定时 编号」）、不执行、有 MISS "
        "param=id（日志无参数值）、确认一次",
        handled is True and own == [] and len(dm.sent) == 1
        and "没能当成指令来执行" in dm.sent[0][0]
        and "删定时 编号" in dm.sent[0][0]
        and dm.acked == ["t10"] and len(vlogs) == 1
        and "param=id" in vlogs[0] and "99" not in vlogs[0],
        (handled, own, dm.sent, vlogs))
    handled, dm, own = run_intent(
        base_msg(text="把 555321 这个群拉进来", id="t10b"),
        jcmd("group_add", {"gid": "555999"}), stub_owner=True)
    vlogs = alog_of("AGENT_INTENT_MISS")
    chk("T10b validate 不过（gid 非原话子串）：回说明（含「加群 群号」）、"
        "不执行、有 MISS param=gid",
        handled is True and own == [] and len(dm.sent) == 1
        and "没能当成指令来执行" in dm.sent[0][0] and "加群 群号" in dm.sent[0][0]
        and len(vlogs) == 1 and "param=gid" in vlogs[0]
        and "555999" not in vlogs[0], (handled, own, dm.sent, vlogs))
finally:
    for _e in (_cd, _ps, _ca, _ga):
        _e["intent"] = True

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


def run_process(msg, judge_raw, owner_fn=None, write_confirm=None,
                send_ok=True):
    """驱动真实 process()。owner_fn 给 owner_command 桩（缺省用真实实现，
    执行体桩在 BASE_STUBS）；write_confirm 不为 None 时把 tool_confirm_write
    写进 reply_config（测完用 set_intent(True) 恢复成只带开关本尊）；
    send_ok=False 模拟 daemon.send 返回 ok 假（复述发送失败不落表）。"""
    dm = OwnerDaemon()
    dm.send_ok = bool(send_ok)
    if write_confirm is not None:
        write_reply_cfg({"owner_intent_enabled": True,
                         "tool_confirm_write": bool(write_confirm)})
    A.PLUGIN_HOST.daemon = dm
    del INTENT_CALLS[:], MAIN_CALLS[:], UI_CALLS[:], OWNCALLS[:]
    ALOG.clear()
    del INTENT_LOG_ALL[:]      # T10c 的泄漏检查在前面已做完，端到端重开账本
    JUDGE_RAW[0] = judge_raw
    patches = dict(E2E_FORCE)
    patches.update(MODEL_STUB)
    patches.update(BASE_STUBS)
    if owner_fn is not None:
        patches["owner_command"] = owner_fn
    exc = ""
    try:
        with patched(A, **patches):
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

# ============ T12 提示词：标注相关两句只在清单带标注时出现 ============

_p = A._intent_prompt("在吗")
_doc = K.intent_doc()
_p_marked = A._intent_prompt("在吗", doc=_doc + "\n" + K.CLOSED_NOTE)
chk("T12a 全部开放：默认文档＝hide 文档、无「暂不支持」标注、18 条全在提示词里，"
    "且默认提示词不含标注说明与「不许代替」规矩这两句",
    _doc == K.intent_doc(closed="hide")
    and _doc.count("暂不支持") == 0
    and all(c["name"] in _p for c in K.COMMANDS)
    and "有的条目行尾标着「暂不支持」" not in _p
    and "不要用清单里的其它条目代替" not in _p, "")
chk("T12b 判定规矩仍在（聊天不算指令、拿不准算不是、参数不许编）",
    "聊天、提问、发任务、发感慨" in _p and "拿不准就算不是指令" in _p
    and "主人没说的参数不要填" in _p, "")
chk("T12c 传带标注的 doc：提示词含标注说明与「不许代替」规矩两句（引用"
    " CLOSED_NOTE 常量判断）",
    "有的条目行尾标着「暂不支持」" in _p_marked
    and "不要用清单里的其它条目代替" in _p_marked
    and K.CLOSED_NOTE in _p_marked, "")

# ============ T13 端到端：write 直接执行（tool_confirm_write 关）＝同一轮两种闸 ============

set_intent(True)   # 确认开关恢复缺省（关）
with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()
dm, exc = run_process(base_msg(text="好了，恢复回复吧", id="e3"), jcmd("resume"))
joined = "".join(t for ts in dm.sent for t in ts)
chk("T13a 端到端：桩判 resume（C3 起开放）→ 真实执行（/api/pause paused=False）、"
    "回复发出、主模型桩零调用（意图命中即 return，不进工具循环、不挂第二种"
    "待人回应——同一轮两种闸）、有 HIT、无待确认",
    exc == "" and len(INTENT_CALLS) == 1 and MAIN_CALLS == []
    and [u for u in UI_CALLS if u[0] == "/api/pause"]
    == [("/api/pause", "POST", {"paused": False, "src": "remote"})]
    and "已恢复回复" in joined
    and alog_of("AGENT_INTENT_HIT name=resume risk=write conf=0.90")
    and A._TOOL_PENDING == {} and dm.acked == ["e3"],
    (exc, len(MAIN_CALLS), UI_CALLS, joined[:60], list(A._TOOL_PENDING)))

# ============ T14 write 九条各一条口语例句：tool_confirm_write 关 → 直接执行 ============

set_intent(True)   # 确保只带 owner_intent_enabled（tool_confirm_write 缺省＝关）
WRITE_HITS = [
    ("pause", "先暂停，别回复任何人", jcmd("pause"), "暂停"),
    ("resume", "好了，恢复回复吧", jcmd("resume"), "恢复"),
    ("cron_add", "每晚十点整提醒我写日报",
     jcmd("cron_add", {"time": "22:00", "text": "提醒我写日报"}),
     "定时 22:00 提醒我写日报"),
    ("cron_del", "把 3 号定时删了", jcmd("cron_del", {"id": 3}), "删定时 3"),
    ("group_add", "把 555021 这个群加进白名单",
     jcmd("group_add", {"gid": "555021"}), "加群 555021"),
    ("group_remove", "把 555022 移出白名单",
     jcmd("group_remove", {"gid": "555022"}), "移除群 555022"),
    ("feature_toggle", "把看图关掉吧",
     jcmd("feature_toggle", {"action": "关闭", "feature": "看图"}),
     "关闭 看图"),
    ("persona_switch", "切换到小雪这个人设",
     jcmd("persona_switch", {"name": "小雪"}), "切换人设 小雪"),
    ("zcode_handoff", "把这个任务转交给 ZCode：写一个爬虫脚本",
     jcmd("zcode_handoff", {"task": "写一个爬虫脚本"}),
     "转交ZCode 写一个爬虫脚本"),
]
for label, utter, raw, rendered in WRITE_HITS:
    handled, dm, own = run_intent(base_msg(text=utter, id="t14"), raw,
                                  stub_owner=True)
    hits = [l for l in ALOG if l.startswith("AGENT_INTENT_HIT")]
    cname = json.loads(raw).get("name")
    chk("T14 write 直接执行（%s）：owner 桩收到渲染文本「%s」、回复＝桩回复、"
        "确认一次、有 HIT（risk=write）、无待确认" % (label, rendered),
        handled is True and own == [rendered] and dm.sent == [["桩回复"]]
        and dm.acked == ["t14"] and len(hits) == 1
        and ("name=%s" % cname) in hits[0] and "risk=write" in hits[0]
        and A._TOOL_PENDING == {}, (handled, own, dm.sent, hits))

# ============ T15 write 置信度门槛 0.85 ============

handled, dm, own = run_intent(base_msg(text="把 555023 这个群加进白名单",
                                       id="t15a"),
                              jcmd("group_add", {"gid": "555023"}, conf=0.84),
                              stub_owner=True)
chk("T15a write 置信度 0.84 → 走聊天：False、不执行、无发送无确认",
    handled is False and own == [] and dm.sent == [] and dm.acked == []
    and len(INTENT_CALLS) == 1 and alog_of("reason=low_conf"),
    (handled, own, dm.sent, ALOG))
handled, dm, own = run_intent(base_msg(text="把 555023 这个群加进白名单",
                                       id="t15b"),
                              jcmd("group_add", {"gid": "555023"}, conf=0.85),
                              stub_owner=True)
chk("T15b write 置信度 0.85 → 通过执行",
    handled is True and own == ["加群 555023"] and dm.sent == [["桩回复"]],
    (handled, own, dm.sent))

# ============ T16 标识型参数不在原话 → 回说明、不执行、有 MISS ============

SUBSET_FAILS = [
    ("群号", "把隔壁那个群加上", jcmd("group_add", {"gid": "555033"}),
     "group_add", "param=gid", "加群 群号"),
    ("定时编号", "把那个不要的定时删了吧", jcmd("cron_del", {"id": 7}),
     "cron_del", "param=id", "删定时 编号"),
    ("人设名", "切换到那个新的人设吧", jcmd("persona_switch", {"name": "小雪"}),
     "persona_switch", "param=name", "切换人设 人设名"),
    ("任务原文", "转交给 ZCode 处理一下",
     jcmd("zcode_handoff", {"task": "整理周报数据"}),
     "zcode_handoff", "param=task", "转交ZCode 任务原文"),
]
for label, utter, raw, cname, param, usage in SUBSET_FAILS:
    handled, dm, own = run_intent(base_msg(text=utter, id="t16"), raw,
                                  stub_owner=True)
    miss = alog_of("AGENT_INTENT_MISS")
    chk("T16 标识型参数不在原话（%s）：回「没能当成指令来执行」（含「%s」）、"
        "不执行、MISS name=%s %s" % (label, usage, cname, param),
        handled is True and own == [] and len(dm.sent) == 1
        and "没能当成指令来执行" in dm.sent[0][0] and usage in dm.sent[0][0]
        and len(miss) == 1
        and miss[0].startswith("AGENT_INTENT_MISS name=%s " % cname)
        and param in miss[0] and dm.acked == ["t16"],
        (handled, own, dm.sent, miss))

# ============ T17 枚举「开 / 关」别名归一 ============

handled, dm, own = run_intent(base_msg(text="把防滥用关了吧", id="t17a"),
                              jcmd("feature_toggle",
                                   {"action": "关", "feature": "防滥用"}),
                              stub_owner=True)
chk("T17a 枚举「关」归一成「关闭」后执行：owner 桩收到「关闭 防滥用」",
    handled is True and own == ["关闭 防滥用"] and dm.sent == [["桩回复"]]
    and alog_of("AGENT_INTENT_HIT name=feature_toggle"), (handled, own, dm.sent))
handled, dm, own = run_intent(base_msg(text="把看图打开吧", id="t17b"),
                              jcmd("feature_toggle",
                                   {"action": "打开", "feature": "识图"}),
                              stub_owner=True)
chk("T17b 枚举「打开」「识图」归一后执行：owner 桩收到「开启 看图」",
    handled is True and own == ["开启 看图"] and dm.sent == [["桩回复"]],
    (handled, own, dm.sent))
handled, dm, own = run_intent(base_msg(text="开个画视频", id="t17c"),
                              jcmd("feature_toggle",
                                   {"action": "打开", "feature": "画视频"}),
                              stub_owner=True)
miss = alog_of("AGENT_INTENT_MISS")
chk("T17c 归一后仍不在选项（画视频）：回说明（含 usage）、不执行、"
    "有 MISS param=feature",
    handled is True and own == [] and len(dm.sent) == 1
    and "没能当成指令来执行" in dm.sent[0][0]
    and "开启/关闭 看图|联网搜索|防滥用" in dm.sent[0][0]
    and len(miss) == 1 and "param=feature" in miss[0],
    (handled, own, dm.sent, miss))

# ============ T18 复述确认与续跑（tool_confirm_write 开） ============

PKEY = "private:" + OWNER_QQ
REAL_OWNER = A.owner_command


def real_owner_rec(m, daemon=None):
    """透传的真实 owner_command＋记录收到的 text（复述确认续跑经它执行）。"""
    OWNCALLS.append(str(m.get("text") or ""))
    return REAL_OWNER(m, daemon)


def seed_cmd_pending(rendered="加群 555031", expires_delta=None, kind="cmd"):
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING[PKEY] = {
            "kind": kind, "cmd": "group_add", "rendered": rendered,
            "conf": 0.9, "ttl": 120.0,
            "expires": time.monotonic() + (A._TOOL_PENDING_TTL
                                           if expires_delta is None
                                           else expires_delta)}


ALOG.clear()
with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()
dm18a, exc = run_process(base_msg(text="把 555031 这个群加进白名单", id="t18a"),
                         jcmd("group_add", {"gid": "555031"}),
                         owner_fn=real_owner_rec, write_confirm=True)
ent18 = A._TOOL_PENDING.get(PKEY)
left18 = (ent18.get("expires") - time.monotonic()) if ent18 else -1
chk("T18a 复述确认开：回复是复述原文、未执行（owner 桩没收到渲染文本）、"
    "发送成功后待确认表有 kind=cmd 一格（cmd/rendered/conf、有效期≈120 秒）、"
    "HIT confirm=asked、WAIT kind=cmd、主模型桩零调用",
    exc == "" and len(dm18a.sent) == 1
    and dm18a.sent[0][0] == ("要做：加群 555031。回「确认」就执行，"
                             "回「取消」就算了（两分钟内）")
    and "加群 555031" not in OWNCALLS
    and ent18 is not None and ent18.get("kind") == "cmd"
    and ent18.get("cmd") == "group_add"
    and ent18.get("rendered") == "加群 555031"
    and ent18.get("conf") == 0.9 and 100 < left18 <= 121
    and alog_of("AGENT_INTENT_HIT name=group_add risk=write conf=0.90 "
                "confirm=asked")
    and [l for l in alog_of("AGENT_TOOL_CONFIRM_WAIT") if l.endswith("kind=cmd")]
    and MAIN_CALLS == [],
    (exc, dm18a.sent, OWNCALLS, ent18,
     alog_of("AGENT_TOOL_CONFIRM_WAIT"), MAIN_CALLS))

dm18b, exc = run_process(base_msg(text="把 555031 这个群加进白名单", id="t18b"),
                         jcmd("group_add", {"gid": "555031"}),
                         owner_fn=real_owner_rec, write_confirm=True,
                         send_ok=False)
chk("T18b 复述发送失败（ok=False）：不落表、无 WAIT（复述没送达到就不能被"
    "确认执行，I1）、确认一次",
    exc == "" and A._TOOL_PENDING == {}
    and not [l for l in alog_of("AGENT_TOOL_CONFIRM_WAIT")
             if l.endswith("kind=cmd")] and dm18b.acked == ["t18b"],
    (exc, list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM_WAIT")))

ALOG.clear()
with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()
seed_cmd_pending("加群 555031")
dm18c, exc = run_process(base_msg(text="确认", id="t18c"), "",
                         owner_fn=real_owner_rec, write_confirm=True)
joined18c = "".join(t for ts in dm18c.sent for t in ts)
chk("T18c 主人回「确认」：渲染文本被执行（owner 桩收到「加群 555031」）、"
    "真实回复发出（已加群）、CONFIRM_DONE、待确认清空、判定模型桩零调用"
    "（续跑不许被判定模型抢走）、主模型桩零调用、有 CONFIRM_GO",
    exc == "" and "加群 555031" in OWNCALLS and "已加群 555031" in joined18c
    and alog_of("AGENT_INTENT_CONFIRM_DONE name=group_add")
    and PKEY not in A._TOOL_PENDING and INTENT_CALLS == [] and MAIN_CALLS == []
    and alog_of("AGENT_TOOL_CONFIRM_GO") and dm18c.acked == ["t18c"],
    (exc, OWNCALLS, joined18c[:60], INTENT_CALLS, MAIN_CALLS,
     [l for l in ALOG if "INTENT" in l or "CONFIRM" in l]))

dm18d, exc = run_process(base_msg(text="把 555031 这个群加进白名单", id="t18d1"),
                         jcmd("group_add", {"gid": "555031"}),
                         owner_fn=real_owner_rec, write_confirm=True)
chk("T18d 前置：待确认已挂上", PKEY in A._TOOL_PENDING, list(A._TOOL_PENDING))
dm18d2, exc = run_process(base_msg(text="取消", id="t18d2"), "",
                          owner_fn=real_owner_rec, write_confirm=True)
chk("T18d 回「取消」→ 作废：待确认清空、不执行（owner 桩没收到渲染文本）、"
    "走聊天（主模型被调）、无 CONFIRM_DONE",
    PKEY not in A._TOOL_PENDING
    and "加群 555031" not in OWNCALLS and len(MAIN_CALLS) >= 1
    and alog_of("AGENT_TOOL_CONFIRM_DROP")
    and not alog_of("AGENT_INTENT_CONFIRM_DONE"),
    (OWNCALLS, len(MAIN_CALLS), list(A._TOOL_PENDING),
     alog_of("AGENT_TOOL_CONFIRM_DROP")))

dm18e, exc = run_process(base_msg(text="把 555031 这个群加进白名单", id="t18e1"),
                         jcmd("group_add", {"gid": "555031"}),
                         owner_fn=real_owner_rec, write_confirm=True)
with A._TOOL_PENDING_LOCK:
    if PKEY in A._TOOL_PENDING:
        A._TOOL_PENDING[PKEY]["expires"] = time.monotonic() - 1.0
dm18e2, exc = run_process(base_msg(text="确认", id="t18e2"), "",
                          owner_fn=real_owner_rec, write_confirm=True)
chk("T18e 超过两分钟（过期）→ 作废：EXPIRE、不执行、待确认清空、走聊天",
    PKEY not in A._TOOL_PENDING and "加群 555031" not in OWNCALLS
    and alog_of("AGENT_TOOL_CONFIRM_EXPIRE") and len(MAIN_CALLS) >= 1
    and not alog_of("AGENT_INTENT_CONFIRM_DONE"),
    (list(A._TOOL_PENDING), OWNCALLS, alog_of("AGENT_TOOL_CONFIRM_EXPIRE")))

dm18f, exc = run_process(base_msg(text="把 555031 这个群加进白名单", id="t18f1"),
                         jcmd("group_add", {"gid": "555031"}),
                         owner_fn=real_owner_rec, write_confirm=True)
snap18 = dict(A._TOOL_PENDING.get(PKEY) or {})
dm18f2, exc = run_process(base_msg(sender_id="99999", chat_id="99999",
                                   chat_name="路人", text="确认", id="t18f2"),
                          "", owner_fn=real_owner_rec, write_confirm=True)
chk("T18f 非主人在它自己的会话说「确认」：私聊待确认原样不变、不执行、"
    "无 CONFIRM 日志、该消息走聊天",
    A._TOOL_PENDING.get(PKEY) == snap18
    and "加群 555031" not in OWNCALLS and len(MAIN_CALLS) >= 1
    and not alog_of("AGENT_TOOL_CONFIRM_GO")
    and not alog_of("AGENT_INTENT_CONFIRM_DONE"),
    (list(A._TOOL_PENDING), OWNCALLS, len(MAIN_CALLS)))
dm18f3, exc = run_process(base_msg(chat_type="group", chat_id="555001",
                                   chat_name="冒烟群", sender_id=OWNER_QQ,
                                   sender_name="主人", text="确认", id="t18f3"),
                          "", owner_fn=real_owner_rec, write_confirm=True)
chk("T18f2 主人在别的会话（群）说「确认」：私聊待确认原样不变、不执行",
    A._TOOL_PENDING.get(PKEY) == snap18
    and "加群 555031" not in OWNCALLS
    and not alog_of("AGENT_INTENT_CONFIRM_DONE"),
    (list(A._TOOL_PENDING), OWNCALLS))

with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()
seed_cmd_pending("加群 555031")
handled, dm18g, own = run_intent(
    base_msg(text="把 555032 这个群加进白名单", id="t18g"),
    jcmd("group_add", {"gid": "555032"}), stub_owner=True)
ent_g = A._TOOL_PENDING.get(PKEY)
chk("T18g 已有待确认（kind=cmd）再说一条 write（_intent_command 层钉，进程级"
    "路径上这条消息会先被拦截区消费旧待确认，照 _smoke_toolloop_confirm.py "
    "C14c 手法）：回「上一条还在等确认，先回「确认」或「取消」…这条先不办："
    "加群 555032」、旧待确认原样不变、不执行、HIT confirm=blocked prev=cmd",
    handled is True and own == [] and len(dm18g.sent) == 1
    and "上一条还在等确认，先回「确认」或「取消」" in dm18g.sent[0][0]
    and "这条先不办：加群 555032" in dm18g.sent[0][0]
    and dm18g.acked == ["t18g"]
    and ent_g is not None and ent_g.get("kind") == "cmd"
    and ent_g.get("rendered") == "加群 555031"
    and alog_of("confirm=blocked prev=cmd"),
    (handled, dm18g.sent, ent_g, own))

with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()
    A._TOOL_PENDING[PKEY] = {"kind": "auth", "calls": [], "first_reply": "",
                             "done": [], "ttl": 300.0,
                             "expires": time.monotonic() + 300.0}
handled, dm18h, own = run_intent(
    base_msg(text="把 555033 这个群加进白名单", id="t18h"),
    jcmd("group_add", {"gid": "555033"}), stub_owner=True)
ent_h = A._TOOL_PENDING.get(PKEY)
chk("T18h 已有待确认是授权类（kind=auth）：不覆盖、旧的原样保留、话术分开——"
    "「还在等授权码：发 6 位授权码就执行，回「取消」就作废；这条先不办："
    "加群 555033」（主人回「确认」对授权类没用，不能让他卡在确认上）、"
    "HIT confirm=blocked prev=auth",
    handled is True and own == [] and len(dm18h.sent) == 1
    and "上一条还在等授权码" in dm18h.sent[0][0]
    and "发 6 位授权码就执行" in dm18h.sent[0][0]
    and "回「取消」就作废" in dm18h.sent[0][0]
    and "这条先不办：加群 555033" in dm18h.sent[0][0]
    and "还在等确认，先回「确认」" not in dm18h.sent[0][0]
    and ent_h is not None and ent_h.get("kind") == "auth"
    and ent_h.get("ttl") == 300.0
    and alog_of("confirm=blocked prev=auth"),
    (handled, dm18h.sent, ent_h))
with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING.clear()

# ============ T19 端到端：带合并转发内容 → 判定模型桩零调用、落回主模型 ============

set_intent(True)   # 关掉 T18 写入的 tool_confirm_write
dm19, exc = run_process(base_msg(text="转发过来的记录帮我看看",
                                 forward_lines=["小明: 你好"], id="t19"),
                        jcmd("status"))
chk("T19 端到端带合并转发内容：判定模型桩零调用、无 HIT/MISS、落回主模型",
    exc == "" and INTENT_CALLS == [] and len(MAIN_CALLS) >= 1
    and not alog_of("AGENT_INTENT_HIT") and not alog_of("AGENT_INTENT_MISS"),
    (exc, INTENT_CALLS, len(MAIN_CALLS)))

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
