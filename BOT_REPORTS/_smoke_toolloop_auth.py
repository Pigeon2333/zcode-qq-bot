# -*- coding: utf-8 -*-
"""冒烟：B2b 工具循环里危险动作的授权——被拒时自动走一次「索码」，主人发来
授权码后接着把刚才的事做完。对应施工单验收 A1–A15 与不变量 I1–I5：

  A1  模型回复里有 pc_open：预检命中 → 不进循环（模型续写零调用）、
      _devctrl_step 桩零调用；发出的确定性文本含「需要授权码」「授权后
      我会执行」和该调用说明；索码窗口已置（≈当前+300）、有
      AGENT_DEVC_ASK 与 AGENT_TOOL_AUTH_HOLD；[TOOL] 行没漏；程序只发
      这一条（I1）。
  A2  最终回复发送成功后：本会话待确认里有授权类一格（kind="auth"、
      calls=这一轮全部调用、first_reply=原始回复、done=空、
      有效期≈+300 秒）；WAIT 日志带 kind=auth。
  A3  主人接着发对的 6 位码：授权窗口打开；没有发固定的「授权成功」话术；
      _devctrl_step 桩恰好 1 次、动作 open；续跑最终回复走普通发送出口
      （[QUIET:5] 剥离且真实安静）；待确认清空；有 AGENT_TOOL_AUTH_GO。
  A4  先发错的码：拒绝话术、待确认仍在、窗口不开；再发对的 → A3 成立。
  A5  主人在待授权期间发别的话：走普通回复（主模型被调）、待确认仍在、
      危险动作没执行；A5b 发的甚至是确认词「确认」——确认不是授权（I3），
      同样普通回复、待确认仍在、不执行。
  A6  待确认已过期（时间拨过 300 秒）后才发码：认码、发固定话术、不续跑、
      不执行，过期待确认被清掉（EXPIRE）。
  A7  最终回复发送失败：不落待确认表。
  A8  授权码开关关：不预检、不索码，pc_open 直接执行（桩 1 次）。
  A9  这一轮只有 read 类调用：不预检、索码窗口不变。
  A10 同一轮先 read 再 danger：授权前什么都不执行（要码文本列出两个调用）；
      授权后续跑时 read 和 danger 都执行、回喂里两份结果都有（read 在前）、
      read 只执行一次。
  A11 续跑时 tool_confirm_write 开、待执行调用里夹着 write 工具：不重复
      要确认，write 与 danger 都直接执行（续跑语义）。
  A12 非主人发授权码：不认码、不续跑、不执行、待确认原样还在。
  A13 两个会话各有待授权：在一个会话认码只续跑那个会话的。
  A14 工具循环关着：不预检、不索码、不落待确认，pc_open 不执行（回复剥行
      照发）——旧路径行为由 _smoke_authswitch/_smoke_toolloop_devctrl 钉。
  A15 没经过预检（索码窗口没开）时，主人发 6 位数不被当成授权码：窗口不开、
      不续跑、待确认原样还在（预检是续跑能成立的前提）。
  A16 授权类待确认遇「取消 / 不做了。」→ 删掉、有 AGENT_TOOL_AUTH_CANCEL、
      照常处理；之后再发授权码只发固定话术、不续跑；遇别的话照旧保留（A5）。
  A17 设备操控开关关着、模型写了 pc_open：不预检不索码（不可用不算命中）、
      照常进循环，回喂是「没有这个工具」；不落表。
  A18 tool_confirm_write 开、同一轮 write+danger：发出的是确定性要码文本
      （同时列出 write 与 danger）、没有复述确认流程；表里授权类一格；
      回「确认」不算数；发对授权码后 write 与 danger 都执行、无任何复述。
  I1  索码预检只置窗口、不发消息——A1（程序只发模型那句）+ 预检函数源码级
      无 send；I1b 单元级：无 danger / 授权已过时不置窗口。
  I2  授权类待确认只被「正确的授权码」或过期消费——A3/A4（码）、A5/A5b
      （别的话与确认词都不丢不触发）、A6（过期才清）。
  I3  授权窗口没开时 danger 永不执行——A1（首轮拒）、A5/A5b/A12/A15（续跑
      之外的每条路都零执行）；确认和索码都不是授权（A5b、A1 只置窗口不开窗）。
  I4  工具循环关着旧授权码路径逐字一致——A14 + 既有测试回归。
  I5  续跑最终回复走普通回复的后处理与发送——A3 的 [QUIET:5] 剥离并真实
      触发安静、AGENT_DONE 带 text:1。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_confirm.py / _smoke_toolloop_devctrl.py 的做法）；模型链、
_devctrl_step、daemon 全部打桩，零联网、零真实设备动作、零真实发送。
用例之间清空 _TOOL_PENDING、_DEVCTRL_ASK、_DEVCTRL_AUTH、_DEVCTRL_CODE、
_DEVCTRL_SESSION。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_auth.py
"""
import contextlib
import copy
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_auth_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import qq_daemon as D  # noqa: E402,F401  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_A = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_bot_agent.py").read_text(
    encoding="utf-8")

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:200] if extra else ""))
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
A.out = lambda line: ALOG.append(str(line))
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None
D.out = lambda line: None

