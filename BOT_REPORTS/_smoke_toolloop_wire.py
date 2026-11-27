# -*- coding: utf-8 -*-
"""冒烟：WP-07 A2a 工具循环接线——开关登记、主人教学替换、出站清洗（不执行任何工具）。

对应派单验收 W1–W8：
  W1 开关关：system 无 [TOOL]/固定说明，带话与画图教学仍在；json_rule_text 不含 [TOOL]。
  W2 开关开+主人：system 有 [TOOL] 与固定说明，带话/画图两段不在；
     设备操控段与开关关时逐字相同（同一条主人消息分别构建，按段比对）。
  W3 开关开+非主人（群聊普通成员、私聊非主人）：system/user 与开关关时逐字一致。
  W4 开关关（_tool_loop_on 钉死 False）零变化下限：主人消息与群聊消息的 system
     无 [TOOL]、无固定说明（与旧代码的逐字节比对由主会话基准比对工具做）。
  W5 出站清洗：正文夹 [TOOL] 行的回复，v1/v2 × 开关开/关 四种组合，发出的
     文字无 [TOOL]，其余文字原样保留。
  W6 开关开+主人：actions[] 带 [TOOL] 条目 → 不执行任何东西（run_tool/tool_loop
     打桩确认未被调用）、无报错。
  W7 键登记：/api/reply-config GET 默认两键 False；POST 保存回读一致；
     agent 两个读取函数读得到。
  W8 三场景冒烟：无引用无图/带引用/纯图 × 开关开/关 各构建一次，不抛异常。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照 _smoke_routes.py
的做法）；模型调用（call_glm_with_fallback / call_vision）、run_tool/tool_loop 全部
打桩，零联网、零执行；管理台处理器 UiHandler.__new__ 直调，不起服务。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_wire.py
"""
import contextlib
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402
import qq_admin_ui as U  # noqa: E402
import qq_tools as T  # noqa: E402

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


def set_tool_loop(on):
    write_reply_cfg({"tool_loop_enabled": bool(on)})


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


class NullDaemon(object):
    """只显式提供 build_messages/process 用到的方法，其余无副作用空操作。"""

    def __init__(self, history=None):
        self._history = history or []
        self.acked = []
        self.sent = []

    def history(self, chat_type, chat_id, limit=30):
        h = self._history
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


def sys_of(daemon, msg):
    msgs, _pname = A.build_messages(daemon, msg, ctx_k=32)
    return msgs[0]["content"], [m for m in msgs if m.get("role") == "user"][-1]["content"]


DAEMON = NullDaemon()
WITH_DC = dict(_device_ctrl_enabled=lambda: True)   # 设备操控段取「已开启」变体
OWNER_PATCH = dict(owner_qq=lambda: OWNER_QQ)

TOOL_MARK = "[TOOL]"
FIX_NOTE = ("需要做上面这些事时，把调用写进 actions，一行一个："
            "[TOOL] 名字 {json参数}；做不了的就直说做不到，不要说已经做了。")
DAIHUA_MARK = "你可以出现在多个群里"          # 带话段开头几个字
DRAW_MARK = "你具备AI生图能力"               # 画图段开头几个字
DEVCTRL_MARK = "「设备操控」"                 # 设备操控段锚点


# ============ W1 开关关：教学不变、无 [TOOL] ============

set_tool_loop(False)
for v2 in (False, True):
    _force = dict(PROMPT_V2_FORCE=v2, **OWNER_PATCH, **WITH_DC)
    with patched(A, **_force):
        s, _u = sys_of(DAEMON, owner_priv())
    chk("W1a v%d 开关关：system 无 [TOOL]" % v2, TOOL_MARK not in s, s[-200:])
    chk("W1b v%d 开关关：无固定说明" % v2, FIX_NOTE not in s)
    chk("W1c v%d 开关关：带话教学仍在" % v2, DAIHUA_MARK in s)
    chk("W1d v%d 开关关：画图教学仍在" % v2, DRAW_MARK in s)
    with patched(A, PROMPT_V2_FORCE=True, REPLY_JSON_FORCE=True, **OWNER_PATCH):
        jr = A.json_rule_text(owner_priv())
    chk("W1e v%d 开关关：json_rule_text 无 [TOOL]" % v2, TOOL_MARK not in jr, jr[-120:])

