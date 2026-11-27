# -*- coding: utf-8 -*-
"""冒烟：WP-07 A2b-2 复述确认的跨消息续跑——write 动作停下复述、发送成功才
落待确认表、主人同会话回确认词才续跑、别的消息/过期/非主人一律不触发
（对应派单验收 C1–C15 与不变量 I1–I5）。

  C1  确认开关开、主人、write 工具：复述被发送；发送成功后 _TOOL_PENDING
      该会话有一格（calls=循环 pending、done=停下前已执行的动作、
      first_reply=带工具行的原回复、expires≈120 秒）；有
      AGENT_TOOL_CONFIRM_WAIT，号码只留尾 4 位。
  C2  主人接着回「确认」：write 工具恰好执行 1 次；模型为续写被调 1 次
      （主模型没调）；最终回复被发送；_TOOL_PENDING 该会话已清空；有
      AGENT_TOOL_CONFIRM_GO。
  C3  主人回的是别的话：DROP、待确认丢弃、工具不执行、走普通回复（主模型被调）。
  C4  已过期再来「确认」：EXPIRE、工具不执行、照常处理（主模型被调）。
  C5  复述发送失败（send 返回 ok 假 / send 抛异常两种）：都不落表。
  C6  非主人发「确认」：不读不删待确认、不执行、无 CONFIRM 日志。
  C7  两个会话各有待确认：在一个会话确认只执行那个会话的，另一个仍在。
  C8  停下之前已执行过一个 read 工具：续跑第一轮回喂同时含 read 与 write
      两个工具的结果（read 在前）；read 不被执行第二遍。
  C9  确认开关关：write 直接执行，不产生待确认（与从前一致）。
  C10 续跑时闸照过：没登记的名字、参数不对、danger 且无授权——都不执行、
      只在回喂里说明。
  C11 确认词边界：「确认。」「 好 」「OK」算；「确认一下 details」「不确认」不算。
  C12 续跑的消息不再次进衔接点的工具行收刮（_tool_resumed 标记）：tool_loop
      只被调 1 次，final 原样往后走。
  C13 没有待确认时拦截处零副作用：_TOOL_PENDING 不变、无 CONFIRM 日志。
  C14 两个会话各自复述各自落表互不覆盖；一条发送失败不影响另一条；同会话
      新复述覆盖旧的并打 AGENT_TOOL_CONFIRM_REPLACE（I2）。
  C15 管理台卡片说明不再有「不会自动续跑」，换成「回复「确认」后执行」。
  I5  续跑最终回复走普通后处理：[QUIET:5] 被剥离且真实触发安静。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_run.py 的做法）；模型链、search_web、daemon 全部打桩，
零联网、零真实发送。每个用例之间把 _TOOL_PENDING 清空。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_confirm.py
"""
import contextlib
import copy
import io
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_confirm_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402
import qq_admin_ui as U  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

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
KEY_GRP = "group:" + GRP_ID


def base_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "group", "chat_id": GRP_ID,
         "chat_name": "冒烟群", "message_id": "m1", "sender_id": NOT_OWNER,
         "sender_name": "小张", "triggered": True, "trigger_reason": "at",
         "text": "在吗", "at_me": True, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


def owner_priv(**kw):
    return base_msg(chat_type="private", chat_id=OWNER_QQ, chat_name="主人",
                    sender_id=OWNER_QQ, sender_name="主人", at_me=False,
                    trigger_reason="chat", **kw)


def owner_grp(**kw):
    return base_msg(sender_id=OWNER_QQ, sender_name="主人", **kw)


class RunDaemon(object):
    """send 可编排结果：默认 ok 真；send_results 逐条弹出（dict=原样返回、
    Exception=抛出）。set_msg_reaction / set_quiet 记参不外发。"""

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


# ---- 桩：搜索与 danger 工具 ----

SEARCH_CALLS = []


def fake_search(q):
    SEARCH_CALLS.append(q)
    return ("- %s 是测试条目（example.com）" % q, "bing")


TB.register_builtin(fake_search)