RC_PATH = os.path.join(A.DATA_DIR, "reply_config.json")
_RC_PAD = [0]


def write_reply_cfg(obj):
    """写 reply_config（追加数量递增的换行保证 mtime+size 签名必变，热加载才生效）。"""
    _RC_PAD[0] += 1
    pathlib.Path(RC_PATH).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + ("\n" * _RC_PAD[0]),
        encoding="utf-8")


def set_cfg(loop_on, confirm_write=False):
    write_reply_cfg({"tool_loop_enabled": bool(loop_on),
                     "tool_confirm_write": bool(confirm_write)})


def alog_of(tag):
    return [l for l in ALOG if tag in l]


def clear_pending():
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING.clear()


def reset_dev(code="654321"):
    """用例之间清空跨消息状态：待确认表、索码窗口、授权窗口、当前码。"""
    clear_pending()
    A._DEVCTRL_ASK["until"] = 0.0
    A._DEVCTRL_AUTH["ok_until"] = 0.0
    A._DEVCTRL_CODE.update({"code": code, "expires": time.time() + 300})
    A._DEVCTRL_SESSION.update({"msgs": [], "noticed": False})
    STEP_CALLS.clear()


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


OWNER_QQ = "10086"
GRP_ID = "555001"
NOT_OWNER = "99999"
KEY_PRIV = "private:" + OWNER_QQ
CODE = "654321"


def owner_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "private", "chat_id": OWNER_QQ,
         "chat_name": "主人", "message_id": "m1", "sender_id": OWNER_QQ,
         "sender_name": "主人", "triggered": True, "trigger_reason": "chat",
         "text": "在吗", "at_me": False, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


def non_owner_msg(**kw):
    return owner_msg(chat_type="group", chat_id=GRP_ID, chat_name="冒烟群",
                     sender_id=NOT_OWNER, sender_name="小张", at_me=True,
                     trigger_reason="at", **kw)


class RunDaemon(object):
    """send 可编排结果：默认 ok 真；send_results 逐条弹出。set_msg_reaction /
    set_quiet 记参不外发。"""

    def __init__(self, history=None, send_results=None):
        self._history = history or []
        self._send_results = list(send_results) if send_results else None
        self.acked = []
        self.sent = []
        self.reactions = []
        self.quiets = []

    def history(self, chat_type, chat_id, limit=30):
        h = self._history
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))
        if self._send_results:
            item = self._send_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"ok": True, "queued": 1}

    def set_msg_reaction(self, message_id, emoji_id, on=True):
        self.reactions.append((message_id, emoji_id, on))
        return True, "ok"

    def set_quiet(self, minutes):
        self.quiets.append(minutes)
        return {"ok": True}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


# ---- 桩：danger 工具（设备操控八个 pc_* 经 register_builtin 进表） ----

STEP_CALLS = []
STEP_TEXT = {"t": "find「测试」→ 0 条：无匹配"}
ENABLED = {"v": True}


def stub_step(daemon, reply_to, action, arg):
    """_devctrl_step 桩：记录 (reply_to, action, arg)，返回预设文本。
    零真实设备动作（不读文件、不开程序、不关进程、不发文件）。"""
    STEP_CALLS.append((tuple(reply_to), action, arg))
    return STEP_TEXT["t"]


def fake_search(q):
    return ("x 测试条目（example.com）", "bing")


TB.register_builtin(fake_search, devctrl_step_fn=stub_step,
                    devctrl_enabled_fn=lambda: ENABLED["v"])


def make_chain(outputs):
    """按剧本吐输出的模型链桩：每次调用快照 messages；空了就抛（多消费会暴露）。"""
    calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append([dict(m) for m in messages])
        item = outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return "mx", item

    return _chain, calls


def run_process(msg, outputs, dm, loop_on=True, confirm_write=False,
                twofa=True, dev_on=True):
    """走真实 process()：模型链、视觉、搜索、贴图上限全打桩。返回
    (dm, 模型调用快照)。"""
    chain, calls = make_chain(list(outputs))
    set_cfg(loop_on, confirm_write)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
                  REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
                  VCAP_FORCE=False, vision_enabled=lambda: False,
                  web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True,
                  lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=chain,
                  call_vision=lambda ms, urls, daemon=None: (_ for _ in ()).throw(
                      AssertionError("同步识图不应被调")),
                  owner_qq=lambda: OWNER_QQ,
                  cfg_2fa_on=lambda: twofa,
                  _device_ctrl_enabled=lambda: dev_on)
    with patched(A, **_force):
        reset_cap()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
    return dm, calls


def joined_of(dm):
    return "".join(t for ts in dm.sent for t in ts)