# ============ W2 开关开 + 主人：教学替换、设备操控段逐字不变 ============

set_tool_loop(True)
for v2 in (False, True):
    set_tool_loop(False)
    with patched(A, PROMPT_V2_FORCE=v2, **OWNER_PATCH, **WITH_DC):
        s_off, _ = sys_of(DAEMON, owner_priv())
    set_tool_loop(True)
    with patched(A, PROMPT_V2_FORCE=v2, **OWNER_PATCH, **WITH_DC):
        s_on, _ = sys_of(DAEMON, owner_priv())
    chk("W2a v%d 开关开+主人：system 有 [TOOL]" % v2, TOOL_MARK in s_on)
    chk("W2b v%d 开关开+主人：有固定说明" % v2, FIX_NOTE in s_on)
    chk("W2c v%d 开关开+主人：带话教学已换掉" % v2, DAIHUA_MARK not in s_on)
    chk("W2d v%d 开关开+主人：画图教学已换掉" % v2, DRAW_MARK not in s_on)
    _seg_off = s_off[s_off.index(DEVCTRL_MARK):]
    _seg_on = s_on[s_on.index(DEVCTRL_MARK):]
    chk("W2e v%d 设备操控段逐字相同（按段比对）" % v2, _seg_off == _seg_on,
        (repr(_seg_off[:60]), repr(_seg_on[:60])))
    chk("W2f v%d 设备操控段本身有内容" % v2, len(_seg_on) > 50 and "## 可用表情包" in _seg_on)

# ============ W3 开关开 + 非主人：逐字一致 ============

_grp = base_msg(text="今天吃什么", trigger_reason="chat_random", at_me=False)
_priv = base_msg(chat_type="private", chat_id=NOT_OWNER,
                 chat_name="路人", text="你好呀", trigger_reason="chat")
set_tool_loop(False)
with patched(A, PROMPT_V2_FORCE=True, **OWNER_PATCH, **WITH_DC):
    g_off, gu_off = sys_of(DAEMON, _grp)
    p_off, pu_off = sys_of(DAEMON, _priv)
set_tool_loop(True)
with patched(A, PROMPT_V2_FORCE=True, **OWNER_PATCH, **WITH_DC):
    g_on, gu_on = sys_of(DAEMON, _grp)
    p_on, pu_on = sys_of(DAEMON, _priv)
chk("W3a 群聊普通成员：system 逐字一致", g_off == g_on)
chk("W3b 群聊普通成员：user 逐字一致", gu_off == gu_on)
chk("W3c 私聊非主人：system 逐字一致", p_off == p_on)
chk("W3d 私聊非主人：user 逐字一致", pu_off == pu_on)

# ============ W4 开关关零变化下限（_tool_loop_on 钉死 False） ============

set_tool_loop(True)   # 文件里开着，但函数被钉死 False：必须零变化
with patched(A, _tool_loop_on=lambda: False, PROMPT_V2_FORCE=False,
             **OWNER_PATCH, **WITH_DC):
    s4o, _ = sys_of(DAEMON, owner_priv())
    s4g, _ = sys_of(DAEMON, _grp)
chk("W4a 钉死关：主人私聊 system 无 [TOOL]", TOOL_MARK not in s4o)
chk("W4b 钉死关：主人私聊 system 无固定说明", FIX_NOTE not in s4o)
chk("W4c 钉死关：主人私聊带话/画图教学仍在",
    DAIHUA_MARK in s4o and DRAW_MARK in s4o)