DANGER_EXEC = []
if T.get_tool("smoke_danger") is None:
    T.register({
        "name": "smoke_danger",
        "desc": "冒烟用 danger 级工具（要授权码）",
        "params": [{"name": "cmd", "type": "str", "required": True,
                    "desc": "命令"}],
        "risk": "danger", "scope": "owner",
        "returns": "执行结果",
        "run": lambda d, m, a: (DANGER_EXEC.append(a), "做了")[1],
    })


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


def run_process(msg, outputs, dm, v2=False, loop_on=True, confirm_write=False,
                log_snapshot=False):
    """走真实 process()：模型链、视觉、搜索、贴图上限全打桩。
    log_snapshot=True 时返回 (dm, 调用快照, 本次新增日志)。"""
    chain, calls = make_chain(list(outputs))
    set_cfg(loop_on, confirm_write)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=v2, REPLY_JSON_FORCE=False,
                  REACT_ONLY_FORCE=False, VCAP_FORCE=False,
                  vision_enabled=lambda: False, web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True, lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=chain,
                  call_vision=lambda ms, urls, daemon=None: (_ for _ in ()).throw(
                      AssertionError("同步识图不应被调")),
                  owner_qq=lambda: OWNER_QQ)
    mark = len(ALOG)
    with patched(A, **_force):
        reset_cap()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
    newlog = ALOG[mark:] if log_snapshot else None
    return dm, calls, newlog


def joined_of(dm):
    return "".join(t for ts in dm.sent for t in ts)


def seed_pending(key, calls, done=None, first_reply="(原始回复)", expires_delta=None):
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING[key] = {
            "calls": copy.deepcopy(calls), "first_reply": first_reply,
            "done": copy.deepcopy(done or []),
            "expires": time.monotonic() + (A._TOOL_PENDING_TTL
                                           if expires_delta is None
                                           else expires_delta)}


BODY_REACT = '[TOOL] react {"emoji_id": "129"}'

# ============ C1 复述被发送、发送成功后落表（I1 正面） ============

clear_pending()
SEARCH_CALLS.clear()
dm1 = RunDaemon()
_m1 = owner_priv(id="c1a", message_id="c1a", text="给这条消息点个赞")
_, _, _ = run_process(_m1, [BODY_REACT], dm1, confirm_write=True)
_ent = A._TOOL_PENDING.get(KEY_PRIV)
_wait_lines = [l for l in alog_of("AGENT_TOOL_CONFIRM_WAIT")]
chk("C1a confirm 开：write 工具未执行", dm1.reactions == [], dm1.reactions)
chk("C1b 复述被发送（回复「确认」我就执行）",
    dm1.sent and "回复「确认」我就执行" in joined_of(dm1), joined_of(dm1)[:120])
chk("C1c 发送成功后落表：calls=循环 pending、done=已执行动作、first_reply=原回复",
    _ent is not None and len(_ent.get("calls") or []) == 1
    and _ent["calls"][0].get("tool") == "react"
    and _ent["calls"][0].get("args") == {"emoji_id": "129"}
    and _ent.get("done") == []
    and _ent.get("first_reply") == BODY_REACT,
    _ent and {k: _ent[k] for k in ("calls", "done", "first_reply")})
chk("C1d 有 AGENT_TOOL_CONFIRM_WAIT：chat=private/…0086 tools=react",
    _wait_lines and _wait_lines[-1] == "AGENT_TOOL_CONFIRM_WAIT "
    "chat=private/…0086 tools=react", _wait_lines)
chk("C1e WAIT 日志里号码只留尾 4 位（无完整号码）",
    bool(_wait_lines) and "10086" not in _wait_lines[-1], _wait_lines)
_left = (_ent.get("expires") - time.monotonic()) if _ent else -1
chk("C1f 有效期≈120 秒", 100 < _left <= 121, _left)

# ============ C2 主人回「确认」→ 续跑执行（I4 的一部分：权限内照做） ============

ALOG.clear()
dm1.reactions.clear()
dm1.sent.clear()
_m2 = owner_priv(id="c1b", message_id="c1b", text="确认")
_, calls2, _ = run_process(_m2, ["好了，挂上了。"], dm1, confirm_write=True)
chk("C2a write 工具被执行恰好 1 次（挂在续跑这条消息上）",
    dm1.reactions == [("c1b", "129", True)], dm1.reactions)