def seed_auth_pending(key, calls, done=None, first_reply="(原始回复)"):
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING[key] = {
            "kind": "auth",
            "calls": copy.deepcopy(calls),
            "first_reply": first_reply,
            "done": copy.deepcopy(done or []),
            "expires": time.monotonic() + 300.0}


BODY_OPEN = '[TOOL] pc_open {"target": "记事本"}'
ASK_FINAL = "请主人到管理台「设备操控」里取 6 位授权码发过来，我再继续。"
_N_MSG = [0]


def auth_round(dm, body=BODY_OPEN, final=ASK_FINAL, text="帮我打开记事本"):
    """跑一轮 A1：主人消息 → 模型回复带 danger 调用 → 预检索码 → 循环拒 →
    模型要码 → 发送 → 落表。返回模型调用快照。"""
    _N_MSG[0] += 1
    _, calls = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                     message_id="a%d" % _N_MSG[0], text=text),
                           [body, final], dm)
    return calls


# ============ I1b 预检函数单元级：没命中不置窗口；命中才算 ============

reset_dev()
ASK_U0 = A._DEVCTRL_ASK["until"]
_pre1 = A._devctrl_danger_precheck([{"tool": "pc_find", "args": {"query": "x"},
                                     "raw": "", "error": None}])
chk("I1b-1 只有 read 调用：预检不置索码窗口、返回 False",
    _pre1 is False and A._DEVCTRL_ASK["until"] == ASK_U0,
    (_pre1, A._DEVCTRL_ASK))
_pre2 = A._devctrl_danger_precheck([{"tool": "ghost_tool", "args": {},
                                     "raw": "", "error": None}])
chk("I1b-2 未登记的名字不算 danger：同样不置窗口",
    _pre2 is False and A._DEVCTRL_ASK["until"] == ASK_U0, (_pre2,))
with patched(A, _device_ctrl_enabled=lambda: False):
    _pre2b = A._devctrl_danger_precheck([{"tool": "pc_open",
                                          "args": {"target": "记事本"},
                                          "raw": BODY_OPEN, "error": None}])
chk("I1b-3 工具不可用（设备操控关）不算命中：不索码、照常进循环",
    _pre2b is False and A._DEVCTRL_ASK["until"] == ASK_U0,
    (_pre2b, A._DEVCTRL_ASK))
A._DEVCTRL_AUTH["ok_until"] = time.time() + 120
_pre3 = A._devctrl_danger_precheck([{"tool": "pc_open",
                                     "args": {"target": "记事本"},
                                     "raw": BODY_OPEN, "error": None}])
chk("I1b-4 授权窗口开着（auth_ok 真）：danger 也不预检、窗口不变",
    _pre3 is False and A._DEVCTRL_ASK["until"] == ASK_U0,
    (_pre3, A._DEVCTRL_ASK))
A._DEVCTRL_AUTH["ok_until"] = 0.0
with patched(A, _device_ctrl_enabled=lambda: True):
    _pre4 = A._devctrl_danger_precheck([{"tool": "pc_open",
                                         "args": {"target": "记事本"},
                                         "raw": BODY_OPEN, "error": None}])
chk("I1b-5 danger 可用且授权没过：预检命中（True）、索码窗口 ≈当前+300",
    _pre4 is True and 280 < A._DEVCTRL_ASK["until"] - time.time() <= 310,
    (_pre4, A._DEVCTRL_ASK["until"] - time.time()))
chk("I1b-6 预检打的日志是 AGENT_DEVC_ASK src=tool",
    alog_of("AGENT_DEVC_ASK")[-1:] == ["AGENT_DEVC_ASK src=tool"],
    alog_of("AGENT_DEVC_ASK"))

# ============ A16 单元级：主动作废词判定 ============

for _t, _exp in [("取消", True), ("不做了。", True), (" 算了 ", True),
                 ("取消一下", False), ("不做了吧", False), ("确认", False)]:
    chk("A16-0 作废词判定 %r → %s" % (_t, _exp),
        A._tool_auth_cancel_word(_t) == _exp, A._tool_auth_cancel_word(_t))

# ============ I1 源码级：索码只置窗口，预检不开授权窗、不发消息 ============

_ask_src = SRC_A[SRC_A.index("def _devctrl_ask_code"):
                 SRC_A.index("\ndef _devctrl_danger_precheck")]
_pre_src = SRC_A[SRC_A.index("def _devctrl_danger_precheck"):
                 SRC_A.index("\ndef _devctrl_code_accept")]
chk("I1c 源码级：_devctrl_ask_code 不发任何消息（无 send 调用）",
    ".send(" not in _ask_src and 'daemon' not in _ask_src, "")
chk("I3a 源码级：预检不碰授权窗口（_DEVCTRL_AUTH 不在预检函数里）",
    "_DEVCTRL_AUTH" not in _pre_src and "_devctrl_ask_code" in _pre_src, "")

# ============ A1+A2 预检命中不进循环：确定性要码文本、发送后落授权类 ============