chk("W4d 钉死关：群聊消息 system 无 [TOOL]/固定说明",
    TOOL_MARK not in s4g and FIX_NOTE not in s4g)
with patched(A, _tool_loop_on=lambda: False, PROMPT_V2_FORCE=True,
             REPLY_JSON_FORCE=True, **OWNER_PATCH):
    chk("W4e 钉死关：json_rule_text 无 [TOOL]", TOOL_MARK not in A.json_rule_text(owner_priv()))

# ============ W7 键登记（先做，后面 W5/W6 需要开关状态） ============

try:
    os.remove(RC_PATH)
except OSError:
    pass


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


r0 = ui_call("do_GET", "/api/reply-config")
chk("W7a 文件缺键：GET 默认 tool_loop_enabled=False",
    len(r0) == 1 and r0[0][0] == 200 and r0[0][1].get("tool_loop_enabled") is False, r0)
chk("W7b 文件缺键：GET 默认 tool_confirm_write=False",
    r0[0][1].get("tool_confirm_write") is False)
chk("W7c 缺键：_tool_loop_on()/_tool_confirm_write_on() 均 False",
    A._tool_loop_on() is False and A._tool_confirm_write_on() is False)
rp = ui_call("do_POST", "/api/reply-config",
             {"tool_loop_enabled": True, "tool_confirm_write": True})
chk("W7d POST 保存返回 ok", rp and rp[0][0] == 200 and rp[0][1].get("ok") is True, rp)
r1 = ui_call("do_GET", "/api/reply-config")
chk("W7e POST 后回读：两键 True",
    r1[0][1].get("tool_loop_enabled") is True
    and r1[0][1].get("tool_confirm_write") is True, r1)
chk("W7f agent 读取函数读到 True",
    A._tool_loop_on() is True and A._tool_confirm_write_on() is True)
on_disk = json.loads(pathlib.Path(RC_PATH).read_text(encoding="utf-8"))
chk("W7g 落盘一致", on_disk.get("tool_loop_enabled") is True
    and on_disk.get("tool_confirm_write") is True)
ui_call("do_POST", "/api/reply-config", {"tool_loop_enabled": False})
chk("W7h POST 关掉后回读 False、读取函数 False",
    ui_call("do_GET", "/api/reply-config")[0][1].get("tool_loop_enabled") is False
    and A._tool_loop_on() is False)
chk("W7i 模块级 import qq_tools（agent 用的就是登记模块本体）", A.qq_tools is T)

# ============ W5/W6：真实 process() 的出站清洗 ============

def run_process(msg, main_reply, v2, json_mode, tool_loop_on):
    """照 _smoke_arch.py 23g 的可行组合走真实 process()，返回 (发出的文字组, 日志)。"""
    dm = NullDaemon()
    A.PLUGIN_HOST.daemon = dm
    _calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        _calls.append(messages)
        return "mx", main_reply

    _tool_calls_made = []
    _loop_calls_made = []

    def _rec_run_tool(*a, **k):
        _tool_calls_made.append(a)
        return {"ok": False, "summary": "smoke: must not be called"}

    def _rec_tool_loop(*a, **k):
        _loop_calls_made.append(a)
        return {"status": "final", "final": "smoke: must not be called"}

    set_tool_loop(tool_loop_on)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=v2, REPLY_JSON_FORCE=json_mode,
                  REACT_ONLY_FORCE=False, VCAP_FORCE=False,
                  vision_enabled=lambda: False, chat_anti_interrupt_enabled=lambda: True,
                  lean_ctx_on=lambda: True, _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=_chain,
                  call_vision=lambda ms, urls, daemon=None: (_ for _ in ()).throw(
                      AssertionError("同步识图不应被调")),
                  **OWNER_PATCH)
    with patched(A, **_force), patched(T, run_tool=_rec_run_tool, tool_loop=_rec_tool_loop):
        reset_cap()
        ALOG.clear()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛：W6 的「没有报错」
            dm.sent = [["EXC:%r" % exc]]
    return dm.sent, list(ALOG), _tool_calls_made, _loop_calls_made