chk("C2b 模型为续写被调 1 次（主模型未调）", len(calls2) == 1,
    [len(c) for c in calls2])
chk("C2c 最终回复被发送", dm1.sent and "好了，挂上了。" in joined_of(dm1),
    joined_of(dm1)[:120])
chk("C2d _TOOL_PENDING 该会话已清空", KEY_PRIV not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))
chk("C2e 有 AGENT_TOOL_CONFIRM_GO",
    any("AGENT_TOOL_CONFIRM_GO chat=private/…0086 tools=react" in l
        for l in alog_of("AGENT_TOOL_CONFIRM_GO")),
    alog_of("AGENT_TOOL_CONFIRM_GO"))
chk("C2f 走普通发送出口（AGENT_DONE 带 text:1）",
    any("AGENT_DONE" in l and "text:1" in l for l in alog_of("AGENT_DONE")),
    alog_of("AGENT_DONE"))

# ============ C3 别的话 → DROP、走普通回复 ============

clear_pending()
seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}])
dm3 = RunDaemon()
_m3 = owner_priv(id="c3", message_id="c3", text="在吗")
_, calls3, _ = run_process(_m3, ["在呢。"], dm3, confirm_write=True)
chk("C3a 别的话：DROP 日志、工具未执行",
    alog_of("AGENT_TOOL_CONFIRM_DROP") and dm3.reactions == [],
    (alog_of("AGENT_TOOL_CONFIRM_DROP"), dm3.reactions))
chk("C3b 待确认已丢弃（该会话清空）", KEY_PRIV not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))
chk("C3c 这条消息走普通回复（主模型被调 1 次、回复发出）",
    len(calls3) == 1 and dm3.sent and "在呢。" in joined_of(dm3),
    (len(calls3), joined_of(dm3)[:80]))

# ============ C4 过期 → EXPIRE、照常处理 ============

clear_pending()
seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}], expires_delta=-1.0)
dm4 = RunDaemon()
_m4 = owner_priv(id="c4", message_id="c4", text="确认")
_, calls4, _ = run_process(_m4, ["刚看到，在的。"], dm4, confirm_write=True)
chk("C4a 过期再来「确认」：EXPIRE 日志、工具不执行",
    alog_of("AGENT_TOOL_CONFIRM_EXPIRE") and dm4.reactions == [],
    (alog_of("AGENT_TOOL_CONFIRM_EXPIRE"), dm4.reactions))
chk("C4b 过期待确认已作废（该会话清空）", KEY_PRIV not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))
chk("C4c 这条消息照常处理（主模型被调、回复发出）",
    len(calls4) == 1 and dm4.sent and "刚看到，在的。" in joined_of(dm4),
    (len(calls4), joined_of(dm4)[:80]))

# ============ C5 复述发送失败 → 不落表（I1） ============

clear_pending()
dm5a = RunDaemon(send_results=[{"ok": False, "error": "限流"}])
_m5a = owner_priv(id="c5a", message_id="c5a", text="给这条消息点个赞")
run_process(_m5a, [BODY_REACT], dm5a, confirm_write=True)
chk("C5a send 返回 ok 假：复述没发成、不落表、无 WAIT",
    A._TOOL_PENDING == {} and not alog_of("AGENT_TOOL_CONFIRM_WAIT"),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM_WAIT")))
clear_pending()
dm5b = RunDaemon(send_results=[RuntimeError("daemon 连不上")])
_m5b = owner_priv(id="c5b", message_id="c5b", text="给这条消息点个赞")
run_process(_m5b, [BODY_REACT], dm5b, confirm_write=True)
chk("C5b send 抛异常：同样不落表、不炸 process",
    A._TOOL_PENDING == {} and "EXC" not in joined_of(dm5b),
    (list(A._TOOL_PENDING), joined_of(dm5b)[:80]))

# ============ C6 非主人发「确认」：不读不删不执行（I3） ============

clear_pending()
ALOG.clear()
SEEDED = [{"tool": "react", "args": {"emoji_id": "777"}, "raw": "", "error": None}]
seed_pending(KEY_GRP, SEEDED)
dm6 = RunDaemon()
_m6 = base_msg(id="c6", message_id="c6", text="确认")
_, calls6, _ = run_process(_m6, ["嗯嗯。"], dm6, confirm_write=True)
_ent6 = A._TOOL_PENDING.get(KEY_GRP)
chk("C6a 非主人「确认」：待确认原样还在（不读不删）",
    _ent6 is not None and _ent6.get("calls") == SEEDED,
    _ent6 and _ent6.get("calls"))
chk("C6b 工具未执行、无任何 CONFIRM 日志",
    dm6.reactions == [] and not alog_of("AGENT_TOOL_CONFIRM"),
    (dm6.reactions, alog_of("AGENT_TOOL_CONFIRM")))
chk("C6c 消息照常处理", len(calls6) == 1 and dm6.sent, len(calls6))

# ============ C7 两个会话各有待确认：确认只执行本会话的（I2/I3） ============

clear_pending()
ALOG.clear()
seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}])
seed_pending(KEY_GRP, [{"tool": "react", "args": {"emoji_id": "131"},
                        "raw": "", "error": None}])