reset_dev()
ALOG.clear()
dm1 = RunDaemon()
_t1 = time.time()
_calls1 = auth_round(dm1)
_left_ask = A._DEVCTRL_ASK["until"] - _t1
chk("A1a 预检命中：不进循环（模型只被主调用 1 次，无续写）",
    len(_calls1) == 1, [len(c) for c in _calls1])
chk("A1b 预检置了索码窗口（≈当前+300）、有 AGENT_DEVC_ASK src=tool",
    280 < _left_ask <= 310
    and alog_of("AGENT_DEVC_ASK")[-1:] == ["AGENT_DEVC_ASK src=tool"],
    (_left_ask, alog_of("AGENT_DEVC_ASK")))
chk("A1c/I3 danger 未执行（_devctrl_step 桩零调用）", STEP_CALLS == [],
    STEP_CALLS)
_sent1 = joined_of(dm1)
chk("A1d 发出的确定性文本含「需要授权码」「授权后我会执行」「取消」与该调用说明",
    "需要授权码" in _sent1 and "授权后我会执行" in _sent1
    and "不想做了就回「取消」" in _sent1
    and 'pc_open({"target": "记事本"})' in _sent1,
    _sent1[:220])
chk("A1e [TOOL] 行没漏进聊天", "[TOOL]" not in _sent1, _sent1[:120])
chk("A1f/I1 程序自己只发这一条（不发第二条、不发索码模板）",
    len(dm1.sent) == 1, [len(t) for t in dm1.sent])
chk("A1g 有 AGENT_TOOL_AUTH_HOLD tools=pc_open",
    alog_of("AGENT_TOOL_AUTH_HOLD") == ["AGENT_TOOL_AUTH_HOLD tools=pc_open"],
    alog_of("AGENT_TOOL_AUTH_HOLD"))
_ent1 = A._TOOL_PENDING.get(KEY_PRIV)
chk("A2a 发送成功后落授权类一格：kind=auth、calls=这一轮全部调用、"
    "first_reply=原始回复、done=空",
    _ent1 is not None and _ent1.get("kind") == "auth"
    and [c.get("tool") for c in (_ent1.get("calls") or [])] == ["pc_open"]
    and (_ent1.get("calls") or [{}])[0].get("args") == {"target": "记事本"}
    and _ent1.get("first_reply") == BODY_OPEN and _ent1.get("done") == [],
    _ent1 and {k: _ent1.get(k) for k in ("kind", "calls", "first_reply", "done")})
_left_ent = (_ent1.get("expires") - time.monotonic()) if _ent1 else -1
chk("A2b 授权类有效期 ≈+300 秒", 280 < _left_ent <= 301, _left_ent)
_w1 = alog_of("AGENT_TOOL_CONFIRM_WAIT")
chk("A2c WAIT 日志带 kind=auth、号码只留尾 4 位",
    _w1 and _w1[-1] == "AGENT_TOOL_CONFIRM_WAIT chat=private/…0086 "
    "tools=pc_open kind=auth" and "10086" not in _w1[-1], _w1)

# ============ A3(+I5) 认码 → 续跑：不发表论文术、恰好执行一次、普通出口 ============

ALOG.clear()
dm3 = RunDaemon()
_N_MSG[0] += 1
_, calls3 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                  message_id="a%d" % _N_MSG[0], text=CODE),
                        ["打开了，主人。[QUIET:5]"], dm3)
chk("A3a 授权窗口已打开", A._DEVCTRL_AUTH["ok_until"] > time.time(),
    A._DEVCTRL_AUTH["ok_until"] - time.time())
chk("A3b 没有发固定的「授权成功」话术", "连线成功" not in joined_of(dm3),
    joined_of(dm3)[:120])
chk("A3c _devctrl_step 桩恰好 1 次、动作 open、reply_to=主人私聊",
    STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")], STEP_CALLS)
chk("A3d 续跑的最终回复被发送", "打开了，主人。" in joined_of(dm3),
    joined_of(dm3)[:120])
chk("A3e 待确认已清空", KEY_PRIV not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))
chk("A3f 有 AGENT_TOOL_AUTH_GO（同等的续跑日志，带 tools=pc_open）",
    any("AGENT_TOOL_AUTH_GO chat=private/…0086 tools=pc_open" in l
        for l in alog_of("AGENT_TOOL_AUTH_GO")),
    alog_of("AGENT_TOOL_AUTH_GO"))
chk("A3g 续跑循环日志：actions=pc_open:成功",
    any("AGENT_TOOL_LOOP" in l and "actions=pc_open:成功" in l
        for l in alog_of("AGENT_TOOL_LOOP")), alog_of("AGENT_TOOL_LOOP"))
chk("A3h/I5 续跑模型只被调 1 次（主模型没调）", len(calls3) == 1,
    [len(c) for c in calls3])
chk("I5a 续跑回复里的 [QUIET:5] 被普通后处理剥离",
    "[QUIET" not in joined_of(dm3), joined_of(dm3)[:120])