BODY_REPLY = "好的呀\n[TOOL] web_search {\"query\": \"测试\"}\n我帮你查一下"
GOOD_TEXTS = ["好的呀", "我帮你查一下"]
_json_reply = json.dumps({
    "want": 2, "say": GOOD_TEXTS, "emoji": None, "memory": [], "wait": False,
    "voice": False, "sticker": None, "quiet_minutes": None,
    "actions": ["[TOOL] web_search {\"query\": \"测试\"}"]}, ensure_ascii=False)

for v2, json_mode, tag in ((False, False, "v1"), (True, True, "v2结构化")):
    for loop_on in (False, True):
        m = base_msg(text="今天吃什么呢", message_id="w5%s%d" % (tag, loop_on))
        sent, logs, runs, loops = run_process(m, _json_reply if json_mode else BODY_REPLY,
                                              v2, json_mode, loop_on)
        joined = "".join(t for ts in sent for t in ts)
        chk("W5 %s 开关%s：发出的文字无 [TOOL]" % (tag, "开" if loop_on else "关"),
            sent and "EXC" not in joined and TOOL_MARK not in joined, (sent, joined[:120]))
        chk("W5 %s 开关%s：其余文字原样保留" % (tag, "开" if loop_on else "关"),
            sent and all(g in joined for g in GOOD_TEXTS), joined[:120])
        chk("W5 %s 开关%s：无 AGENT_TOOL_STRIP_ERR/AGENT_ERROR" % (tag, "开" if loop_on else "关"),
            not alog_of("AGENT_TOOL_STRIP_ERR") and not alog_of("AGENT_ERROR"))

# W6：开关开 + 主人 + actions[] 带 [TOOL] 条目 → 不执行任何东西、无报错
set_tool_loop(True)
_m6 = base_msg(text="今天吃什么呢", message_id="w6", sender_id=OWNER_QQ,
               chat_type="private", chat_id=OWNER_QQ, chat_name="主人",
               sender_name="主人", at_me=False, trigger_reason="chat")
sent6, logs6, runs6, loops6 = run_process(_m6, _json_reply, True, True, True)
joined6 = "".join(t for ts in sent6 for t in ts)
chk("W6a run_tool 打桩未被调用", runs6 == [], runs6)
chk("W6b tool_loop 打桩未被调用", loops6 == [], loops6)
chk("W6c 不报错、正文无 [TOOL]、其余文字保留",
    sent6 and "EXC" not in joined6 and TOOL_MARK not in joined6
    and all(g in joined6 for g in GOOD_TEXTS) and not alog_of("AGENT_ERROR"),
    (sent6, joined6[:120]))

# ============ W8 三场景冒烟：不抛异常 ============

_scenes = [
    ("无引用无图", base_msg(text="今天天气不错", trigger_reason="chat_random", at_me=False)),
    ("带引用", base_msg(text="这是什么意思呀", has_reply=True, reply_id="r1",
                        reply_to={"sender_name": "某人", "text": "被引的话", "is_bot": False},
                        reply_text="被引的话", trigger_reason="reply_to_bot", at_me=False)),
    ("纯图", base_msg(text="", image_urls=["https://x/1.jpg"],
                      trigger_reason="chat_random", at_me=False)),
]
for loop_on in (False, True):
    set_tool_loop(loop_on)
    for name, m in _scenes:
        err = None
        try:
            with patched(A, PROMPT_V2_FORCE=True, **OWNER_PATCH, **WITH_DC):
                sys_of(DAEMON, m)
        except Exception as exc:
            err = repr(exc)
        chk("W8 %s 开关%s：%s构建不抛异常" % (name, "开" if loop_on else "关", name),
            err is None, err)

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