dm7 = RunDaemon()
_m7 = owner_grp(id="c7", message_id="c7", text="确认")
_, _, _ = run_process(_m7, ["办好了。"], dm7, confirm_write=True)
chk("C7a 群会话确认：执行的是群那条待确认（表情 131、挂在本条消息）",
    dm7.reactions == [("c7", "131", True)], dm7.reactions)
chk("C7b 私聊会话的待确认原样仍在",
    A._TOOL_PENDING.get(KEY_PRIV, {}).get("calls")
    == [{"tool": "react", "args": {"emoji_id": "129"}, "raw": "", "error": None}],
    A._TOOL_PENDING.get(KEY_PRIV))
chk("C7c 群会话待确认已消费、GO 日志带群号尾 4 位",
    KEY_GRP not in A._TOOL_PENDING
    and any("AGENT_TOOL_CONFIRM_GO chat=group/…5001" in l
            for l in alog_of("AGENT_TOOL_CONFIRM_GO")),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM_GO")))

# ============ C8 停下前已执行过一个 read：续跑回喂带两份结果 ============

clear_pending()
SEARCH_CALLS.clear()
dm8 = RunDaemon()
BODY_C8 = '[TOOL] web_search {"query": "zz"}\n[TOOL] react {"emoji_id": "129"}'
_m8 = owner_priv(id="c8a", message_id="c8a", text="查下 zz 再给我这条点赞")
run_process(_m8, [BODY_C8], dm8, confirm_write=True)
_ent8 = A._TOOL_PENDING.get(KEY_PRIV)
chk("C8a 首轮：read 执行、write 停在待确认；done 里是 read 的动作",
    _ent8 is not None and SEARCH_CALLS == ["zz"]
    and [c.get("tool") for c in (_ent8.get("calls") or [])] == ["react"]
    and [a.get("tool") for a in (_ent8.get("done") or [])] == ["web_search"]
    and (_ent8.get("done") or [{}])[0].get("ok") is True,
    (SEARCH_CALLS, _ent8 and (_ent8.get("calls"), _ent8.get("done"))))
ALOG.clear()
dm8.reactions.clear()
_m8b = owner_priv(id="c8b", message_id="c8b", text="确认")
_, calls8b, _ = run_process(_m8b, ["都办好了。"], dm8, confirm_write=True)
_fb = calls8b[0][-1]["content"] if calls8b and calls8b[0] else ""
chk("C8b 续跑第一轮回喂同时有 read 与 write 的结果，read 在前",
    "【已执行动作】" in _fb and "- web_search(" in _fb and "- react(" in _fb
    and _fb.index("- web_search(") < _fb.index("- react("),
    _fb[:300])
chk("C8c read 不执行第二遍；write 在续跑里执行 1 次",
    SEARCH_CALLS == ["zz"] and dm8.reactions == [("c8b", "129", True)],
    (SEARCH_CALLS, dm8.reactions))

# ============ C9 确认开关关：直接执行、不产生待确认 ============

clear_pending()
ALOG.clear()
SEARCH_CALLS.clear()
dm9 = RunDaemon()
_m9 = owner_priv(id="c9", message_id="c9", text="给这条消息点个赞")
run_process(_m9, [BODY_REACT, "好了，挂上了。"], dm9, confirm_write=False)
chk("C9a confirm 关：write 直接执行（挂在触发消息上）",
    dm9.reactions == [("c9", "129", True)], dm9.reactions)
chk("C9b 不产生待确认、无 WAIT 日志",
    A._TOOL_PENDING == {} and not alog_of("AGENT_TOOL_CONFIRM_WAIT"),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM_WAIT")))
chk("C9c 最终回复照发", dm9.sent and "好了，挂上了。" in joined_of(dm9),
    joined_of(dm9)[:80])

# ============ C10 续跑时闸照过（I4） ============

clear_pending()
DANGER_EXEC.clear()
seed_pending(KEY_PRIV, [
    {"tool": "ghost_tool", "args": {}, "raw": "", "error": None},
    {"tool": "react", "args": {"wrong": 1}, "raw": "", "error": None},
    {"tool": "smoke_danger", "args": {"cmd": "ls"}, "raw": "", "error": None},
], done=[{"tool": "web_search", "args": {"query": "zz"}, "ok": True,
          "summary": "搜到了"}])
dm10 = RunDaemon()
_m10 = owner_priv(id="c10", message_id="c10", text="确认")
_, calls10, _ = run_process(_m10, ["这三件都没办成。"], dm10, confirm_write=True)
_fb10 = calls10[0][-1]["content"] if calls10 and calls10[0] else ""
chk("C10a 未登记的名字不执行、只在回喂里说明",
    dm10.reactions == [] and DANGER_EXEC == []
    and "没有这个工具：ghost_tool" in _fb10,
    (dm10.reactions, DANGER_EXEC, _fb10[:200]))
chk("C10b 参数不对不执行、回喂说明原因", "多了不认识的参数：wrong" in _fb10,
    _fb10[:200])
chk("C10c danger 无授权：续跑也拒、回喂是授权码指引",
    "需要授权码" in _fb10, _fb10[:200])

# ============ C11 确认词边界 ============

for _t, _exp in [("确认。", True), (" 好 ", True), ("OK", True),
                 ("确认一下 details", False), ("不确认", False)]:
    chk("C11 确认词判定 %r → %s" % (_t, _exp),
        A._tool_confirm_word(_t) == _exp, A._tool_confirm_word(_t))

# ============ C12 续跑的消息不再进衔接点收刮（_tool_resumed） ============

clear_pending()
REC12 = []


def _loop12(call_model, specs, first_reply, ctx, opts, resume=None,
            resume_done=(), **k):
    REC12.append({"resume": copy.deepcopy(resume),
                  "resume_done": copy.deepcopy(resume_done),
                  "first_reply": first_reply})
    return {"final": '好了。\n[TOOL] react {"emoji_id": "42"}',
            "actions": [], "rounds": 1, "stop": "final", "pending": []}


seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}],
             done=[{"tool": "web_search", "args": {"query": "zz"}, "ok": True,
                    "summary": "搜到了"}])