chk("I5b [QUIET:5] 真实触发安静（daemon.set_quiet 收到 5）",
    dm3.quiets == [5], dm3.quiets)
chk("I5c 走普通发送出口（AGENT_DONE 带 text:1）",
    any("AGENT_DONE" in l and "text:1" in l for l in alog_of("AGENT_DONE")),
    alog_of("AGENT_DONE"))

# ============ A4 错码 → 拒、待确认仍在；再发对的 → A3 成立 ============

reset_dev()
ALOG.clear()
dm4 = RunDaemon()
auth_round(dm4)
_N_MSG[0] += 1
_, calls4a = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text="000000"),
                         [], dm4)
_ent4 = A._TOOL_PENDING.get(KEY_PRIV)
chk("A4a 错码：拒绝话术（授权码有误）", "授权码有误" in joined_of(dm4),
    joined_of(dm4)[:120])
chk("A4b 错码：待确认仍在（calls 原样）", _ent4 is not None
    and _ent4.get("kind") == "auth"
    and [c.get("tool") for c in (_ent4.get("calls") or [])] == ["pc_open"],
    _ent4 and _ent4.get("calls"))
chk("A4c 错码：窗口不开、不执行、不走模型",
    A._DEVCTRL_AUTH["ok_until"] == 0.0 and STEP_CALLS == [] and calls4a == [],
    (A._DEVCTRL_AUTH["ok_until"], STEP_CALLS, len(calls4a)))
ALOG.clear()
dm4b = RunDaemon()
_N_MSG[0] += 1
_, calls4b = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text=CODE),
                         ["打开了。"], dm4b)
chk("A4d 再发对的码：A3 成立（执行 1 次 open、待确认清空、无论文术）",
    STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")]
    and KEY_PRIV not in A._TOOL_PENDING
    and "连线成功" not in joined_of(dm4b)
    and A._DEVCTRL_AUTH["ok_until"] > time.time(),
    (STEP_CALLS, list(A._TOOL_PENDING), A._DEVCTRL_AUTH["ok_until"]))

# ============ A5/A5b 待授权期间发别的话 / 确认词：普通回复、待确认仍在 ============

reset_dev()
ALOG.clear()
dm5 = RunDaemon()
auth_round(dm5)
_N_MSG[0] += 1
_, calls5 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                  message_id="a%d" % _N_MSG[0],
                                  text="今天天气怎么样"),
                        ["在呢。"], dm5)
_ent5 = A._TOOL_PENDING.get(KEY_PRIV)
chk("A5a 别的话：走普通回复（主模型被调 1 次、回复发出）",
    len(calls5) == 1 and "在呢。" in joined_of(dm5),
    (len(calls5), joined_of(dm5)[:80]))
chk("A5b/I2 别的话：待确认仍在、危险动作没执行",
    _ent5 is not None and _ent5.get("kind") == "auth" and STEP_CALLS == [],
    (_ent5 and _ent5.get("kind"), STEP_CALLS))
ALOG.clear()
dm5b = RunDaemon()
_N_MSG[0] += 1
_, calls5b = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text="确认"),
                         ["嗯？确认什么？"], dm5b)
_ent5b = A._TOOL_PENDING.get(KEY_PRIV)
chk("A5c/I3 确认词对授权类不算数：不续跑、走普通回复、待确认仍在、不执行",
    len(calls5b) == 1 and _ent5b is not None
    and _ent5b.get("kind") == "auth" and STEP_CALLS == []
    and "嗯？确认什么？" in joined_of(dm5b),
    (len(calls5b), _ent5b and _ent5b.get("kind"), STEP_CALLS))

# ============ A6 过期后才发码：认码、发固定话术、不续跑 ============

reset_dev()
ALOG.clear()
dm6 = RunDaemon()
auth_round(dm6)
with A._TOOL_PENDING_LOCK:
    A._TOOL_PENDING[KEY_PRIV]["expires"] = time.monotonic() - 1.0
_N_MSG[0] += 1
_, calls6 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                  message_id="a%d" % _N_MSG[0], text=CODE),
                        [], dm6)
chk("A6a 过期后发码：EXPIRE 日志、认码发固定话术（连线成功）",
    bool(alog_of("AGENT_TOOL_CONFIRM_EXPIRE"))
    and "连线成功" in joined_of(dm6),
    (alog_of("AGENT_TOOL_CONFIRM_EXPIRE"), joined_of(dm6)[:100]))
chk("A6b 不续跑：危险动作没执行、无 AUTH_GO、不走模型",
    STEP_CALLS == [] and not alog_of("AGENT_TOOL_AUTH_GO") and calls6 == [],
    (STEP_CALLS, len(calls6)))
chk("A6c 过期待确认已被清掉", KEY_PRIV not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))

# ============ A7 发送失败不落表 ============

