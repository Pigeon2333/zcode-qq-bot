# -*- coding: utf-8 -*-
"""冒烟：C7 群管动作并入工具表（gadmin_ban/gadmin_unban/gadmin_whole_ban/
gadmin_whole_unban/gadmin_notice/gadmin_recall）＋旧 [GADMIN] 标记在工具
循环开时转换成工具调用、循环关时旧路径逐字不变。对应施工单 A/B/C 与
C7 定稿表（2026-11-28 06:05）：

  G1  登记：六个工具 name/risk/scope/参数与定稿表一致；两个 fn 不传不登记、
      传了才登记；连调 register_builtin 两次不抛、表里各一个；main() 接线
      （gadmin_exec_fn=_gadmin_tool_call / gadmin_enabled_fn=_gadmin_tool_enabled）。
  G2  群管开关关：_tool_available 对 gadmin_* 全 False；清单与工具教学里没有
      gadmin_*；工具循环 specs 里也没有（模型凭空写 → 回喂「没有这个工具」、
      桩零调用）；run 第一道闸 → 「对话群管没有开启（管理台 → 群管 里打开）」。
  G3  开关开＋循环开：清单与教学里列出六个；旧 [GADMIN] 教学（_gadmin_rule）
      不再注入（工具清单代替，照设备操控「已开启版」的处理）；循环关时旧
      教学照旧注入（与源码原文同文）；非主人两种循环状态下都不注入。
  G4  工具循环关：旧标记照旧直接执行（对照基线：桩收到的 op 与参数）、
      AGENT_GADMIN_EXEC 日志在、标记剥掉不漏。
  G5  旧标记转换（端到端，真实 process＋真实工具循环）：五种动作带参数、
      全体禁言「开=1 / 开=0 / 缺省」、无参数行；转换后经工具执行恰好一次，
      桩收到的 op 与参数和循环关时旧分发处完全相同（同一份标记两种模式
      对照，按 daemon.gadmin_exec 的请求形状归一后逐字段相等）；循环日志
      是 AGENT_TOOL_LOOP 而不是 AGENT_GADMIN_EXEC。
  G6  转换函数单测（_gadmin_lines_to_calls）：映射、全角＝、坏行/认不出动作
      的行原样保留、[TOOL] 行与普通行不动。
  G7  三个风险级别各走各的闸：soft 直接执行（confirm_write 开着也一样）；
      write 受 tool_confirm_write 保护（复述→确认→续跑执行一次，关时直接
      执行）；danger 在授权码开关开时回确定性要码文本（逐行列出将执行的
      动作）且不执行，主人发对授权码后执行恰好一次、待确认清空、无论文术；
      错码拒之、待确认仍在；授权码开关关时直接执行。
  G8  「同一轮两种闸」：一句话里既有 write 又有 danger——授权没过时整轮
      不进循环（要码文本列出两个调用、都落授权类待确认）；授权窗口开着时
      进循环，write 停在复述，确认后两个都执行。
  G9  失败如实：gadmin_exec 返回失败 → 回喂带「没做成：」与原因；执行函数
      抛异常 → 「群管执行出错：…」；全程任何一轮发出的文本都不含 [GADMIN]
      也不含 [TOOL]。
  G10 只执行一次、不静默丢失：循环开时旧分发处不再执行任何标记（无
      AGENT_GADMIN_EXEC）；循环续写抛错退回剥行文本 → 已执行的那次照旧、
      不再执行第二遍、标记不漏；_tool_loop_run 抛错 → 标记剥掉而不执行；
      循环 final 里旧习惯又写出的 [GADMIN] 行照旧只剥掉。

  不变量钉法：参数与 op 一致＝G5 对照（解析与归类复用同一个
  _gadmin_parse_mark，源码级钉两处调用点）；不漏＝G9 的全程扫描＋G6 坏行
  原样保留＋出站拦截（daemon 侧，不在本脚本范围）；闸复用＝G7/G8 全组＋
  源码级（_devctrl_danger_precheck/_tool_auth_hold_text 不写死工具名）；
  循环关逐字不变＝G4＋_smoke_gadmin_chat.py 16 项（跑法保证，断言未改）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录；gadmin_exec、
模型链（call_glm_with_fallback）、daemon.send、_mcp_tools_doc/_mcp_agent_loop
全部打桩——零真实群管动作、零真实发送、零联网。一次性临时数据目录，跑完删。
用法：python BOT_REPORTS/_smoke_toolloop_gadmin.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_gadmin_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import qq_daemon as D  # noqa: E402,F401  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_A = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_bot_agent.py").read_text(
    encoding="utf-8")
SRC_TB = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_tools_builtin.py").read_text(
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
LEAK_ALL = []          # 全程所有发出的文本（G9 不漏扫描用）
A.out = lambda line: ALOG.append(str(line))
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None
D.out = lambda line: None

OWNER_QQ = "10001"
NOT_OWNER = "10002"
GRP_ID = "555001"
CODE = "654321"
KEY_PRIV = "private:" + OWNER_QQ

GADMIN_TABLE = {
    # name: (risk, [必填参数], [可选参数])
    # 定稿表 soft＝恢复性动作（会改状态但随时可反悔）；qq_tools 自 C7 起
    # 有 soft 这一级，闸上与 read 同一支直接执行（G7a/G7a2 钉）。
    "gadmin_ban": ("write", ["group", "member"], ["minutes"]),
    "gadmin_unban": ("soft", ["group", "member"], []),
    "gadmin_whole_ban": ("danger", ["group"], []),
    "gadmin_whole_unban": ("soft", ["group"], []),
    "gadmin_notice": ("write", ["group", "text"], []),
    "gadmin_recall": ("danger", ["group", "member"], ["count"]),
}
SIX = tuple(GADMIN_TABLE)


def fake_search(q):
    return ("x 是测试条目（example.com）", "bing")


# ============ G1 登记（定稿表；两个 fn 都传才登记；幂等） ============

TB.register_builtin(fake_search)
chk("G1a 不传 gadmin 两个 fn：六个 gadmin_* 都不登记（web_search 照常）",
    all(T.get_tool(n) is None for n in SIX)
    and T.get_tool("web_search") is not None,
    [n for n in SIX if T.get_tool(n) is not None])

TB.register_builtin(fake_search, gadmin_exec_fn=A._gadmin_tool_call,
                    gadmin_enabled_fn=A._gadmin_tool_enabled)
bad1 = []
for n, (risk, req, opt) in GADMIN_TABLE.items():
    spec = T.get_tool(n)
    if spec is None:
        bad1.append((n, "缺"))
        continue
    got_req = [p["name"] for p in spec["params"] if p["required"]]
    got_opt = [p["name"] for p in spec["params"] if not p["required"]]
    if (spec["risk"], got_req, got_opt, spec["scope"]) != \
            (risk, req, opt, "owner"):
        bad1.append((n, (spec["risk"], got_req, got_opt, spec["scope"])))
chk("G1b 传了才登记；六个的 risk/scope/必填可选参数与 C7 定稿表一致",
    not bad1, bad1)

_try_dup = ""
try:
    TB.register_builtin(fake_search, gadmin_exec_fn=A._gadmin_tool_call,
                        gadmin_enabled_fn=A._gadmin_tool_enabled)
    TB.register_builtin(fake_search, gadmin_exec_fn=A._gadmin_tool_call,
                        gadmin_enabled_fn=A._gadmin_tool_enabled)
except Exception as exc:
    _try_dup = repr(exc)
chk("G1c 连调两次不抛、表里各恰好一个",
    _try_dup == ""
    and all(sum(1 for s in T.tools_for({"is_owner": True})
                if s["name"] == n) == 1 for n in SIX), _try_dup)

chk("G1d main() 把 _gadmin_tool_call/_gadmin_tool_enabled 接进 register_builtin",
    "gadmin_exec_fn=_gadmin_tool_call" in SRC_A
    and "gadmin_enabled_fn=_gadmin_tool_enabled" in SRC_A, "")

chk("G1e 描述如实（不夸大）：六个 desc 都在、不出现「保证/一定」",
    all(str(T.get_tool(n).get("desc") or "").strip() for n in SIX)
    and not any(w in "".join(T.get_tool(n)["desc"] for n in SIX)
                for w in ("保证", "一定成功")), "")


# ============ 打桩：daemon / 模型链 / 消息 ============

class RunDaemon(object):
    """gadmin_exec 记参可编排结果；send 返回 ok 真（复述/授权落表要它）；
    其余方法无副作用空操作。"""

    def __init__(self, gadmin_results=None):
        self._gadmin_results = list(gadmin_results) if gadmin_results else None
        self.gadmin_calls = []
        self.acked = []
        self.sent = []
        self.reactions = []
        self.quiets = []

    def history(self, chat_type, chat_id, limit=30):
        return []

    def group_names(self):
        return {GRP_ID: "冒烟一群"}

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))
        LEAK_ALL.extend(str(t) for t in texts)
        return {"ok": True, "queued": 1}

    def set_msg_reaction(self, message_id, emoji_id, on=True):
        self.reactions.append((message_id, emoji_id, on))
        return True, "ok"

    def set_quiet(self, minutes):
        self.quiets.append(minutes)
        return {"ok": True}

    def gadmin_exec(self, op, group_id, who="", minutes=0, count=1,
                    content="", on=True):
        self.gadmin_calls.append(dict(op=op, group_id=group_id, who=who,
                                      minutes=minutes, count=count,
                                      content=content, on=on))
        if self._gadmin_results:
            item = self._gadmin_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"ok": True, "detail": "已办妥（冒烟桩）"}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


class NullDaemon(RunDaemon):
    pass


def norm_gc(c):
    """桩收到的调用 → daemon.gadmin_exec 真正发往 /gadmin-exec 的请求形状
    （op/群/人/分钟/条数/公告文本/on；数值照它自己的 int(...) 归一）。"""
    return (c["op"], str(c["group_id"]), str(c["who"] or ""),
            int(c["minutes"] or 0), int(c["count"] or 1),
            str(c["content"] or ""), bool(c["on"]))


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
    return owner_msg(chat_type="group", chat_id=GRP_ID, chat_name="冒烟一群",
                     sender_id=NOT_OWNER, sender_name="张三", at_me=True,
                     trigger_reason="at", **kw)


def make_chain(outputs):
    calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append([dict(m) for m in messages])
        item = outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return "mx", item

    return _chain, calls


_RC_PAD = [0]
RC_PATH = os.path.join(A.DATA_DIR, "reply_config.json")


def set_cfg(loop_on, confirm_write=False):
    _RC_PAD[0] += 1   # 变文件大小 → load_json_const 的 mtime+size 签名失效
    pathlib.Path(RC_PATH).write_text(
        json.dumps({"tool_loop_enabled": bool(loop_on),
                    "tool_confirm_write": bool(confirm_write)},
                   ensure_ascii=False, indent=2) + ("\n" * _RC_PAD[0]),
        encoding="utf-8")


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


def reset_dev(code=CODE, ok_until=0.0):
    """授权码三件套复位（码是测试假值；窗口/索码全关）。"""
    A._DEVCTRL_AUTH["ok_until"] = ok_until
    A._DEVCTRL_ASK["until"] = 0.0
    A._DEVCTRL_CODE.update({"code": code, "expires": time.time() + 300})
    A._DEVCTRL_SESSION.update({"msgs": [], "noticed": False})


def clear_pending():
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING.clear()


def alog_of(tag):
    return [l for l in ALOG if tag in l]


def joined_of(dm):
    return "".join(t for ts in dm.sent for t in ts)


_M_STUB_DOC = lambda: ""
_M_STUB_OLD_LOOP = lambda d, m, r, t: None
_N_MSG = [0]


def run_process(msg, outputs, dm, loop_on=True, confirm_write=False,
                twofa=True, gadmin_on=True, dev_on=True,
                patch_extra=None):
    """走真实 process()：模型链、视觉、搜索、贴图上限、旧 MCP 面、群管开关
    全打桩。返回 (dm, 模型调用快照)。"""
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
                  _device_ctrl_enabled=lambda: dev_on,
                  _gadmin_chat_enabled=lambda: gadmin_on,
                  _mcp_tools_doc=_M_STUB_DOC,
                  _mcp_agent_loop=_M_STUB_OLD_LOOP)
    if patch_extra:
        _force.update(patch_extra)
    with patched(A, **_force):
        reset_cap()
        ALOG.clear()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
            LEAK_ALL.extend(str(t) for t in dm.sent)
    return dm, calls


def avail_names():
    """主人视角、过 _tool_available 过滤后的清单名（与 _tool_doc_block/
    _tool_loop_run 同一道过滤）。"""
    return [s["name"] for s in T.tools_for({"is_owner": True, "is_gadmin": False})
            if A._tool_available(s.get("name"))]


def run_named(name, args, opts=None, is_owner=True):
    m = owner_msg()
    ctx = {"daemon": RunDaemon(), "msg": m, "is_owner": is_owner,
           "is_gadmin": False}
    return T.run_tool(T.get_tool(name), args, ctx, opts or {}), ctx["daemon"]


# ============ G2 群管开关关：不进清单/循环；写了回「没有这个工具」 ============

with patched(A, _gadmin_chat_enabled=lambda: False):
    g2a = (all(not A._tool_available(n) for n in SIX)
           and A._tool_available("web_search") and A._tool_available("react"))
    g2b = avail_names()
    doc_off = A._tool_doc_block()
chk("G2a 开关关：_tool_available 对 gadmin_* 全 False、对其它工具 True", g2a, "")
chk("G2b 开关关：主人可用清单里没有任何 gadmin_*",
    not [n for n in g2b if n.startswith("gadmin_")], g2b)
chk("G2c 开关关：工具教学清单里没有 gadmin_ban（不列出）",
    "gadmin_ban" not in doc_off and "你能用的工具" in doc_off, doc_off[:120])
r2, dm2 = run_named("gadmin_ban", {"group": GRP_ID, "member": "张三"},
                    opts={"auth_ok": True})
chk("G2d/I2 开关关：gadmin_ban 不执行（桩零调用）、结果=「对话群管没有开启（管理台 → 群管 里打开）」",
    r2["ok"] is False
    and r2["summary"] == "对话群管没有开启（管理台 → 群管 里打开）"
    and dm2.gadmin_calls == [], (r2["summary"], dm2.gadmin_calls))
r2b, dm2b = run_named("gadmin_whole_ban", {"group": GRP_ID},
                      opts={"auth_ok": True})
chk("G2e/I2 开关关：danger 的 gadmin_whole_ban 同样不执行、同样那句话",
    r2b["ok"] is False and "对话群管没有开启" in r2b["summary"]
    and dm2b.gadmin_calls == [], (r2b["summary"],))

# 开关关 + 循环开：模型凭空写 [TOOL] gadmin_ban → 循环按「没有这个工具」回喂
reset_dev()
_dm2f = RunDaemon()
_N_MSG[0] += 1
_dm2f, _c2f = run_process(
    owner_msg(id="f%d" % _N_MSG[0], message_id="f%d" % _N_MSG[0],
              text="帮我把张三禁言"),
    ['[TOOL] gadmin_ban {"group": "%s", "member": "张三"}' % GRP_ID,
     "主人，现在群管没开，办不了"], dm=_dm2f, loop_on=True, gadmin_on=False)
_feed2 = _c2f[1][-1].get("content") if len(_c2f) > 1 else ""
chk("G2f/I2 开关关＋循环开：模型凭空写 → 回喂「没有这个工具」、桩零调用、不发工具行",
    "没有这个工具：gadmin_ban" in _feed2 and _dm2f.gadmin_calls == []
    and "[TOOL]" not in joined_of(_dm2f), (_feed2[-120:], _dm2f.gadmin_calls))

# 开关关 + 循环开 + 模型按旧习惯写 [GADMIN] 标记：旧段照旧整句替换（逐字不变）
reset_dev()
_dm2g = RunDaemon()
_N_MSG[0] += 1
_dm2g, _ = run_process(
    owner_msg(id="g%d" % _N_MSG[0], message_id="g%d" % _N_MSG[0],
              text="把张三禁言一下"),
    ["好嘞这就办\n[GADMIN] 禁言|群=%s|人=张三|分钟=10" % GRP_ID],
    dm=_dm2g, loop_on=True, gadmin_on=False)
chk("G2g/I2 循环开＋群管开关关：标记照旧剥掉、整句替换「对话群管没有开启」、桩零调用",
    _dm2g.gadmin_calls == [] and joined_of(_dm2g).strip() == "对话群管没有开启",
    (joined_of(_dm2g)[:80], _dm2g.gadmin_calls))

# ============ G3 教学与清单（循环开不注入旧教学；循环关照旧） ============

OLD_ANCHOR = "群管理能力（只有主人可见的私密教学"


def build_sys(msg, loop_on, gadmin_on):
    with patched(A, _tool_loop_on=lambda: loop_on,
                 _gadmin_chat_enabled=lambda: gadmin_on,
                 PROMPT_V2_FORCE=True, owner_qq=lambda: OWNER_QQ):
        msgs, _pname = A.build_messages(RunDaemon(), msg, ctx_k=32)
    return msgs[0]["content"]


s1 = build_sys(owner_msg(text="帮我管下群"), True, True)
chk("G3a/I2 循环开+主人+群管开：旧教学不在、工具清单列出六个 gadmin_*",
    OLD_ANCHOR not in s1 and "[GADMIN] 禁言|群=群号" not in s1
    and all(("%s(" % n) in s1 for n in SIX),
    (OLD_ANCHOR in s1, [n for n in SIX if ("%s(" % n) not in s1]))
s2 = build_sys(owner_msg(text="帮我管下群"), False, True)
chk("G3b/I2 循环关+主人+群管开：旧教学照旧注入（与源码原文同文）",
    OLD_ANCHOR in s2 and "[GADMIN] 禁言|群=群号" in s2
    and "gadmin_ban(" not in s2, (OLD_ANCHOR in s2,))
s3 = build_sys(owner_msg(text="帮我管下群"), True, False)
s4 = build_sys(owner_msg(text="帮我管下群"), False, False)
chk("G3c 开关关（两种循环状态）：旧教学本就空串、清单里也没有 gadmin_*",
    OLD_ANCHOR not in s3 and "gadmin_ban(" not in s3
    and OLD_ANCHOR not in s4 and "gadmin_ban(" not in s4, "")
s5a = build_sys(non_owner_msg(text="帮我管下群"), True, True)
s5b = build_sys(non_owner_msg(text="帮我管下群"), False, True)
chk("G3d/I4 非主人：两种循环状态下都没有旧教学、清单里没有 gadmin_*",
    OLD_ANCHOR not in s5a and "gadmin_ban(" not in s5a
    and OLD_ANCHOR not in s5b and "gadmin_ban(" not in s5b, "")

with patched(A, _gadmin_chat_enabled=lambda: True):
    g3e = avail_names()
    doc_on = A._tool_doc_block()
chk("G3e 开关开：清单与工具教学里列出全部六个",
    set(SIX) <= set(g3e) and all((n in doc_on) for n in SIX),
    ([n for n in SIX if n not in g3e], [n for n in SIX if n not in doc_on]))


# ============ G4 工具循环关：旧标记照旧直接执行（对照基线） ============

BASE_MARKS = [
    ("[GADMIN] 禁言|群=%s|人=张三|分钟=10" % GRP_ID,
     ("ban", "555001", "张三", 10, 1, "", True)),
    ("[GADMIN] 解禁|群=%s|人=张三" % GRP_ID,
     ("unban", "555001", "张三", 0, 1, "", True)),
    ("[GADMIN] 全体禁言|群=%s|开=1" % GRP_ID,
     ("whole", "555001", "", 0, 1, "", True)),
    ("[GADMIN] 全体禁言|群=%s|开=0" % GRP_ID,
     ("whole", "555001", "", 0, 1, "", False)),
    ("[GADMIN] 公告|群=%s|内容=今晚维护" % GRP_ID,
     ("notice", "555001", "", 0, 1, "今晚维护", True)),
    ("[GADMIN] 撤回|群=%s|人=李四|条数=3" % GRP_ID,
     ("recall", "555001", "李四", 0, 3, "", True)),
]

reset_dev()
OLD_BASE = []
for _i, (mark, want) in enumerate(BASE_MARKS):
    dm4 = RunDaemon()
    _N_MSG[0] += 1
    dm4, _ = run_process(
        owner_msg(id="o%d" % _N_MSG[0], message_id="o%d" % _N_MSG[0],
                  text="帮我管群%d" % _i),
        ["好嘞主人，这就办\n" + mark], dm=dm4, loop_on=False, gadmin_on=True)
    OLD_BASE.append([norm_gc(c) for c in dm4.gadmin_calls])
    if [norm_gc(c) for c in dm4.gadmin_calls] != [want]:
        chk("G4 循环关基线 %s" % mark[:14], False,
            (dm4.gadmin_calls, want))
        break
else:
    chk("G4a/I2 循环关：六个基线标记照旧直接执行（桩收到的 op/参数与旧分发一致）",
        True, "")
chk("G4b/I2 循环关：日志走旧分发（AGENT_GADMIN_EXEC）、标记剥掉不漏",
    any("AGENT_GADMIN_EXEC" in l for l in ALOG)
    and all("[GADMIN]" not in t for t in LEAK_ALL), alog_of("AGENT_GADMIN_EXEC"))

# ============ G5 旧标记转换（端到端）＋两种模式对照 ============

reset_dev()
NEW_RUNS = []
_g5_log_ok = True
_g5_log_why = []
for _i, (mark, want) in enumerate(BASE_MARKS):
    dm5 = RunDaemon()
    _N_MSG[0] += 1
    # twofa=False：danger 的两个基线标记在「对照」场景里不该被授权闸拦下
    # （授权闸本身是 G7/G8 的事）
    dm5, calls5 = run_process(
        owner_msg(id="n%d" % _N_MSG[0], message_id="n%d" % _N_MSG[0],
                  text="帮我管群%d" % _i),
        ["好嘞主人，这就办\n" + mark, "办好了主人"], dm=dm5,
        loop_on=True, gadmin_on=True, twofa=False)
    got = [norm_gc(c) for c in dm5.gadmin_calls]
    NEW_RUNS.append(got)
    chk("G5-%d 转换后经工具执行：%s → 桩收到 %s" % (_i, mark[:16], (want[0], want[6])),
        got == [want], (got, want))
    if not (len(got) == 1 and not alog_of("AGENT_GADMIN_EXEC")
            and any("AGENT_TOOL_LOOP" in l and ":成功" in l
                    for l in alog_of("AGENT_TOOL_LOOP"))):
        _g5_log_ok = False
        _g5_log_why.append((_i, len(got), alog_of("AGENT_GADMIN_EXEC")[:1],
                            alog_of("AGENT_TOOL_LOOP")[:1]))
chk("G5a 对照：同一份标记，循环开（经工具）与循环关（旧分发）喂给 gadmin_exec 的请求完全相同",
    NEW_RUNS == OLD_BASE,
    [(a, b) for a, b in zip(NEW_RUNS, OLD_BASE) if a != b])
chk("G5b 只执行一次：每条标记恰好 1 次调用、走循环日志、无 AGENT_GADMIN_EXEC",
    _g5_log_ok, _g5_log_why)

# 无参数行单独跑（公告没有内容键）
reset_dev()
_dm5d = RunDaemon()
_N_MSG[0] += 1
_dm5d, _ = run_process(
    owner_msg(id="n9%d" % _N_MSG[0], message_id="n9%d" % _N_MSG[0],
              text="发个公告"),
    ["好嘞\n[GADMIN] 公告|群=%s" % GRP_ID, "发了主人"], dm=_dm5d,
    loop_on=True, gadmin_on=True)
_dm5e = RunDaemon()
_N_MSG[0] += 1
_dm5e, _ = run_process(
    owner_msg(id="n9%d" % _N_MSG[0], message_id="n9%d" % _N_MSG[0],
              text="发个公告2"),
    ["好嘞\n[GADMIN] 公告|群=%s" % GRP_ID], dm=_dm5e,
    loop_on=False, gadmin_on=True)
chk("G5d 无参数行（只有群键）：两种模式对照一致（notice/群/内容空）",
    [norm_gc(c) for c in _dm5d.gadmin_calls] ==
    [norm_gc(c) for c in _dm5e.gadmin_calls]
    == [("notice", "555001", "", 0, 1, "", True)],
    (_dm5d.gadmin_calls, _dm5e.gadmin_calls))

# ============ G6 转换函数单测 ============

_conv = A._gadmin_lines_to_calls
_want_ban = '[TOOL] gadmin_ban %s' % json.dumps(
    {"group": "555001", "member": "张三", "minutes": "10"}, ensure_ascii=False)
_want_unban = '[TOOL] gadmin_unban %s' % json.dumps(
    {"group": "555001", "member": "张三"}, ensure_ascii=False)
chk("G6a 转换：禁言行 → [TOOL] gadmin_ban（参数键换成登记表名字，值原样）",
    _conv("[GADMIN] 禁言|群=555001|人=张三|分钟=10") == _want_ban,
    _conv("[GADMIN] 禁言|群=555001|人=张三|分钟=10"))
chk("G6b 转换：解禁行 → gadmin_unban；全体禁言 开=0 → gadmin_whole_unban",
    _conv("[GADMIN] 解禁|群=555001|人=张三") == _want_unban
    and _conv("[GADMIN] 全体禁言|群=555001|开=0")
    == '[TOOL] gadmin_whole_unban {"group": "555001"}',
    (_conv("[GADMIN] 全体禁言|群=555001|开=0"),))
chk("G6c 转换：全体禁言 缺「开」→ gadmin_whole_ban（与旧分发处同一缺省=开）",
    _conv("[GADMIN] 全体禁言|群=555001")
    == '[TOOL] gadmin_whole_ban {"group": "555001"}',
    _conv("[GADMIN] 全体禁言|群=555001"))
chk("G6d 转换：全角＝赋值照旧解析（旧分发处同一套）",
    _conv("[GADMIN] 公告|群=555001|内容＝今晚维护")
    == '[TOOL] gadmin_notice %s' % json.dumps(
        {"group": "555001", "text": "今晚维护"}, ensure_ascii=False),
    _conv("[GADMIN] 公告|群=555001|内容＝今晚维护"))
chk("G6e 坏行原样保留（正则匹配不了/认不出动作/缺竖线/行中间提到）",
    _conv("[GADMIN] 踢人|群=1|人=x") == "[GADMIN] 踢人|群=1|人=x"
    and _conv("[GADMIN] 撤禁|群=1") == "[GADMIN] 撤禁|群=1"
    and _conv("[GADMIN] 全体禁言") == "[GADMIN] 全体禁言"
    and _conv("另外 [GADMIN] 禁言|群=1|人=x 没动") == "另外 [GADMIN] 禁言|群=1|人=x 没动",
    (_conv("[GADMIN] 撤禁|群=1"),))
chk("G6e2 动作归类与旧分发处同一套：公告撤回在旧分发里就归 notice（共用解析）",
    _conv("[GADMIN] 公告撤回|群=1")
    == '[TOOL] gadmin_notice %s' % json.dumps({"group": "1", "text": ""},
                                              ensure_ascii=False),
    _conv("[GADMIN] 公告撤回|群=1"))
chk("G6f 多行混合：[TOOL] 行与普通行不动、只有 [GADMIN] 行转换",
    _conv('[TOOL] web_search {"query": "x"}\n普通话\n[GADMIN] 解禁|群=555001|人=张三')
    == '[TOOL] web_search {"query": "x"}\n普通话\n' + _want_unban, "")
chk("G6g 坏行端到端：循环开时只剥不执行、不漏、走 AGENT_GADMIN_VIA_TOOL",
    True, "")   # 占位对齐（真正的断言在 G10d）

# ============ G7 三个风险级别各走各的闸 ============

# soft：confirm_write 开着也直接执行
reset_dev()
_g7a2 = []
for _n in ("gadmin_unban", "gadmin_whole_unban"):
    _d, _why = T.gate(T.get_tool(_n),
                      {"is_owner": True, "is_gadmin": False},
                      {"confirm_write": True, "auth_ok": False})
    _g7a2.append((_n, _d, _why))
chk("G7a2 gate 级钉死：两个 soft 工具在复述确认开＋授权码开关开（没授权）时都直接 run",
    all(d == "run" for _n, d, _w in _g7a2), _g7a2)
_dm7a = RunDaemon()
_N_MSG[0] += 1
_dm7a, _ = run_process(
    owner_msg(id="s%d" % _N_MSG[0], message_id="s%d" % _N_MSG[0],
              text="帮张三解禁"),
    ['[TOOL] gadmin_unban {"group": "%s", "member": "张三"}' % GRP_ID,
     "解开了主人"], dm=_dm7a, loop_on=True, confirm_write=True, gadmin_on=True)
chk("G7a soft 直接执行（confirm_write 开着也一样）",
    len(_dm7a.gadmin_calls) == 1 and _dm7a.gadmin_calls[0]["op"] == "unban"
    and "回复「确认」我就执行" not in joined_of(_dm7a),
    (_dm7a.gadmin_calls, joined_of(_dm7a)[:80]))

# write：confirm_write 开 → 复述确认，确认后续跑执行恰好一次
reset_dev()
_dm7b = RunDaemon()
_N_MSG[0] += 1
_dm7b, _ = run_process(
    owner_msg(id="w%d" % _N_MSG[0], message_id="w%d" % _N_MSG[0],
              text="禁言张三"),
    ['[TOOL] gadmin_ban {"group": "%s", "member": "张三", "minutes": "10"}'
     % GRP_ID, "占位不该被消费"],
    dm=_dm7b, loop_on=True, confirm_write=True, gadmin_on=True)
_ent7b = A._TOOL_PENDING.get(KEY_PRIV)
chk("G7b write 受复述确认保护：要确认文案、桩零调用、待确认落表（gadmin_ban）",
    "回复「确认」我就执行" in joined_of(_dm7b)
    and "禁言群里的某个成员" in joined_of(_dm7b)
    and _dm7b.gadmin_calls == [] and _ent7b is not None
    and [c.get("tool") for c in (_ent7b.get("calls") or [])] == ["gadmin_ban"],
    (joined_of(_dm7b)[:120], _dm7b.gadmin_calls))
_N_MSG[0] += 1
_dm7b2 = RunDaemon()
_dm7b2, _ = run_process(
    owner_msg(id="w%d" % _N_MSG[0], message_id="w%d" % _N_MSG[0], text="确认"),
    ["办好了主人"], dm=_dm7b2, loop_on=True, confirm_write=True, gadmin_on=True)
chk("G7c write 确认后续跑：桩恰好 1 次（ban/张三/10 分钟）、不再重复要确认",
    [norm_gc(c) for c in _dm7b2.gadmin_calls]
    == [("ban", "555001", "张三", 10, 1, "", True)]
    and "回复「确认」我就执行" not in joined_of(_dm7b2)
    and "办好了主人" in joined_of(_dm7b2)
    and A._TOOL_PENDING.get(KEY_PRIV) is None,
    (_dm7b2.gadmin_calls, joined_of(_dm7b2)[:100]))

# write：confirm_write 关 → 直接执行
reset_dev()
_dm7c = RunDaemon()
_N_MSG[0] += 1
_dm7c, _ = run_process(
    owner_msg(id="w%d" % _N_MSG[0], message_id="w%d" % _N_MSG[0],
              text="发个公告"),
    ['[TOOL] gadmin_notice {"group": "%s", "text": "今晚维护"}' % GRP_ID,
     "发了主人"], dm=_dm7c, loop_on=True, confirm_write=False, gadmin_on=True)
chk("G7d write 确认开关关：直接执行（notice/内容一致）",
    [norm_gc(c) for c in _dm7c.gadmin_calls]
    == [("notice", "555001", "", 0, 1, "今晚维护", True)],
    _dm7c.gadmin_calls)

# danger：授权码开关开（窗口没开）→ 要码文本、不执行、落授权类待确认
reset_dev()
_dm7d = RunDaemon()
_N_MSG[0] += 1
_dm7d, _c7d = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0],
              text="全体禁言"),
    ['[TOOL] gadmin_whole_ban {"group": "%s"}' % GRP_ID, "占位不该被消费"],
    dm=_dm7d, loop_on=True, twofa=True, gadmin_on=True)
_j7d = joined_of(_dm7d)
chk("G7e danger 授权码开关开：确定性要码文本（逐行列出该工具与参数）、桩零调用",
    "需要授权码" in _j7d and "授权后我会执行" in _j7d
    and 'gadmin_whole_ban({"group": "%s"})' % GRP_ID in _j7d
    and "不想做了就回「取消」" in _j7d
    and _dm7d.gadmin_calls == [] and len(_c7d) == 1,
    (_j7d[:200], _dm7d.gadmin_calls, len(_c7d)))
_ent7d = A._TOOL_PENDING.get(KEY_PRIV)
chk("G7f 落授权类待确认：kind=auth、calls=这一轮调用、first_reply=原始回复",
    _ent7d is not None and _ent7d.get("kind") == "auth"
    and [c.get("tool") for c in (_ent7d.get("calls") or [])] == ["gadmin_whole_ban"]
    and (_ent7d.get("calls") or [{}])[0].get("args") == {"group": GRP_ID},
    _ent7d and {k: _ent7d.get(k) for k in ("kind", "calls")})
_ask7 = bool(alog_of("AGENT_DEVC_ASK src=tool"))
chk("G7e2 索码来自预检（AGENT_DEVC_ASK src=tool，只置窗口不发消息）", _ask7,
    alog_of("AGENT_DEVC_ASK"))
_N_MSG[0] += 1
_dm7d2 = RunDaemon()
_dm7d2, _c7d2 = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0], text=CODE),
    ["开好了主人"], dm=_dm7d2, loop_on=True, twofa=True, gadmin_on=True)
chk("G7g danger 发对授权码：执行恰好一次、待确认清空、无论文术、主模型没调",
    [norm_gc(c) for c in _dm7d2.gadmin_calls]
    == [("whole", "555001", "", 0, 1, "", True)]
    and A._TOOL_PENDING.get(KEY_PRIV) is None
    and "连线成功" not in joined_of(_dm7d2) and len(_c7d2) == 1,
    (_dm7d2.gadmin_calls, len(_c7d2)))
chk("G7h danger 续跑日志 AGENT_TOOL_AUTH_GO（带 gadmin_whole_ban）",
    any("AGENT_TOOL_AUTH_GO" in l and "gadmin_whole_ban" in l
        for l in alog_of("AGENT_TOOL_AUTH_GO")),
    alog_of("AGENT_TOOL_AUTH_GO"))

# danger：错码 → 拒、待确认仍在、不执行
reset_dev()
_dm7i = RunDaemon()
_N_MSG[0] += 1
_dm7i, _ = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0], text="撤回消息"),
    ['[TOOL] gadmin_recall {"group": "%s", "member": "李四", "count": "3"}'
     % GRP_ID, "占位不该被消费"],
    dm=_dm7i, loop_on=True, twofa=True, gadmin_on=True)
_N_MSG[0] += 1
_dm7i2 = RunDaemon()
_dm7i2, _c7i2 = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0],
              text="000000"), [], dm=_dm7i2, loop_on=True, twofa=True,
    gadmin_on=True)
_ent7i = A._TOOL_PENDING.get(KEY_PRIV)
chk("G7i danger 错码：拒绝话术、窗口不开、不执行、待确认仍在（gadmin_recall）",
    "授权码有误" in joined_of(_dm7i2)
    and A._DEVCTRL_AUTH["ok_until"] == 0.0 and _dm7i2.gadmin_calls == []
    and _c7i2 == [] and _ent7i is not None and _ent7i.get("kind") == "auth"
    and [c.get("tool") for c in (_ent7i.get("calls") or [])] == ["gadmin_recall"],
    (joined_of(_dm7i2)[:100], _dm7i2.gadmin_calls, len(_c7i2)))
_N_MSG[0] += 1
_dm7i3 = RunDaemon()
_dm7i3, _ = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0], text=CODE),
    ["撤好了主人"], dm=_dm7i3, loop_on=True, twofa=True, gadmin_on=True)
chk("G7j danger 再发对的码：执行恰好一次（recall/李四/3 条）",
    [norm_gc(c) for c in _dm7i3.gadmin_calls]
    == [("recall", "555001", "李四", 0, 3, "", True)],
    _dm7i3.gadmin_calls)

# danger：授权码开关关 → 直接执行
reset_dev()
_dm7k = RunDaemon()
_N_MSG[0] += 1
_dm7k, _ = run_process(
    owner_msg(id="d%d" % _N_MSG[0], message_id="d%d" % _N_MSG[0],
              text="全体禁言"),
    ['[TOOL] gadmin_whole_ban {"group": "%s"}' % GRP_ID, "开好了主人"],
    dm=_dm7k, loop_on=True, twofa=False, gadmin_on=True)
chk("G7k danger 授权码开关关：直接执行、无索码（I3 同设备操控）",
    [norm_gc(c) for c in _dm7k.gadmin_calls]
    == [("whole", "555001", "", 0, 1, "", True)]
    and not alog_of("AGENT_DEVC_ASK"), (_dm7k.gadmin_calls,))

# ============ G8 同一轮两种闸 ============

# 授权没过：danger 在场 → 整轮不进循环（要码文本列出两个调用、都落授权类）
reset_dev()
_dm8a = RunDaemon()
_N_MSG[0] += 1
_dm8a, _c8a = run_process(
    owner_msg(id="m%d" % _N_MSG[0], message_id="m%d" % _N_MSG[0],
              text="禁言并全体禁言"),
    ['[TOOL] gadmin_notice {"group": "%s", "text": " maintenance"}' % GRP_ID
     + '\n[TOOL] gadmin_whole_ban {"group": "%s"}' % GRP_ID,
     "占位不该被消费"],
    dm=_dm8a, loop_on=True, confirm_write=True, twofa=True, gadmin_on=True)
_j8a = joined_of(_dm8a)
_ent8a = A._TOOL_PENDING.get(KEY_PRIV)
chk("G8a 同轮 write+danger、授权没过：整轮不进循环（要码文本、两个都列、复述没发生）",
    "需要授权码" in _j8a and "gadmin_notice(" in _j8a
    and "gadmin_whole_ban(" in _j8a
    and "回复「确认」我就执行" not in _j8a
    and _dm8a.gadmin_calls == [] and len(_c8a) == 1
    and _ent8a is not None and _ent8a.get("kind") == "auth"
    and [c.get("tool") for c in (_ent8a.get("calls") or [])]
    == ["gadmin_notice", "gadmin_whole_ban"],
    (_j8a[:160], _dm8a.gadmin_calls, _ent8a and [c.get("tool") for c in _ent8a.get("calls") or []]))
_N_MSG[0] += 1
_dm8a2 = RunDaemon()
_dm8a2, _ = run_process(
    owner_msg(id="m%d" % _N_MSG[0], message_id="m%d" % _N_MSG[0], text=CODE),
    ["都办好了主人"], dm=_dm8a2, loop_on=True, twofa=True, gadmin_on=True)
chk("G8b 发对授权码：待确认的两个调用按序执行恰好一次（write+danger）",
    [norm_gc(c) for c in _dm8a2.gadmin_calls]
    == [("notice", "555001", "", 0, 1, " maintenance", True),
        ("whole", "555001", "", 0, 1, "", True)]
    and A._TOOL_PENDING.get(KEY_PRIV) is None,
    (_dm8a2.gadmin_calls,))

# 授权窗口开着：进循环，write 停在复述；确认后 write 与 danger 都执行
reset_dev(ok_until=time.time() + 120)
_dm8c = RunDaemon()
_N_MSG[0] += 1
_dm8c, _ = run_process(
    owner_msg(id="m%d" % _N_MSG[0], message_id="m%d" % _N_MSG[0],
              text="禁言并全体禁言"),
    ['[TOOL] gadmin_ban {"group": "%s", "member": "张三", "minutes": "10"}'
     % GRP_ID + '\n[TOOL] gadmin_whole_ban {"group": "%s"}' % GRP_ID,
     "占位不该被消费"],
    dm=_dm8c, loop_on=True, confirm_write=True, twofa=True, gadmin_on=True)
_ent8c = A._TOOL_PENDING.get(KEY_PRIV)
chk("G8c 同轮 write+danger、窗口开着：write 停在复述（danger 还没轮到、未执行）",
    "回复「确认」我就执行" in joined_of(_dm8c)
    and _dm8c.gadmin_calls == []
    and _ent8c is not None and _ent8c.get("kind") is None
    and [c.get("tool") for c in (_ent8c.get("calls") or [])]
    == ["gadmin_ban", "gadmin_whole_ban"],
    (joined_of(_dm8c)[:120], _dm8c.gadmin_calls,
     _ent8c and [c.get("tool") for c in _ent8c.get("calls") or []]))
_N_MSG[0] += 1
_dm8c2 = RunDaemon()
_dm8c2, _ = run_process(
    owner_msg(id="m%d" % _N_MSG[0], message_id="m%d" % _N_MSG[0], text="确认"),
    ["都办好了主人"], dm=_dm8c2, loop_on=True, confirm_write=True,
    twofa=True, gadmin_on=True)
chk("G8d 确认后续跑：write 不再要确认、danger 走窗口授权，两个恰好各一次",
    [norm_gc(c) for c in _dm8c2.gadmin_calls]
    == [("ban", "555001", "张三", 10, 1, "", True),
        ("whole", "555001", "", 0, 1, "", True)]
    and "回复「确认」我就执行" not in joined_of(_dm8c2),
    (_dm8c2.gadmin_calls, joined_of(_dm8c2)[:100]))

# ============ G9 失败如实 + 全程不漏 ============

reset_dev()
_dm9a = RunDaemon(gadmin_results=[{"ok": False, "detail": "没找到这个人"}])
_N_MSG[0] += 1
_dm9a, _c9a = run_process(
    owner_msg(id="x%d" % _N_MSG[0], message_id="x%d" % _N_MSG[0],
              text="禁言张三"),
    ['[TOOL] gadmin_ban {"group": "%s", "member": "张三"}' % GRP_ID,
     "主人，没办成"], dm=_dm9a, loop_on=True, gadmin_on=True)
_feed9 = _c9a[1][-1].get("content") if len(_c9a) > 1 else ""
chk("G9a 失败如实：回喂带「没做成：」与原因（只标一次）、日志 gadmin_ban:失败",
    "没做成：没找到这个人" in _feed9
    and "没做成：没做成：" not in _feed9
    and any("AGENT_TOOL_LOOP" in l and "gadmin_ban:失败" in l
            for l in alog_of("AGENT_TOOL_LOOP")),
    (_feed9[-200:], alog_of("AGENT_TOOL_LOOP")))
chk("G9b 失败态发出的文本不含 [GADMIN]/[TOOL]",
    "[GADMIN]" not in joined_of(_dm9a) and "[TOOL]" not in joined_of(_dm9a),
    joined_of(_dm9a)[:120])


def _boom(daemon, op, args):
    raise RuntimeError("坏" * 120)


_r9c = TB._gadmin_run(_boom, lambda: True, "ban", {})(None, None,
                                                      {"group": GRP_ID})
chk("G9c 执行函数抛异常：ok=False、「群管执行出错：」且原因截 200 字",
    _r9c["ok"] is False and _r9c["summary"].startswith("群管执行出错：")
    and len(_r9c["summary"]) <= len("群管执行出错：") + 200,
    len(_r9c["summary"]))

_r9d = TB._gadmin_run(lambda d, o, a: {"ok": True, "detail": "已办妥（冒烟桩）"},
                      lambda: True, "ban", {})(None, None, {"group": GRP_ID})
chk("G9d 成功如实回喂：summary=daemon 拟好的人话原文",
    _r9d["ok"] is True and _r9d["summary"] == "已办妥（冒烟桩）", _r9d["summary"])

chk("G9e 全程扫描：所有轮次发出的文本都不含 [GADMIN] 与 [TOOL]",
    LEAK_ALL and all("[GADMIN]" not in t and "[TOOL]" not in t
                     for t in LEAK_ALL),
    [t[:60] for t in LEAK_ALL if "[GADMIN]" in t or "[TOOL]" in t][:3])

# ============ G10 只执行一次、不静默丢失 ============

# 循环续写抛错（stop=error → 退回剥行文本）：已执行的那次照旧、不再执行第二遍
reset_dev()
_dm10a = RunDaemon()
_N_MSG[0] += 1
_dm10a, _ = run_process(
    owner_msg(id="e%d" % _N_MSG[0], message_id="e%d" % _N_MSG[0],
              text="禁言张三"),
    ['好嘞这就办\n[TOOL] gadmin_ban {"group": "%s", "member": "张三"}' % GRP_ID,
     RuntimeError("续写熔断")],
    dm=_dm10a, loop_on=True, gadmin_on=True)
_j10a = joined_of(_dm10a)
chk("G10a 循环续写抛错：工具已执行恰好 1 次、退回剥行文本（无标记、无 AGENT_GADMIN_EXEC）",
    len(_dm10a.gadmin_calls) == 1 and "[TOOL]" not in _j10a
    and "[GADMIN]" not in _j10a and "好嘞这就办" in _j10a
    and not alog_of("AGENT_GADMIN_EXEC")
    and any("AGENT_TOOL_LOOP_ERR" in l for l in ALOG),
    (_dm10a.gadmin_calls, _j10a[:100], alog_of("AGENT_TOOL_LOOP_ERR")))

# _tool_loop_run 整体抛错：标记剥掉而不执行、也不漏出
reset_dev()
_dm10b = RunDaemon()
_N_MSG[0] += 1


def _loop_boom(*a, **k):
    raise RuntimeError("循环熔断")


_dm10b, _ = run_process(
    owner_msg(id="e%d" % _N_MSG[0], message_id="e%d" % _N_MSG[0],
              text="禁言张三"),
    ['好嘞这就办\n[TOOL] gadmin_ban {"group": "%s", "member": "张三"}' % GRP_ID],
    dm=_dm10b, loop_on=True, gadmin_on=True,
    patch_extra={"_tool_loop_run": _loop_boom})
_j10b = joined_of(_dm10b)
chk("G10b/I3 循环体抛错退回剥行文本：标记剥掉而不执行、不漏出",
    _dm10b.gadmin_calls == [] and "[TOOL]" not in _j10b
    and "[GADMIN]" not in _j10b and "好嘞这就办" in _j10b,
    (_dm10b.gadmin_calls, _j10b[:100]))

# 循环 final 里旧习惯又写出 [GADMIN]：照旧只剥掉、不再经旧分发执行
reset_dev()
_dm10c = RunDaemon()
_N_MSG[0] += 1
_dm10c, _ = run_process(
    owner_msg(id="e%d" % _N_MSG[0], message_id="e%d" % _N_MSG[0],
              text="禁言张三"),
    ['好嘞这就办\n[TOOL] gadmin_ban {"group": "%s", "member": "张三"}' % GRP_ID,
     "办好了主人\n[GADMIN] 解禁|群=%s|人=张三" % GRP_ID],
    dm=_dm10c, loop_on=True, gadmin_on=True)
_j10c = joined_of(_dm10c)
chk("G10c/I3 final 里残余 [GADMIN] 行：只剥掉、旧分发不执行、不漏出",
    len(_dm10c.gadmin_calls) == 1 and _dm10c.gadmin_calls[0]["op"] == "ban"
    and "[GADMIN]" not in _j10c and "办好了主人" in _j10c
    and any("AGENT_GADMIN_VIA_TOOL" in l for l in ALOG),
    (_dm10c.gadmin_calls, _j10c[:100]))

# 坏行（认不出动作）端到端：不转换、只剥掉、不执行（G6g 的实体断言）
reset_dev()
_dm10d = RunDaemon()
_N_MSG[0] += 1
_dm10d, _ = run_process(
    owner_msg(id="e%d" % _N_MSG[0], message_id="e%d" % _N_MSG[0],
              text="踢了他"),
    ["这个我做不了\n[GADMIN] 撤禁|群=%s" % GRP_ID],
    dm=_dm10d, loop_on=True, gadmin_on=True)
_j10d = joined_of(_dm10d)
chk("G10d/I3 坏行（认不出动作）：不转换、只剥掉、不执行、不漏出",
    _dm10d.gadmin_calls == [] and "[GADMIN]" not in _j10d
    and "这个我做不了" in _j10d
    and any("AGENT_GADMIN_VIA_TOOL" in l for l in ALOG),
    (_dm10d.gadmin_calls, _j10d[:100]))

# ============ 源码级钉住 ============

chk("S1 源码级：衔接点转换在 parse_calls 之前（owner+循环开+群管开）",
    SRC_A.index("if owner and _tool_loop_on() and _gadmin_chat_enabled()")
    < SRC_A.index("_tool_calls, _tool_rest = qq_tools.parse_calls(reply)")
    and "reply = _gadmin_lines_to_calls(reply)" in SRC_A, "")
chk("S2 源码级：旧分发处收窄——循环开走只剥分支、循环关走原执行分支（逐字保留）",
    "elif _tool_loop_on():" in SRC_A
    and "AGENT_GADMIN_VIA_TOOL" in SRC_A
    and SRC_A.index("elif _tool_loop_on():")
    < SRC_A.index("for _gop, _gargs in _gmarks[:3]:")
    and "for _gop, _gargs in _gmarks[:3]:" in SRC_A, "")
chk("S3 源码级：解析与归类是同一个函数（旧分发处与转换都调 _gadmin_parse_mark）",
    SRC_A.count("_gadmin_parse_mark(") >= 3
    and SRC_A.index("def _gadmin_parse_mark") < SRC_A.index("def _gadmin_lines_to_calls"),
    SRC_A.count("_gadmin_parse_mark("))
chk("S4 源码级：教学跳过条件＝循环开且主人（总开关关时 _gadmin_rule 本就空串）",
    "if not (_tool_loop_on() and _owner_msg):" in SRC_A
    and SRC_A.index("if not (_tool_loop_on() and _owner_msg):")
    < SRC_A.index("system += _gadmin_rule(daemon, msg)"), "")
chk("S5 源码级：_tool_available 有 gadmin_ 前缀分支（照 pc_*、mcp_call 先例）",
    'if n.startswith("gadmin_"):' in SRC_A
    and "return _gadmin_tool_enabled()" in SRC_A, "")
chk("S6 源码级：六个 run 全是同一个薄包装（第一行查开关、只调 exec_fn）",
    SRC_TB.count("def _gadmin_run") == 1
    and SRC_TB.count('"run": _gadmin_run(exec_fn, enabled_fn, ') == 6
    and SRC_TB.count('return qq_tools.make_result(\n                False, "对话群管没有开启（管理台 → 群管 里打开）")') == 1, "")
chk("S7 源码级：qq_tools_builtin 里没有第二套群管实现（不解析人名/不钳制参数）",
    all(m not in SRC_TB for m in ("_resolve_member", "set_group_ban",
                                  "get_group_member_list", "43200")), "")
chk("S8 源码级：danger 授权两处不写死工具名（预检与要码文本都按登记表 risk）",
    "pc_open" not in SRC_A[SRC_A.index("def _devctrl_danger_precheck"):
                           SRC_A.index("def _tool_auth_hold_text")]
    and "pc_close" not in SRC_A[SRC_A.index("def _devctrl_danger_precheck"):
                                SRC_A.index("def _tool_auth_hold_text")]
    and 'spec.get("risk") == "danger"' in SRC_A
    and SRC_A.count("for c in (calls or []):") >= 2, "")
chk("S9 源码级：whole 的 on 不出现在登记参数里（两个工具固定带入）",
    all("on" not in [p["name"] for p in T.get_tool(n)["params"]]
        for n in ("gadmin_whole_ban", "gadmin_whole_unban"))
    and SRC_TB.count('{"on": True}') == 1 and SRC_TB.count('{"on": False}') == 1, "")

# 清理核对：登记表回到只读状态不残留（本进程内断言）
chk("Z 登记表无重复（六个 gadmin_* 各恰好一个、无多余 gadmin_ 前缀）",
    sorted(s["name"] for s in T.tools_for({"is_owner": True})
           if s["name"].startswith("gadmin_")) == sorted(SIX), "")

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
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