dm12 = RunDaemon()
_m12 = owner_priv(id="c12", message_id="c12", text="确认")
set_cfg(True, True)
_force12 = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=False,
                REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
                VCAP_FORCE=False, vision_enabled=lambda: False,
                web_search_enabled=lambda: False,
                chat_anti_interrupt_enabled=lambda: True,
                lean_ctx_on=lambda: True,
                _cap_chat_allowed=lambda ct, cid: False,
                call_glm_with_fallback=make_chain([])[0],
                owner_qq=lambda: OWNER_QQ)
with patched(A, **_force12), patched(T, tool_loop=_loop12):
    reset_cap()
    ALOG.clear()
    try:
        A.process(dm12, _m12)
    except Exception as exc:
        dm12.sent = [["EXC:%r" % exc]]
chk("C12a 续跑调用带 resume/resume_done，且 tool_loop 只被调 1 次",
    len(REC12) == 1 and REC12[0]["resume"][0]["tool"] == "react"
    and REC12[0]["resume_done"][0]["tool"] == "web_search"
    and REC12[0]["first_reply"] == "(原始回复)",
    [(r["resume"], r["resume_done"]) for r in REC12])
chk("C12b 衔接点跳过：final 原样往后走（[TOOL] 行未被二次收刮、未二次进循环）",
    len(REC12) == 1 and "[TOOL] react" in joined_of(dm12), joined_of(dm12)[:160])