reset_dev()
ALOG.clear()
dm7 = RunDaemon(send_results=[{"ok": False, "error": "限流"}])
auth_round(dm7)
chk("A7 发送失败：不落待确认表、无 WAIT 日志",
    A._TOOL_PENDING == {} and not alog_of("AGENT_TOOL_CONFIRM_WAIT"),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM_WAIT")))

# ============ A8 授权码开关关：不预检、直接执行 ============

reset_dev()
ALOG.clear()
dm8 = RunDaemon()
_N_MSG[0] += 1
_, calls8 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                  message_id="a%d" % _N_MSG[0],
                                  text="帮我打开记事本"),
                        [BODY_OPEN, "打开了。"], dm8, twofa=False)
chk("A8a 授权码开关关：不预检不索码（窗口不变、无 DEVC_ASK）",
    A._DEVCTRL_ASK["until"] == 0.0 and not alog_of("AGENT_DEVC_ASK"),
    (A._DEVCTRL_ASK["until"], alog_of("AGENT_DEVC_ASK")))
chk("A8b pc_open 直接执行（桩 1 次）",
    STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")], STEP_CALLS)
chk("A8c 不产生待确认", A._TOOL_PENDING == {}, list(A._TOOL_PENDING))

# ============ A9 只有 read 调用：不预检、窗口不变 ============

reset_dev()
ALOG.clear()
dm9 = RunDaemon()
_N_MSG[0] += 1
_, _ = run_process(owner_msg(id="a%d" % _N_MSG[0],
                             message_id="a%d" % _N_MSG[0], text="找一下报告"),
                   ['[TOOL] pc_find {"query": "报告"}', "找到了 1 份。"], dm9)
chk("A9 只有 read：不预检（窗口不变、无 DEVC_ASK）、工具照常执行、不落表",
    A._DEVCTRL_ASK["until"] == 0.0 and not alog_of("AGENT_DEVC_ASK")
    and STEP_CALLS == [(("private", OWNER_QQ), "find", "报告")]
    and A._TOOL_PENDING == {},
    (A._DEVCTRL_ASK["until"], STEP_CALLS, list(A._TOOL_PENDING)))

# ============ A10 同轮 read+danger：授权前不执行，授权后一起续跑 ============

reset_dev()
ALOG.clear()
dm10 = RunDaemon()
BODY10 = '[TOOL] pc_find {"query": "报告"}\n' + BODY_OPEN
auth_round(dm10, body=BODY10, text="找一下报告再打开记事本")
_ent10 = A._TOOL_PENDING.get(KEY_PRIV)
chk("A10a 授权前什么都不执行（read 也不跑）", STEP_CALLS == [], STEP_CALLS)
chk("A10b 要码文本同时列出 read 与 danger 两个调用",
    dm10.sent and "- pc_find(" in joined_of(dm10)
    and "- pc_open(" in joined_of(dm10), joined_of(dm10)[:200])
chk("A10c 待授权：calls=这一轮全部调用（find+open，不只 danger）、done=空",
    _ent10 is not None
    and [c.get("tool") for c in (_ent10.get("calls") or [])]
    == ["pc_find", "pc_open"] and _ent10.get("done") == [],
    _ent10 and _ent10.get("calls"))
ALOG.clear()
dm10b = RunDaemon()
_N_MSG[0] += 1
_, calls10b = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                    message_id="a%d" % _N_MSG[0], text=CODE),
                          ["都办好了。"], dm10b)
_fb10 = calls10b[0][-1]["content"] if calls10b and calls10b[0] else ""
chk("A10d 续跑第一轮回喂同时有 read 与 open 的结果，read 在前",
    "- pc_find(" in _fb10 and "- pc_open(" in _fb10
    and _fb10.index("- pc_find(") < _fb10.index("- pc_open("), _fb10[:300])
chk("A10e 授权后两个都执行、read 只执行一次（桩合计 find 1 + open 1）",
    [c[1] for c in STEP_CALLS] == ["find", "open"], STEP_CALLS)
chk("A10f 待确认已清空、授权窗口已开",
    KEY_PRIV not in A._TOOL_PENDING
    and A._DEVCTRL_AUTH["ok_until"] > time.time(),
    (list(A._TOOL_PENDING),))

# ============ A11 续跑夹 write：不再重复要确认，直接执行 ============

reset_dev()
ALOG.clear()
dm11 = RunDaemon()
seed_auth_pending(KEY_PRIV, [
    {"tool": "react", "args": {"emoji_id": "129"}, "raw": "", "error": None},
    {"tool": "pc_open", "args": {"target": "记事本"},
     "raw": BODY_OPEN, "error": None}])
# 真实世界里授权类待确认存在时索码窗口必然开着（预检置的）——补上窗口，
# 否则这条码消息按 A15 的语义走普通聊天。
A._DEVCTRL_ASK["until"] = time.time() + 300
_N_MSG[0] += 1
_, calls11 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text=CODE),
                         ["都办好了。"], dm11, confirm_write=True)
chk("A11a write 工具不再重复要确认（表情直接挂上）、danger 也执行",
    dm11.reactions == [("a%d" % _N_MSG[0], "129", True)]
    and STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")],
    (dm11.reactions, STEP_CALLS))
