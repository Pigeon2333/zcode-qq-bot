# -*- coding: utf-8 -*-
"""冒烟：WP-07 A2a-2 工具循环真执行——开关开且是主人时 [TOOL] 行进 tool_loop，
结果回喂模型后发最终回复；其余情况与改前一致（对应派单验收 R1–R10）。

  R1  开+主人+v2：actions[] 里的 [TOOL] web_search 进循环执行（search 桩被调
      1 次、参数正确）；第二轮纯文本照发；AGENT_TOOL_LOOP 日志列 web_search:成功；
      回喂调用带基础 messages（无 JSON 格式段）+ history + 最终回复提示；
      提示并进末条回喂拷贝（无相邻两条 user，R1f）；history 为空时提示是
      独立末条 user 消息（R1h）。
  R2  开+主人+v1：正文里的 [TOOL] 行同样进循环执行。
  R3  非主人（群聊普通成员、私聊非主人）：不执行任何工具、tool_loop 不被调，
      行被剥掉、其余文字保留。
  R4  关+主人：不执行（同上一单 W6），无 AGENT_TOOL_LOOP。
  R5  第二轮回了带 say 的 JSON → 发出 say 文字，无 JSON 外壳、无 [TOOL]。
  R6a 第二轮抛异常 → AGENT_TOOL_LOOP_ERR，发出剥掉工具行之后的文本；
  R6b 第二轮空且无动作 → 不发送；R6c 空但有动作 → 发「已经做了」收尾清单。
  R7a confirm_write 开 + react → set_msg_reaction 未被调、发复述、stop=confirm；
  R7b 关 → 直接执行，桩收到 (message_id, emoji_id)。
  R8  register_builtin 连调两次不抛异常，工具表里两个工具各一份（重复登记报
      同名）、risk/scope 与派单一致。
  R9  设置页有「工具循环（实验性）」卡片与两个复选框（页面脚本各出现一次保
      存入口）；POST 后 reply_config 落盘为布尔值（_ui_jscheck 单独跑）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_wire.py 的做法）；模型调用（call_glm_with_fallback / call_vision）、
search_web、daemon.set_msg_reaction 全部打桩，零联网、零真实执行；管理台处理器
UiHandler.__new__ 直调，不起服务。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_run.py
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_run_iso_")
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
DLOG = []
D.out = lambda line: DLOG.append(str(line))

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


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


OWNER_QQ = "10086"
NOT_OWNER = "99999"


def base_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "group", "chat_id": "555001",
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


class RunDaemon(object):
    """显式提供 process/工具用到的真实桩；set_msg_reaction 记参不外发。"""

    def __init__(self, history=None):
        self._history = history or []
        self.acked = []
        self.sent = []
        self.reactions = []

    def history(self, chat_type, chat_id, limit=30):
        h = self._history
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))

    def set_msg_reaction(self, message_id, emoji_id, on=True):
        self.reactions.append((message_id, emoji_id, on))
        return True, "ok"

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


# ---- 桩：搜索与模型链 ----

SEARCH_CALLS = []


def fake_search(q):
    SEARCH_CALLS.append(q)
    return ("- x 是测试条目（example.com）\n- x 的第二条（baike.example）", "bing")


TB.register_builtin(fake_search)


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


def run_process(msg, outputs, v2=True, json_mode=True, loop_on=True,
                confirm_write=False, loop_rec=None):
    """照 _smoke_toolloop_wire.py 的可行组合走真实 process()，返回 (dm, 调用快照)。
    loop_rec 是列表时：T.tool_loop 被打桩记账（用于证明没进循环）。"""
    dm = RunDaemon()
    A.PLUGIN_HOST.daemon = dm
    chain, calls = make_chain(list(outputs))
    _rec_calls = []

    def _rec_loop(*a, **k):
        _rec_calls.append(a)
        return {"final": "smoke: must not be called", "actions": [],
                "stop": "final", "rounds": 0, "pending": []}

    set_cfg(loop_on, confirm_write)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=v2, REPLY_JSON_FORCE=json_mode,
                  REACT_ONLY_FORCE=False, VCAP_FORCE=False,
                  vision_enabled=lambda: False, web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True, lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=chain,
                  call_vision=lambda ms, urls, daemon=None: (_ for _ in ()).throw(
                      AssertionError("同步识图不应被调")),
                  owner_qq=lambda: OWNER_QQ)
    _tp = {"tool_loop": _rec_loop} if loop_rec is not None else {}
    with patched(A, **_force), patched(T, **_tp):
        reset_cap()
        ALOG.clear()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
    if loop_rec is not None:
        loop_rec.extend(_rec_calls)
    return dm, calls


def joined_of(dm):
    return "".join(t for ts in dm.sent for t in ts)


HINT = "只用纯文本写最终回复"
R1_TEXT = "查好了，x 是这么回事。"


def _json_main(query, say="主人稍等，我查一下"):
    return json.dumps({"want": 2, "say": [say], "emoji": None, "memory": [],
                       "wait": False, "voice": False, "sticker": None,
                       "quiet_minutes": None,
                       "actions": ["[TOOL] web_search {\"query\": \"%s\"}" % query]},
                      ensure_ascii=False)


# ============ R1 开+主人+v2：actions 里的 [TOOL] 进循环 ============

SEARCH_CALLS.clear()
_m1 = owner_priv(message_id="r1", text="帮我查查x怎么样")
dm1, calls1 = run_process(_m1, [_json_main("x"), R1_TEXT], v2=True, json_mode=True)
j1 = joined_of(dm1)
chk("R1a search 桩被调 1 次、参数正确", SEARCH_CALLS == ["x"], SEARCH_CALLS)
chk("R1b 最终发出的文字=第二轮纯文本",
    dm1.sent and j1.strip() == R1_TEXT and "EXC" not in j1, j1[:120])
chk("R1c AGENT_TOOL_LOOP 日志：stop=final、web_search:成功",
    any("AGENT_TOOL_LOOP" in l and "stop=final" in l and "web_search:成功" in l
        for l in ALOG), alog_of("AGENT_TOOL_LOOP"))
chk("R1d 主调用+循环回喂共 2 次模型调用", len(calls1) == 2,
    [len(c) for c in calls1])
chk("R1e 循环调用=基础 messages（system 无 JSON 格式段，主调用有）",
    '"want"' in calls1[0][0]["content"] and '"want"' not in calls1[1][0]["content"],
    calls1[1][0]["content"][-80:])
chk("R1f 循环调用消息列表无相邻两条 user（提示并进末条回喂拷贝）",
    calls1[1] and all(not (calls1[1][i].get("role") == "user"
                           and calls1[1][i + 1].get("role") == "user")
                      for i in range(len(calls1[1]) - 1))
    and calls1[1][-1].get("role") == "user" and HINT in calls1[1][-1]["content"],
    [m.get("role") for m in calls1[1]])
chk("R1g history 带首轮 assistant 原文与【已执行动作】回喂",
    any(m.get("role") == "assistant" and "[TOOL] web_search" in m.get("content", "")
        for m in calls1[1])
    and any("【已执行动作】" in m.get("content", "") and "web_search" in m.get("content", "")
            for m in calls1[1]), "")

# R1h：history 为空的调用（tool_loop 打桩后用空 history 直调 call_model）
# → 此时无回喂消息可并，提示作为独立 user 消息出现在末尾


def run_history_empty():
    dm = RunDaemon()
    A.PLUGIN_HOST.daemon = dm
    chain, calls = make_chain([_json_main("x"), "行"])

    def _loop_probe(call_model, specs, first_reply, ctx, opts, **k):
        call_model([])
        return {"stop": "final", "final": "行", "actions": [],
                "rounds": 0, "pending": []}

    set_cfg(True, False)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True,
                  REACT_ONLY_FORCE=False, VCAP_FORCE=False,
                  vision_enabled=lambda: False, web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True, lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=chain, owner_qq=lambda: OWNER_QQ)
    with patched(A, **_force), patched(T, tool_loop=_loop_probe):
        reset_cap()
        ALOG.clear()
        try:
            A.process(dm, owner_priv(message_id="r1h", text="帮我查查"))
        except Exception as exc:
            dm.sent = [["EXC:%r" % exc]]
    return calls


_calls_h = run_history_empty()
chk("R1h history 为空：提示是独立 user 消息且在末尾",
    len(_calls_h) == 2 and len(_calls_h[1]) == 3
    and _calls_h[1][-1].get("role") == "user"
    and _calls_h[1][-1]["content"] == A._TOOL_FINAL_NOTE,
    ([m.get("role") for m in _calls_h[1]],
     repr(_calls_h[1][-1]["content"][:60]) if _calls_h[1] else ""))
chk("R1g history 带首轮 assistant 原文与【已执行动作】回喂",
    any(m.get("role") == "assistant" and "[TOOL] web_search" in m.get("content", "")
        for m in calls1[1])
    and any("【已执行动作】" in m.get("content", "") and "web_search" in m.get("content", "")
            for m in calls1[1]), "")

# ============ R2 开+主人+v1：正文里的 [TOOL] 行进循环 ============

SEARCH_CALLS.clear()
_m2 = owner_priv(message_id="r2", text="帮我查查y怎么样")
BODY2 = "让我查查\n[TOOL] web_search {\"query\": \"y\"}"
dm2, calls2 = run_process(_m2, [BODY2, "查好了，y 是这么回事。"],
                          v2=False, json_mode=False)
j2 = joined_of(dm2)
chk("R2a v1 正文 [TOOL] 行同样进循环（search 被调、参数正确）",
    SEARCH_CALLS == ["y"], SEARCH_CALLS)
chk("R2b 发出的是第二轮文本、无 [TOOL]",
    dm2.sent and j2.strip() == "查好了，y 是这么回事。" and "[TOOL]" not in j2, j2[:120])
chk("R2c 日志 web_search:成功", any("web_search:成功" in l for l in alog_of("AGENT_TOOL_LOOP")),
    alog_of("AGENT_TOOL_LOOP"))

# ============ R3 非主人：不执行、tool_loop 不被调、行被剥掉 ============

SEARCH_CALLS.clear()
REC = []
_grp3 = base_msg(message_id="r3g", text="帮我查下z\n[TOOL] web_search {\"query\": \"z\"}",
                 trigger_reason="at", at_me=True)
dm3g, _ = run_process(_grp3, [_json_main("z")], loop_on=True, loop_rec=REC)
j3g = joined_of(dm3g)
chk("R3a 群聊普通成员：不执行工具、tool_loop 未被调",
    SEARCH_CALLS == [] and REC == [], (SEARCH_CALLS, REC))
chk("R3b 群聊普通成员：行被剥掉、其余文字保留、无 AGENT_TOOL_LOOP",
    dm3g.sent and "[TOOL]" not in j3g and "主人稍等，我查一下" in j3g
    and not alog_of("AGENT_TOOL_LOOP"), j3g[:120])
_priv3 = base_msg(chat_type="private", chat_id=NOT_OWNER, chat_name="路人",
                  sender_id=NOT_OWNER, sender_name="路人", at_me=False,
                  trigger_reason="chat", message_id="r3p",
                  text="帮我查下w\n[TOOL] web_search {\"query\": \"w\"}")
dm3p, _ = run_process(_priv3, [_json_main("w", say="好的，我看看")],
                      loop_on=True, loop_rec=REC)
chk("R3c 私聊非主人：同样不执行、tool_loop 未被调",
    SEARCH_CALLS == [] and len(REC) == 0, (SEARCH_CALLS, REC))

# ============ R4 关+主人：不执行（同上一单 W6） ============

SEARCH_CALLS.clear()
REC4 = []
_m4 = owner_priv(message_id="r4", text="帮我查查x怎么样")
dm4, _ = run_process(_m4, [_json_main("x")], loop_on=False, loop_rec=REC4)
j4 = joined_of(dm4)
chk("R4 开关关+主人：不执行、tool_loop 未被调、无 AGENT_TOOL_LOOP",
    SEARCH_CALLS == [] and REC4 == [] and not alog_of("AGENT_TOOL_LOOP"),
    (SEARCH_CALLS, REC4))
chk("R4b 行为与改前一致：行剥掉、其余文字保留",
    dm4.sent and "[TOOL]" not in j4 and "主人稍等，我查一下" in j4, j4[:120])

# ============ R5 第二轮回了带 say 的 JSON ============

SEARCH_CALLS.clear()
_json_say = json.dumps({"want": 2, "say": [R1_TEXT], "emoji": None, "memory": [],
                        "wait": False, "voice": False, "sticker": None,
                        "quiet_minutes": None, "actions": []}, ensure_ascii=False)
_m5 = owner_priv(message_id="r5", text="帮我查查x怎么样")
dm5, _ = run_process(_m5, [_json_main("x"), _json_say])
j5 = joined_of(dm5)
chk("R5 发出的是 say 里的文字，无 JSON 外壳、无 [TOOL]",
    dm5.sent and j5.strip() == R1_TEXT and "{" not in j5 and "[TOOL]" not in j5, j5[:160])

# ============ R6a 第二轮抛异常 ============

SEARCH_CALLS.clear()
BODY6 = "让我查查\n[TOOL] web_search {\"query\": \"q6\"}"
dm6a, _ = run_process(owner_priv(message_id="r6a", text="帮我查查"),
                      [BODY6, RuntimeError("第二轮炸了")], v2=False, json_mode=False)
j6a = joined_of(dm6a)
chk("R6a 第二轮抛异常：AGENT_TOOL_LOOP_ERR、发出剥行后的文本、无循环收尾",
    alog_of("AGENT_TOOL_LOOP_ERR") and j6a.strip() == "让我查查"
    and "已经做了" not in j6a and "[TOOL]" not in j6a, (alog_of("AGENT_TOOL_LOOP_ERR"), j6a[:120]))

# ============ R6b 第二轮空且无动作 → 不发送 ============

dm6b, _ = run_process(owner_priv(message_id="r6b", text="帮我查查"),
                      ["[TOOL] ghost_tool {}", ""], v2=False, json_mode=False)
chk("R6b 第二轮空+无动作：不发送、不抛异常",
    dm6b.sent == [] and "EXC" not in joined_of(dm6b), dm6b.sent)

# ============ R6c 第二轮空但有动作 → 发收尾清单 ============

SEARCH_CALLS.clear()
dm6c, _ = run_process(owner_priv(message_id="r6c", text="帮我查查"),
                      ["[TOOL] web_search {\"query\": \"zc\"}", ""],
                      v2=False, json_mode=False)
j6c = joined_of(dm6c)
chk("R6c 第二轮空+有动作：发出含「已经做了」与工具名的收尾清单",
    dm6c.sent and "已经做了" in j6c and "web_search" in j6c, j6c[:160])

# ============ R7 复述确认 ============

BODY7 = "[TOOL] react {\"emoji_id\": \"129\"}"
dm7a, calls7a = run_process(owner_priv(message_id="7712", text="给这条消息点个赞"),
                            [BODY7], v2=False, json_mode=False, loop_on=True,
                            confirm_write=True)
j7a = joined_of(dm7a)
chk("R7a confirm 开：set_msg_reaction 未被调", dm7a.reactions == [], dm7a.reactions)
chk("R7b confirm 开：发出复述（回复「确认」我就执行）",
    dm7a.sent and "回复「确认」我就执行" in j7a, j7a[:160])
chk("R7c confirm 开：日志 stop=confirm、模型未补调",
    any("stop=confirm" in l for l in alog_of("AGENT_TOOL_LOOP")) and len(calls7a) == 1,
    alog_of("AGENT_TOOL_LOOP"))

SEARCH_CALLS.clear()
dm7b, _ = run_process(owner_priv(message_id="7712", text="给这条消息点个赞"),
                      [BODY7, "好了，挂上了。"], v2=False, json_mode=False,
                      loop_on=True, confirm_write=False)
j7b = joined_of(dm7b)
chk("R7d confirm 关：直接执行，桩收到 (message_id, emoji_id)",
    dm7b.reactions == [("7712", "129", True)], dm7b.reactions)
chk("R7e confirm 关：发最终回复、日志 react:成功",
    dm7b.sent and "好了，挂上了。" in j7b
    and any("react:成功" in l for l in alog_of("AGENT_TOOL_LOOP")),
    (j7b[:120], alog_of("AGENT_TOOL_LOOP")))

# ============ R8 register_builtin 幂等 ============

try:
    TB.register_builtin(fake_search)
    TB.register_builtin(fake_search)
    dup_ok = True
except Exception as exc:
    dup_ok = False
    print("register_builtin 异常：%r" % exc)


def spec_named(name):
    return {"name": name, "desc": "d", "params": [], "risk": "read",
            "scope": "all", "returns": "r", "run": lambda d, m, a: None}


def _raises_dup(name):
    try:
        T.register(spec_named(name))
        return False
    except ValueError as e:
        return "已登记" in str(e)


ws, rc = T.get_tool("web_search"), T.get_tool("react")
chk("R8 重复登记不抛异常；两个工具各一份（手工再登记报同名）",
    dup_ok and _raises_dup("web_search") and _raises_dup("react")
    and ws is not None and rc is not None, "")
chk("R8b web_search=read/all、react=write/owner，参数与派单一致",
    ws and ws["risk"] == "read" and ws["scope"] == "all"
    and [p["name"] for p in ws["params"]] == ["query"]
    and rc and rc["risk"] == "write" and rc["scope"] == "owner"
    and [p["name"] for p in rc["params"]] == ["emoji_id"],
    (ws and (ws["risk"], ws["scope"]), rc and (rc["risk"], rc["scope"])))

# ============ R9 界面卡片与落盘 ============

SRC_UI = pathlib.Path("plugins", "qq-bot", "scripts", "qq_admin_ui.py").read_text(
    encoding="utf-8")
chk("R9a 设置页源码有「工具循环（实验性）」卡片且挂在对话架构分节后",
    "工具循环（实验性）" in SRC_UI and "+stArchV2(rv)+stToolLoop(rv)" in SRC_UI, "")
chk("R9b 两个复选框的保存入口各出现一次",
    SRC_UI.count("onchange=\"saveKVLoad('/api/reply-config',{tool_loop_enabled:") == 1
    and SRC_UI.count("onchange=\"saveKVLoad('/api/reply-config',{tool_confirm_write:") == 1, "")


def ui_call(method, path, bodyq=None):
    """进程内直调 UiHandler（照 _smoke_routes.py 的写法）。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    raw = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


rp9 = ui_call("do_POST", "/api/reply-config",
              {"tool_loop_enabled": True, "tool_confirm_write": True})
r9 = ui_call("do_GET", "/api/reply-config")
on_disk9 = json.loads(pathlib.Path(RC_PATH).read_text(encoding="utf-8"))
chk("R9c 勾选后 reply_config 落盘为布尔值",
    rp9 and rp9[0][0] == 200 and rp9[0][1].get("ok") is True
    and r9[0][1].get("tool_loop_enabled") is True
    and r9[0][1].get("tool_confirm_write") is True
    and on_disk9.get("tool_loop_enabled") is True
    and on_disk9.get("tool_confirm_write") is True, (r9, on_disk9))
chk("R9d agent 读取函数读到布尔 True",
    A._tool_loop_on() is True and A._tool_confirm_write_on() is True, "")

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