chk("C12c 续跑的循环照打 AGENT_TOOL_LOOP 日志",
    any("AGENT_TOOL_LOOP" in l and "stop=final" in l for l in ALOG),
    alog_of("AGENT_TOOL_LOOP"))

# ============ C13 没有待确认：拦截处零副作用 ============

clear_pending()
ALOG.clear()
dm13 = RunDaemon()
dm13 = RunDaemon()
_m13 = owner_priv(id="c13", message_id="c13", text="在吗")
run_process(_m13, ["在呢。"], dm13, confirm_write=True)
chk("C13a 无待确认：_TOOL_PENDING 不变（仍为空）、无 CONFIRM 日志",
    A._TOOL_PENDING == {} and not alog_of("AGENT_TOOL_CONFIRM"),
    (list(A._TOOL_PENDING), alog_of("AGENT_TOOL_CONFIRM")))
chk("C13b 消息照常回复", dm13.sent and "在呢。" in joined_of(dm13),
    joined_of(dm13)[:80])

# ============ C14 两会话各自落表互不覆盖；失败不影响别人；覆盖打 REPLACE ============

clear_pending()
dmA1 = RunDaemon()
_mA1 = owner_priv(id="c14a1", message_id="c14a1", text="给这条消息点个赞")
run_process(_mA1, [BODY_REACT], dmA1, confirm_write=True)
dmB1 = RunDaemon(send_results=[{"ok": False, "error": "限流"}])
_mB1 = owner_grp(id="c14b1", message_id="c14b1", text="给这条点个赞")
run_process(_mB1, ['[TOOL] react {"emoji_id": "131"}'], dmB1, confirm_write=True)
_a_after = copy.deepcopy(A._TOOL_PENDING.get(KEY_PRIV))
chk("C14a 会话 A 落表、会话 B 发送失败不落表",
    _a_after is not None and KEY_GRP not in A._TOOL_PENDING,
    list(A._TOOL_PENDING))
dmB2 = RunDaemon()
_mB2 = owner_grp(id="c14b2", message_id="c14b2", text="给这条点个赞")
run_process(_mB2, ['[TOOL] react {"emoji_id": "131"}'], dmB2, confirm_write=True)
chk("C14b B 发送成功后落表，与 A 各占一格、互不覆盖（I2）",
    KEY_GRP in A._TOOL_PENDING and A._TOOL_PENDING.get(KEY_PRIV) == _a_after
    and A._TOOL_PENDING[KEY_GRP]["calls"][0]["args"] == {"emoji_id": "131"},
    list(A._TOOL_PENDING))
# 覆盖分支（同会话已有待确认 → 覆盖 + REPLACE）：进程级路径上同会话的任何
# 主人消息都会先被拦截消费掉旧待确认，提交时才撞上旧待确认的入口是提交
# 函数本身——直接调 _tool_commit_pending 钉住这个分支。
clear_pending()
seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}])
ALOG.clear()
_msg_cand = {"chat_type": "private", "chat_id": OWNER_QQ,
             "_tool_confirm_candidate": {
                 "calls": [{"tool": "react", "args": {"emoji_id": "140"},
                            "raw": "", "error": None}],
                 "first_reply": "(新复述)", "done": []}}
A._tool_commit_pending(_msg_cand, {"ok": True})
_newa = A._TOOL_PENDING.get(KEY_PRIV)
chk("C14c 同会话已有待确认：提交覆盖旧的（calls 已换成新调用、候选已消费）",
    _newa is not None and _newa.get("calls", [{}])[0].get("args") \
        == {"emoji_id": "140"}
    and "_tool_confirm_candidate" not in _msg_cand,
    _newa and _newa.get("calls"))