chk("A11b 没有复述确认文案发出", "回复「确认」我就执行" not in joined_of(dm11),
    joined_of(dm11)[:120])
chk("A11c 走的是续跑（模型只被调 1 次）", len(calls11) == 1,
    [len(c) for c in calls11])

# ============ A12 非主人发授权码：什么都不发生 ============

reset_dev()
ALOG.clear()
dm12 = RunDaemon()
auth_round(dm12)
ALOG.clear()
dm12b = RunDaemon()
_N_MSG[0] += 1
_, calls12 = run_process(non_owner_msg(id="a%d" % _N_MSG[0],
                                       message_id="a%d" % _N_MSG[0],
                                       text=CODE),
                         ["嗯嗯。"], dm12b)
chk("A12a 非主人发码：不认码（窗口不开）、不执行",
    A._DEVCTRL_AUTH["ok_until"] == 0.0 and STEP_CALLS == [],
    (A._DEVCTRL_AUTH["ok_until"], STEP_CALLS))
chk("A12b 待确认原样还在、消息照常回复",
    KEY_PRIV in A._TOOL_PENDING
    and A._TOOL_PENDING[KEY_PRIV].get("kind") == "auth"
    and len(calls12) == 1 and "嗯嗯。" in joined_of(dm12b),
    (list(A._TOOL_PENDING), len(calls12)))

# ============ A13 两个会话各自待授权：认码只续跑本会话的 ============

reset_dev()
ALOG.clear()
KEY_GRP2 = "group:555002"
dm13 = RunDaemon()
auth_round(dm13)
seed_auth_pending(KEY_GRP2, [{"tool": "pc_close", "args": {"name": "qq"},
                              "raw": "", "error": None}])
ALOG.clear()
_N_MSG[0] += 1
_, calls13 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text=CODE),
                         ["打开了。"], dm13)
chk("A13a 私聊认码：只执行私聊那条（open），群的 close 不动",
    STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")], STEP_CALLS)
chk("A13b 私聊待确认已消费、群会话的待授权原样还在",
    KEY_PRIV not in A._TOOL_PENDING
    and KEY_GRP2 in A._TOOL_PENDING
    and A._TOOL_PENDING[KEY_GRP2].get("kind") == "auth"
    and [c.get("tool") for c in A._TOOL_PENDING[KEY_GRP2]["calls"]]
    == ["pc_close"],
    (list(A._TOOL_PENDING),))

# ============ A14 工具循环关：不预检、不落表、不执行（旧路径照旧） ============

reset_dev()
ALOG.clear()
dm14 = RunDaemon()
_N_MSG[0] += 1
_, calls14 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text="在吗"),
                         ["我先看看\n" + BODY_OPEN], dm14, loop_on=False)
chk("A14a 循环关：不预检不索码（窗口不变、无 DEVC_ASK）",
    A._DEVCTRL_ASK["until"] == 0.0 and not alog_of("AGENT_DEVC_ASK"),
    (A._DEVCTRL_ASK["until"], alog_of("AGENT_DEVC_ASK")))
chk("A14b pc_open 不执行、不落待确认",
    STEP_CALLS == [] and A._TOOL_PENDING == {},
    (STEP_CALLS, list(A._TOOL_PENDING)))
chk("A14c 剥行后的回复照发（[TOOL] 不漏进聊天）",
    "我先看看" in joined_of(dm14) and "[TOOL]" not in joined_of(dm14),
    joined_of(dm14)[:120])

# ============ A15 没经过预检：6 位数不被当成授权码 ============

reset_dev()
ALOG.clear()
dm15 = RunDaemon()
seed_auth_pending(KEY_PRIV, [{"tool": "pc_open", "args": {"target": "记事本"},
                              "raw": BODY_OPEN, "error": None}])
_N_MSG[0] += 1
_, calls15 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text=CODE),
                         ["你说的是 " + CODE + " 这个数吗？"], dm15)
chk("A15a 索码窗口没开：发码不被认（授权窗口不开、不执行）",
    A._DEVCTRL_AUTH["ok_until"] == 0.0 and STEP_CALLS == [],
    (A._DEVCTRL_AUTH["ok_until"], STEP_CALLS))
chk("A15b 待确认原样还在（没被这条消息消费）、按普通聊天回复",
    KEY_PRIV in A._TOOL_PENDING
    and A._TOOL_PENDING[KEY_PRIV].get("kind") == "auth"
    and len(calls15) == 1, (list(A._TOOL_PENDING), len(calls15)))

# ============ A16 主动作废：取消 / 不做了 ============

reset_dev()
ALOG.clear()
dm16 = RunDaemon()
auth_round(dm16)
_N_MSG[0] += 1
_, calls16 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0], text="取消"),
                         ["好，不做了。"], dm16)