chk("C14d 覆盖时打 AGENT_TOOL_CONFIRM_REPLACE",
    any("AGENT_TOOL_CONFIRM_REPLACE chat=private/…0086" in l for l in ALOG),
    alog_of("AGENT_TOOL_CONFIRM_REPLACE"))

# ============ C15 管理台卡片说明 ============

SRC_UI = pathlib.Path("plugins", "qq-bot", "scripts", "qq_admin_ui.py").read_text(
    encoding="utf-8")
chk("C15a 卡片说明不再有「不会自动续跑」", "不会自动续跑" not in SRC_UI, "")
chk("C15b 说明换成「回复「确认」后执行」，两个复选框仍在",
    "回复「确认」后执行" in SRC_UI
    and SRC_UI.count("onchange=\"saveKVLoad('/api/reply-config',{tool_confirm_write:") == 1
    and SRC_UI.count("onchange=\"saveKVLoad('/api/reply-config',{tool_loop_enabled:") == 1, "")

# ============ C16 续跑出错不静默：确定性、与事实相符的回复 ============

clear_pending()
dm16a = RunDaemon()
# pending 里放一个未登记的工具名：续跑第一轮不产生任何已执行动作（只在回喂
# 里说明），随后模型续写抛异常 → actions 为空 → 发固定话
seed_pending(KEY_PRIV, [{"tool": "ghost_tool", "args": {},
                         "raw": "", "error": None}],
             first_reply="我马上就办，请稍等。")
ALOG.clear()
_m16a = owner_priv(id="c16a", message_id="c16a", text="确认")
_, _, _ = run_process(_m16a, [RuntimeError("续写炸了")], dm16a,
                      confirm_write=True)
_j16a = joined_of(dm16a)
chk("C16a 无任何动作时发出固定话（不含模型输出、不含工具行、有 ERR 日志）",
    A._TOOL_RESUME_FAIL_NOTE in _j16a
    and "我马上就办" not in _j16a and "这件事没有做成" not in _j16a
    and "[TOOL]" not in _j16a and "ghost_tool" not in _j16a
    and any("AGENT_TOOL_RESUME_ERR" in l for l in alog_of("AGENT_TOOL_RESUME_ERR")),
    (_j16a[:160], alog_of("AGENT_TOOL_RESUME_ERR")))

clear_pending()
SEARCH_CALLS.clear()
dm16b = RunDaemon()
seed_pending(KEY_PRIV, [{"tool": "web_search", "args": {"query": "cc"},
                         "raw": "", "error": None}],
             first_reply="我先查一下再告诉你。")
ALOG.clear()
_m16b = owner_priv(id="c16b", message_id="c16b", text="确认")
_, _, _ = run_process(_m16b, [RuntimeError("续写又炸了")], dm16b,
                      confirm_write=True)
_j16b = joined_of(dm16b)
chk("C16b 已执行一个 read 后出错：发「已经做了」清单（含工具名、不含模型输出）",
    "已经做了" in _j16b and "web_search" in _j16b
    and "我先查一下" not in _j16b and "这件事没有做成" not in _j16b
    and any("AGENT_TOOL_RESUME_ERR" in l for l in alog_of("AGENT_TOOL_RESUME_ERR")),
    (_j16b[:160], alog_of("AGENT_TOOL_RESUME_ERR")))

# ============ I5 续跑最终回复走普通后处理：[QUIET:5] 剥离并真实安静 ============

clear_pending()
seed_pending(KEY_PRIV, [{"tool": "react", "args": {"emoji_id": "129"},
                         "raw": "", "error": None}])
dmI5 = RunDaemon()
_mI5 = owner_priv(id="i5", message_id="i5", text="确认")
_, _, _ = run_process(_mI5, ["办好了。[QUIET:5]"], dmI5, confirm_write=True)
chk("I5a 续跑回复里的 [QUIET:5] 被普通后处理剥离",
    dmI5.sent and "办好了。" in joined_of(dmI5)
    and "[QUIET" not in joined_of(dmI5), joined_of(dmI5)[:120])
chk("I5b [QUIET:5] 真实触发安静（daemon.set_quiet 收到 5）",
    dmI5.quiets == [5], dmI5.quiets)

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
sys.exit(1 if fails else 0)