chk("A16a 「取消」：表已删、有 AGENT_TOOL_AUTH_CANCEL、这条照常回复",
    KEY_PRIV not in A._TOOL_PENDING
    and bool(alog_of("AGENT_TOOL_AUTH_CANCEL"))
    and len(calls16) == 1 and "不做了" in joined_of(dm16),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_AUTH_CANCEL"), len(calls16)))
_N_MSG[0] += 1
_, calls16b = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                    message_id="a%d" % _N_MSG[0], text=CODE),
                          [], dm16)
chk("A16b 取消后再发授权码：认码发固定话术、不续跑、不执行、无 AUTH_GO",
    "连线成功" in joined_of(dm16) and STEP_CALLS == []
    and calls16b == [] and not alog_of("AGENT_TOOL_AUTH_GO"),
    (joined_of(dm16)[-100:], STEP_CALLS, len(calls16b)))

# ============ A17 设备操控关：pc_open 不算命中，照常进循环 ============

reset_dev()
ALOG.clear()
dm17 = RunDaemon()
_N_MSG[0] += 1
_, calls17 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0],
                                   text="帮我打开记事本"),
                         [BODY_OPEN, "这台设备的电脑操控没开，做不了。"],
                         dm17, dev_on=False)
_fb17 = calls17[1][-1]["content"] if len(calls17) > 1 else ""
chk("A17a 设备操控关：不预检不索码（窗口不变、无 DEVC_ASK、无 HOLD）",
    A._DEVCTRL_ASK["until"] == 0.0 and not alog_of("AGENT_DEVC_ASK")
    and not alog_of("AGENT_TOOL_AUTH_HOLD"),
    (A._DEVCTRL_ASK["until"], alog_of("AGENT_DEVC_ASK")))
chk("A17b 照常进循环：回喂是「没有这个工具」、桩零调用、不落表",
    "没有这个工具" in _fb17 and STEP_CALLS == []
    and A._TOOL_PENDING == {},
    (_fb17[:160], STEP_CALLS, list(A._TOOL_PENDING)))

# ============ A18 确认开关开 + 同轮 write+danger：要码文本、无复述 ============

reset_dev()
ALOG.clear()
dm18 = RunDaemon()
BODY18 = '[TOOL] react {"emoji_id": "129"}\n' + BODY_OPEN
_N_MSG[0] += 1
_, calls18 = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                   message_id="a%d" % _N_MSG[0],
                                   text="给我这条点赞再打开记事本"),
                         [BODY18], dm18, confirm_write=True)
_ent18 = A._TOOL_PENDING.get(KEY_PRIV)
_j18 = joined_of(dm18)
chk("A18a 发出的是确定性要码文本：同时列出 write 与 danger、没有复述文案",
    "授权后我会执行" in _j18 and "react(" in _j18 and "pc_open(" in _j18
    and "回复「确认」我就执行" not in _j18 and "[TOOL]" not in _j18,
    _j18[:220])
chk("A18b write 未被执行、模型无续写（不进循环）",
    dm18.reactions == [] and len(calls18) == 1,
    (dm18.reactions, [len(c) for c in calls18]))
chk("A18c 表里是授权类一格：calls=两个调用",
    _ent18 is not None and _ent18.get("kind") == "auth"
    and [c.get("tool") for c in (_ent18.get("calls") or [])]
    == ["react", "pc_open"], _ent18 and _ent18.get("calls"))
ALOG.clear()
dm18b = RunDaemon()
_N_MSG[0] += 1
_, calls18b = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                    message_id="a%d" % _N_MSG[0], text="确认"),
                          ["嗯？"], dm18b, confirm_write=True)
chk("A18d 回「确认」不算数：照常处理、表仍在、write 仍未执行",
    len(calls18b) == 1 and KEY_PRIV in A._TOOL_PENDING
    and dm18b.reactions == [],
    (len(calls18b), list(A._TOOL_PENDING), dm18b.reactions))
ALOG.clear()
dm18c = RunDaemon()
_N_MSG[0] += 1
_, calls18c = run_process(owner_msg(id="a%d" % _N_MSG[0],
                                    message_id="a%d" % _N_MSG[0], text=CODE),
                          ["都办好了。"], dm18c, confirm_write=True)
chk("A18e 发对码：write 与 danger 都执行、无任何复述、表已清空",
    dm18c.reactions == [("a%d" % _N_MSG[0], "129", True)]
    and STEP_CALLS == [(("private", OWNER_QQ), "open", "记事本")]
    and "回复「确认」我就执行" not in joined_of(dm18c)
    and KEY_PRIV not in A._TOOL_PENDING,
    (dm18c.reactions, STEP_CALLS, list(A._TOOL_PENDING)))

# ============ 收尾：不许有残留进程状态 ============

chk("Z 收尾：待确认表已清空（内存态，不落盘）", clear_pending() is None
    and A._TOOL_PENDING == {}, list(A._TOOL_PENDING))

print()
print("通过 %d 项；失败 %d 项%s" % (n_pass, len(fails), (": %s" % fails) if fails else ""))
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
