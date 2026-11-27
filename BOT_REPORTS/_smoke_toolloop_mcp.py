# -*- coding: utf-8 -*-
"""冒烟：B3 MCP 并入工具表——通用工具 mcp_call（write/owner）＋工具清单附
MCP 索引（60s 缓存、按行截断、兜底句）＋工具循环开时旧 MCP 路径停用
（_mcp_tools_doc 注入、[MCP] 分流）＋[MCP] 行转 mcp_call 走统一循环。
对应施工单验收 M1–M11 与不变量 I1–I4：

  M1  mcp_call 的 risk/scope/参数名与派单表一致；不传新参数（mcp_call_fn/
      mcp_allowed_fn）不登记；连调两次 register_builtin 不抛。
  M2  MCP 总开关关：mcp_call_fn 桩零调用，结果是「MCP 没有启用」。
  M3  args 非法 JSON / 数组 / 标量：桩零调用，结果是那句说明。
  M4  合法调用：桩收到的 server/tool/args(dict) 正确；run 层把超长返回文本
      截 1500 字并带截断说明（捕获 run 传给 make_result 的原文验证；
      make_result 对 summary 另有 600 字上限、回喂另有 500 字上限——既有
      管线层叠，本测试如实断言两层）；异常 → ok=False 带原因（截 200）。
  M5  工具清单：总开关开时附索引段（标题+行，且在固定用法说明之前）；
      总开关关或 mcp_allowed() 假时不附；超长按行截断并注明
      「（其余外部工具略）」；拉取异常附「（外部工具清单暂时拿不到）」；
      空行不附。
  M6  索引缓存：两次构建之间 tool_index 只调一次（60s 内）；异常不缓存
      （下次重试）。
  M7  旧注入停用：循环开时 system 没有 _mcp_tools_doc 的锚段（且桩零调用）；
      循环关时有。
  M8  旧分流停用：循环开时正文/actions 里的 [MCP] 行被转成 mcp_call 走循环
      （桩收到 server/tool/args）、不进 _mcp_agent_loop、不漏进聊天
      （含 final 里残余 [MCP] 行、解析不了的坏行两支）；循环关时照旧进旧
      循环（桩记账）。
  M9  非主人：清单里没有 mcp_call；[TOOL]/[MCP] 行照旧只剥掉不执行；
      [MCP] 行照旧只剥掉（不进旧循环）。
  M10 tool_confirm_write 开时 mcp_call 先复述确认（write 级），确认后续跑
      执行且不再重复要确认；关时直接执行。
  M11 工具循环关：_mcp_tools_doc 注入、[MCP] 分流、_mcp_agent_loop 与从前
      逐字一致——本脚本钉关键行原样存在；既有 _smoke_mcp.py 全过由跑法
      保证（改前基线 36 项 → 改后 36 项），不在本脚本内复跑。

  不变量钉法：I1＝M2/M5b（开关由 MCP 总开关决定）+ M8a/M8e（循环开能调、
  循环关走旧循环也能调——工具循环开关不改变「谁」能用）+ 源码级（索引段
  与 _mcp_tool_allowed 都查 M.mcp_allowed）；I2＝M7b/M8e/M11 源码级行原样；
  I3＝M8a–d/M9c 发出文字不含 [MCP] + 坏行剥掉 + 源码级（sub 分支无条件
  保留）；I4＝M1b（write/owner）+ M9d（gate deny）+ M10（复述确认）+
  M8 组（write 不触发索码桩）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录；mcp_call_fn、
mcp_allowed_fn、qq_mcp.mcp_allowed/tool_index/doc_cap_cfg、模型链
（call_glm_with_fallback）、daemon.send、_mcp_agent_loop、_mcp_tools_doc、
_devctrl_ask_code 全部打桩——零真实 MCP 连接、零真实发送、零联网。
一次性临时数据目录，跑完删。
用法：python BOT_REPORTS/_smoke_toolloop_mcp.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_mcp_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import qq_daemon as D  # noqa: E402,F401  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_mcp as MQ  # noqa: E402
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
A.out = lambda line: ALOG.append(str(line))
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None
D.out = lambda line: None

OWNER_QQ = "10086"
NOT_OWNER = "99999"


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


def owner_priv(**kw):
    return base_msg(**kw)


def non_owner_grp(**kw):
    return base_msg(chat_type="group", chat_id="555001", chat_name="冒烟群",
                    sender_id=NOT_OWNER, sender_name="小张", at_me=True,
                    trigger_reason="at", **kw)


class NullDaemon(object):
    """send 返回 ok 真（复述确认落表要它）；其余方法无副作用空操作。"""

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
        return {"ok": True, "queued": 1}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


# ---- MCP 桩：工具的执行/开关函数（TB 登记面）----

MCP_CALLS = []
MCP_TEXT = {"t": "外部工具结果文本"}


def stub_mcp_call(server, tool, args):
    """mcp_call_fn 桩：记 (server, tool, args dict)，返回预设文本。
    零真实 MCP 连接。"""
    MCP_CALLS.append((server, tool, dict(args)))
    return MCP_TEXT["t"]


MCP_ALLOWED = {"v": True}


def stub_mcp_allowed():
    return MCP_ALLOWED["v"]


def fake_search(q):
    return ("x 是测试条目（example.com）", "bing")


# ---- qq_mcp 模块桩：索引与总开关（A 侧 _tool_mcp_index_block /
# _mcp_index_rows / _mcp_tool_call / _mcp_tool_allowed 走它）----

QQ_ALLOWED = {"v": False}
IDX_CALLS = []
IDX_ROWS = {"rows": []}
IDX_RAISE = {"v": False}
DOC_CAP = {"v": 4000}


def stub_qq_allowed():
    return QQ_ALLOWED["v"]


def stub_tool_index(limit_per_server=20, doc_cap=None):
    IDX_CALLS.append(doc_cap)
    if IDX_RAISE["v"]:
        raise RuntimeError("stub: index unavailable")
    return list(IDX_ROWS["rows"]), []


def stub_doc_cap():
    return DOC_CAP["v"]


def reset_mcp_state():
    MCP_CALLS.clear()
    IDX_CALLS.clear()
    QQ_ALLOWED["v"] = False
    IDX_ROWS["rows"] = []
    IDX_RAISE["v"] = False
    DOC_CAP["v"] = 4000
    A._MCP_IDX_CACHE.update(ts=0.0, rows=None)
    A._MCP_DOC_CACHE.update(ts=0.0, txt="", n=0)
    with A._TOOL_PENDING_LOCK:
        A._TOOL_PENDING.clear()


# ---- reply_config 开关 ----

RC_PATH = os.path.join(A.DATA_DIR, "reply_config.json")
_RC_PAD = [0]


def write_reply_cfg(obj):
    _RC_PAD[0] += 1
    pathlib.Path(RC_PATH).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + ("\n" * _RC_PAD[0]),
        encoding="utf-8")


def set_cfg(loop_on, confirm_write=False):
    write_reply_cfg({"tool_loop_enabled": bool(loop_on),
                     "tool_confirm_write": bool(confirm_write)})


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False


def alog_of(tag):
    return [l for l in ALOG if tag in l]


# ---- 旧 MCP 路径桩（锚文本 = _mcp_tools_doc 的固定标题，M7 的锚点）----

OLD_DOC_ANCHOR = "## MCP 外部工具（主人已授权；只在主人对话里用）"
OLD_DOC_CALLS = []
OLD_LOOP_CALLS = []
ASK_CODE_CALLS = []


def stub_old_doc():
    OLD_DOC_CALLS.append(1)
    return OLD_DOC_ANCHOR + "\n[MCP] 服务器名.工具名 {\"参数\": \"值\"}\n可用工具：\n"


def stub_old_loop(daemon, msg, messages, reply):
    OLD_LOOP_CALLS.append(str(reply or "")[:80])


def stub_ask_code(src):
    ASK_CODE_CALLS.append(str(src or "-"))


# ---- 模型链桩 ----

def make_chain(outputs):
    calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append([dict(m) for m in messages])
        item = outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return "mx", item

    return _chain, calls


def run_process(msg, outputs, v2=True, json_mode=True, loop_on=True,
                confirm_write=False):
    """走真实 process()：模型链、视觉、搜索、贴图上限、旧 MCP 路径、
    qq_mcp 索引面全打桩。返回 (dm, 模型调用快照)。"""
    dm = NullDaemon()
    A.PLUGIN_HOST.daemon = dm
    chain, calls = make_chain(list(outputs))
    set_cfg(loop_on, confirm_write)
    _force = dict(PROMPT_V2_FORCE=True, REPLY_V2_FORCE=v2,
                  REPLY_JSON_FORCE=json_mode,
                  REACT_ONLY_FORCE=False, VCAP_FORCE=False,
                  vision_enabled=lambda: False, web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True,
                  lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=chain,
                  call_vision=lambda ms, urls, daemon=None: (_ for _ in ()).throw(
                      AssertionError("同步识图不应被调")),
                  owner_qq=lambda: OWNER_QQ,
                  _mcp_tools_doc=stub_old_doc,
                  _mcp_agent_loop=stub_old_loop,
                  _devctrl_ask_code=stub_ask_code)
    _mq = dict(mcp_allowed=stub_qq_allowed, tool_index=stub_tool_index,
               doc_cap_cfg=stub_doc_cap)
    with patched(A, **_force), patched(MQ, **_mq):
        reset_cap()
        ALOG.clear()
        OLD_DOC_CALLS.clear()
        OLD_LOOP_CALLS.clear()
        ASK_CODE_CALLS.clear()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
    return dm, calls


def joined_of(dm):
    return "".join(t for ts in dm.sent for t in ts)


def _json_main(actions, say="主人稍等，我看看"):
    return json.dumps({"want": 2, "say": [say], "emoji": None, "memory": [],
                       "wait": False, "voice": False, "sticker": None,
                       "quiet_minutes": None, "actions": actions},
                      ensure_ascii=False)


# ============ M1 登记 ============

TB.register_builtin(fake_search)
chk("M1a 不传新参数不登记 mcp_call（表里已有 web_search 证明调用生效）",
    T.get_tool("mcp_call") is None and T.get_tool("web_search") is not None, "")

TB.register_builtin(fake_search, mcp_call_fn=stub_mcp_call,
                    mcp_allowed_fn=stub_mcp_allowed)
_spec = T.get_tool("mcp_call")
chk("M1b 传两个 fn 才登记；risk=write、scope=owner（I4a）",
    _spec is not None and _spec["risk"] == "write"
    and _spec["scope"] == "owner",
    (_spec and (_spec["risk"], _spec["scope"])))
_pnames = [(p["name"], p["required"]) for p in (_spec or {}).get("params", [])]
chk("M1c 参数表：server/tool 必填、args 可选",
    _pnames == [("server", True), ("tool", True), ("args", False)], _pnames)
_dup_exc = ""
try:
    TB.register_builtin(fake_search, mcp_call_fn=stub_mcp_call,
                        mcp_allowed_fn=stub_mcp_allowed)
except Exception as exc:
    _dup_exc = repr(exc)
chk("M1d 连调两次不抛、表里仍是一个",
    _dup_exc == "" and T.get_tool("mcp_call") is not None, _dup_exc)
chk("M1e main() 把 _mcp_tool_call/_mcp_tool_allowed 接进 register_builtin",
    "mcp_call_fn=_mcp_tool_call" in SRC_A
    and "mcp_allowed_fn=_mcp_tool_allowed" in SRC_A, "")

# ============ M2/M3/M4 run 行为（直调 run，不经循环） ============

_MCP_RUN = T.get_tool("mcp_call")["run"]

MCP_CALLS.clear()
MCP_ALLOWED["v"] = False
_r2 = _MCP_RUN(None, None, {"server": "s", "tool": "t", "args": "{}"})
chk("M2/I1a 总开关关：ok=False、那句说明、mcp_call_fn 桩零调用",
    _r2["ok"] is False
    and _r2["summary"] == "MCP 没有启用（管理台 → MCP 服务 里打开）"
    and MCP_CALLS == [], (_r2["summary"], MCP_CALLS))

MCP_ALLOWED["v"] = True
_MCP_CALL_N = len(MCP_CALLS)
_r3a = _MCP_RUN(None, None, {"server": "s", "tool": "t", "args": '{"a":'})
_r3b = _MCP_RUN(None, None, {"server": "s", "tool": "t", "args": "[1,2]"})
_r3c = _MCP_RUN(None, None, {"server": "s", "tool": "t", "args": "123"})
_r3d = _MCP_RUN(None, None, {"server": "s", "tool": "t", "args": '"文本"'})
chk("M3 非法 JSON / 数组 / 标量 / 字符串：都是那句说明、桩零调用",
    all(r["ok"] is False
        and r["summary"].startswith("args 要是 JSON 对象的文本")
        and len(MCP_CALLS) == _MCP_CALL_N
        for r in (_r3a, _r3b, _r3c, _r3d)),
    [r["summary"] for r in (_r3a, _r3b, _r3c, _r3d)])

# M4 合法调用：捕获 run 传给 make_result 的原文（观察 run 层 1500 截断），
# 再由真 make_result 得到最终 dict（验证既有管线的 summary 上限如实生效）。
_real_make = T.make_result
_MAKE_SEEN = {}


def _rec_make(ok, summary, evidence="", data=None):
    _MAKE_SEEN["summary"] = summary
    return _real_make(ok, summary, evidence=evidence, data=data)


MCP_ALLOWED["v"] = True
MCP_TEXT["t"] = "外部结果"
with patched(T, make_result=_rec_make):
    _r4 = _MCP_RUN(None, None, {"server": "srva", "tool": "toolb",
                                "args": '{"k": "v"}'})
chk("M4a 桩收到 server/tool/args(dict) 正确",
    MCP_CALLS == [("srva", "toolb", {"k": "v"})], MCP_CALLS)
chk("M4b 结果文本进 summary（ok=True）",
    _r4["ok"] is True and _r4["summary"] == "外部结果", _r4["summary"])

_MCP_CALL_N = len(MCP_CALLS)
MCP_TEXT["t"] = "字" * 1600
with patched(T, make_result=_rec_make):
    _r4b = _MCP_RUN(None, None, {"server": "s", "tool": "t"})
_cap_len = len("（结果过长，已截断）")
chk("M4c run 层把超长文本截 1500 字并带截断说明（args 缺省时桩收到 {}）",
    len(MCP_CALLS) == _MCP_CALL_N + 1 and MCP_CALLS[-1][2] == {}
    and len(_MAKE_SEEN["summary"]) == 1500 + 1 + _cap_len
    and _MAKE_SEEN["summary"].startswith("字" * 100)
    and _MAKE_SEEN["summary"].endswith("（结果过长，已截断）"),
    (len(_MAKE_SEEN["summary"]), MCP_CALLS[-1][2]))
chk("M4c2 最终 summary 是 make_result 既有上限内的前缀（层叠如实生效）",
    _r4b["ok"] is True and len(_r4b["summary"]) <= 600
    and _r4b["summary"] == _MAKE_SEEN["summary"][:len(_r4b["summary"])],
    len(_r4b["summary"]))

_MCP_CALL_N = len(MCP_CALLS)
_M4_ERR = RuntimeError("坏" * 300)


def _boom(server, tool, args):
    raise _M4_ERR


_run_boom = TB._mcp_call_run(_boom, stub_mcp_allowed)
_r4c = _run_boom(None, None, {"server": "s", "tool": "t"})
chk("M4d 异常 → ok=False 带原因「调用失败：…」且原因截 200 字",
    _r4c["ok"] is False and _r4c["summary"].startswith("调用失败：")
    and len(_r4c["summary"]) <= len("调用失败：") + 200
    and len(MCP_CALLS) == _MCP_CALL_N,
    (len(_r4c["summary"]),))

chk("M4e/M3 源码级：1500 截断与说明、开关句、args 说明句、异常截 200 都在 TB 源码",
    "_MCP_RESULT_CAP = 1500" in SRC_TB
    and "（结果过长，已截断）" in SRC_TB
    and "MCP 没有启用（管理台 → MCP 服务 里打开）" in SRC_TB
    and "args 要是 JSON 对象的文本" in SRC_TB
    and "调用失败：%s" in SRC_TB and "str(exc)[:200]" in SRC_TB, "")

# ============ M5 工具清单附 MCP 索引（直调 _tool_doc_block） ============

_HEAD = "## 外部工具（用 mcp_call 调用，服务器名和工具名照下面写）"
_MQ_STUBS = dict(mcp_allowed=stub_qq_allowed, tool_index=stub_tool_index,
                 doc_cap_cfg=stub_doc_cap)

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_ROWS["rows"] = ["- srva.toolb — 测试外部工具一",
                    "- srva.toolc — 测试外部工具二"]
with patched(MQ, **_MQ_STUBS):
    _block = A._tool_doc_block()
chk("M5a/I1b 总开关开：附索引段（标题+行），且在固定用法说明之后（标题层级同旧段 ##）",
    _HEAD in _block and "- srva.toolb — 测试外部工具一" in _block
    and "- srva.toolc — 测试外部工具二" in _block
    and _block.index(_HEAD) > _block.index("需要做上面这些事时")
    and _HEAD.startswith("## "),
    _block[-160:])
chk("M5a2 索引行取自 tool_index(doc_cap=doc_cap_cfg())",
    IDX_CALLS == [4000], IDX_CALLS)

reset_mcp_state()
QQ_ALLOWED["v"] = False
with patched(MQ, **_MQ_STUBS):
    _block2 = A._tool_doc_block()
chk("M5b/I1b 总开关关：不附索引段（I1：由 MCP 自己的开关决定）",
    _HEAD not in _block2 and IDX_CALLS == [] and "需要做上面这些事时" in _block2,
    (_HEAD in _block2, IDX_CALLS))
chk("M5b2 发回1 总开关关：清单里也没有 mcp_call 行（没有服务器可照写，不自相矛盾）",
    "mcp_call" not in _block2, "mcp_call" in _block2)

reset_mcp_state()
QQ_ALLOWED["v"] = True
DOC_CAP["v"] = 500          # 下限 500 → 预算 500+700=1200
IDX_ROWS["rows"] = ["- srva.tool%02d — %s" % (i, "描" * 100)
                    for i in range(30)]
with patched(MQ, **_MQ_STUBS):
    _block3 = A._tool_mcp_index_block()
_kept_n = sum(1 for i in range(30)
              if "- srva.tool%02d — " % i in _block3)
chk("M5c 超长按行截断并注明（其余外部工具略）；块长受 doc_cap_cfg()+700 约束",
    _kept_n < 30 and "（其余外部工具略）" in _block3
    and len(_block3) <= 1200 + len("（其余外部工具略）") + 2
    and "- srva.tool29" not in _block3,
    (_kept_n, len(_block3)))

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_RAISE["v"] = True
with patched(MQ, **_MQ_STUBS):
    _block4 = A._tool_mcp_index_block()
chk("M5d 拉取异常：标题 + 兜底句「（外部工具清单暂时拿不到）」",
    _HEAD in _block4 and "（外部工具清单暂时拿不到）" in _block4
    and "srva" not in _block4, _block4[:160])

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_ROWS["rows"] = []
with patched(MQ, **_MQ_STUBS):
    _block5 = A._tool_mcp_index_block()
chk("M5e 总开关开但索引行空：不附",
    _block5 == "" and IDX_CALLS == [4000], (_block5, IDX_CALLS))

# ============ M6 索引 60 秒缓存 ============

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_ROWS["rows"] = ["- srva.toolb — 缓存测试"]
with patched(MQ, **_MQ_STUBS):
    A._tool_doc_block()
    A._tool_doc_block()
    A._tool_doc_block()
chk("M6a 60 秒内三次构建工具清单，tool_index 只拉一次",
    len(IDX_CALLS) == 1, IDX_CALLS)

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_RAISE["v"] = True
with patched(MQ, **_MQ_STUBS):
    A._tool_mcp_index_block()
    IDX_RAISE["v"] = False
    IDX_ROWS["rows"] = ["- srva.toolb — 异常后重试"]
    _block6 = A._tool_mcp_index_block()
chk("M6b 异常不缓存：下次构建重试成功（行出现在第二次结果里）",
    len(IDX_CALLS) == 2 and "异常后重试" in _block6,
    (len(IDX_CALLS), _block6[-80:]))

# ============ M7 旧注入停用（process 驱动——注入点在 process 主链路） ============

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：清单/循环按总开关过滤 mcp_call——场景要有它
_m7a = owner_priv(id=701, message_id="m701", text="帮我用外部工具查个事")
_dm7a, _c7a = run_process(_m7a, [_json_main([], say="主人我看看")],
                          v2=True, json_mode=True, loop_on=True)
_s7a = _c7a[0][0]["content"]
chk("M7a/I2 循环开+主人：旧 _mcp_tools_doc 锚段不在 system、桩零调用；工具清单在（含 mcp_call 行）",
    OLD_DOC_ANCHOR not in _s7a and len(OLD_DOC_CALLS) == 0
    and "你能用的工具" in _s7a and "mcp_call(server,tool,args?)" in _s7a,
    (OLD_DOC_ANCHOR in _s7a, len(OLD_DOC_CALLS)))

reset_mcp_state()
_m7b = owner_priv(id=702, message_id="m702", text="在吗主人")
_dm7b, _c7b = run_process(_m7b, ["主人我在"],
                          v2=False, json_mode=False, loop_on=False)
_s7b = _c7b[0][0]["content"]
chk("M7b/M11a/I2 循环关+主人：旧锚段照旧注入（桩被调，逐字一致）",
    OLD_DOC_ANCHOR in _s7b and len(OLD_DOC_CALLS) == 1
    and "mcp_call(server,tool,args?)" not in _s7b,
    (OLD_DOC_ANCHOR in _s7b, len(OLD_DOC_CALLS)))

reset_mcp_state()
_m7c = non_owner_grp(id=703, message_id="m703", text="帮我用外部工具查个事")
_dm7c, _c7c = run_process(_m7c, ["好的帮你看"],
                          v2=False, json_mode=False, loop_on=True)
_s7c = _c7c[0][0]["content"]
chk("M7c 非主人：旧锚段与工具清单都不在、旧 doc 桩零调用（照旧不注入）",
    OLD_DOC_ANCHOR not in _s7c and "mcp_call(server,tool,args?)" not in _s7c
    and len(OLD_DOC_CALLS) == 0,
    (OLD_DOC_ANCHOR in _s7c, len(OLD_DOC_CALLS)))

# 教学段集成（build_messages 面）：循环开+主人+总开关开 → 工具教学段带新索引段
reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_ROWS["rows"] = ["- srva.toolb — 清单集成测试"]
with patched(A, _tool_loop_on=lambda: True, PROMPT_V2_FORCE=True,
             owner_qq=lambda: OWNER_QQ), patched(MQ, **_MQ_STUBS):
    _msgs, _pname = A.build_messages(NullDaemon(),
                                     owner_priv(text="帮我用外部工具查个事"),
                                     ctx_k=32)
    _s7d = _msgs[0]["content"]
chk("M7d/M5f 集成：循环开+主人+总开关开 → 教学段带新索引段（标题+行）",
    _HEAD in _s7d and "- srva.toolb — 清单集成测试" in _s7d
    and OLD_DOC_ANCHOR not in _s7d,
    (_HEAD in _s7d, OLD_DOC_ANCHOR in _s7d))

# ============ M8 [MCP] 行转换与旧分流停用（process 驱动） ============

reset_mcp_state()
QQ_ALLOWED["v"] = True
IDX_ROWS["rows"] = ["- srva.toolb — 测试外部工具一"]
_m8a = owner_priv(id=801, message_id="m801", text="帮我用外部工具办个事")
_dm8a, _c8a = run_process(
    _m8a,
    [_json_main(["[MCP] srva.toolb {\"k\": \"v\"}"]), "办好了主人"],
    v2=True, json_mode=True, loop_on=True)
_j8a = joined_of(_dm8a)
chk("M8a/I3 循环开：actions 里的 [MCP] 行转 mcp_call 执行（桩收到 server/tool/args）",
    MCP_CALLS == [("srva", "toolb", {"k": "v"})], MCP_CALLS)
chk("M8a2 不进旧循环（_mcp_agent_loop 桩零调用）",
    OLD_LOOP_CALLS == [], OLD_LOOP_CALLS)
chk("M8a3/I3 发出的文字不含 [MCP]",
    "[MCP]" not in _j8a and "办好了主人" in _j8a, _j8a[:120])
chk("M8a4 write 级不触发索码（I4 的一部分）",
    ASK_CODE_CALLS == [], ASK_CODE_CALLS)
chk("M8a5 循环日志：mcp_call:成功",
    any("AGENT_TOOL_LOOP" in l and "mcp_call:成功" in l for l in ALOG),
    alog_of("AGENT_TOOL_LOOP"))
chk("M8a6 主调用的 system 带索引段（M7d 同源，循环模型的输入也有）",
    _c8a and _HEAD in _c8a[0][0]["content"]
    and "- srva.toolb — 测试外部工具一" in _c8a[0][0]["content"],
    _HEAD in (_c8a[0][0]["content"] if _c8a else ""))

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：mcp_call 可用（总开关开）才进循环
_m8b = owner_priv(id=802, message_id="m802", text="帮我用外部工具再办一件")
_dm8b, _c8b = run_process(
    _m8b,
    ["[MCP] srvc.toold {\"n\": 1}\n我看看哈主人", "查好了，是 1"],
    v2=False, json_mode=False, loop_on=True)
_j8b = joined_of(_dm8b)
chk("M8b/I3 循环开：正文（v1 纯文本）里的 [MCP] 行同样转 mcp_call 执行",
    MCP_CALLS == [("srvc", "toold", {"n": 1})] and "[MCP]" not in _j8b
    and "查好了，是 1" in _j8b,
    (MCP_CALLS, _j8b[:120]))

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：mcp_call 可用但行格式坏——剥掉不执行
_m8c = owner_priv(id=803, message_id="m803", text="帮我用外部工具办第三件")
_dm8c, _ = run_process(
    _m8c,
    ["[MCP] 坏格式行不是工具\n我看下哈", "这个我办不了"],
    v2=False, json_mode=False, loop_on=True)
_j8c = joined_of(_dm8c)
chk("M8c/I3 解析不了的 [MCP] 行：桩零调用、不进旧循环、照旧剥掉不漏进聊天",
    MCP_CALLS == [] and OLD_LOOP_CALLS == [] and "[MCP]" not in _j8c
    and "我看下哈" in _j8c,
    (MCP_CALLS, OLD_LOOP_CALLS, _j8c[:120]))

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：mcp_call 可用才进循环
_m8d = owner_priv(id=804, message_id="m804", text="帮我用外部工具办第四件")
_dm8d, _ = run_process(
    _m8d,
    [_json_main(["[MCP] s1.t1"]), "好了主人\n[MCP] s2.t2 我没再动"],
    v2=True, json_mode=True, loop_on=True)
_j8d = joined_of(_dm8d)
chk("M8d/I3 无参数 [MCP] 行转 mcp_call（桩收到空 dict）；final 里残余 [MCP] 行被剥掉",
    MCP_CALLS == [("s1", "t1", {})] and "[MCP]" not in _j8d
    and "好了主人" in _j8d,
    (MCP_CALLS, _j8d[:160]))

reset_mcp_state()
_m8e = owner_priv(id=805, message_id="m805", text="帮我用外部工具办第五件")
_dm8e, _ = run_process(
    _m8e,
    ["[MCP] srve.toolf {\"x\": 1}"],
    v2=False, json_mode=False, loop_on=False)
_j8e = joined_of(_dm8e)
chk("M8e/M11b/I2/I1c 循环关+主人+doc 非空：照旧进旧循环（桩 1 次、旧 doc 桩被查）",
    OLD_LOOP_CALLS and MCP_CALLS == [] and len(OLD_DOC_CALLS) >= 1,
    (OLD_LOOP_CALLS, MCP_CALLS, len(OLD_DOC_CALLS)))

# 转换函数单测（M8 的单元面）
_conv = A._mcp_lines_to_calls
chk("M8f1 转换：带参数行 → [TOOL] mcp_call（args 规范化 JSON 文本）",
    _conv('[MCP] a.b {"x": "值"}') == '[TOOL] mcp_call {"server": "a", "tool": "b", "args": "{\\"x\\": \\"值\\"}"}',
    _conv('[MCP] a.b {"x": "值"}'))
chk("M8f2 转换：无参数行 → 不带 args 键",
    _conv("[MCP] a.b") == '[TOOL] mcp_call {"server": "a", "tool": "b"}',
    _conv("[MCP] a.b"))
chk("M8f3 转换：坏 JSON / 数组参数 / 无点行 / 尾随文字 → 原样保留",
    _conv("[MCP] a.b {坏") == "[MCP] a.b {坏"
    and _conv("[MCP] a.b [1]") == "[MCP] a.b [1]"
    and _conv("[MCP] 只有一个名字") == "[MCP] 只有一个名字"
    and _conv("[MCP] a.b {\"x\":1} 尾随") == "[MCP] a.b {\"x\":1} 尾随",
    (_conv("[MCP] a.b {坏"), _conv("[MCP] a.b [1]")))
chk("M8f4 转换：多行混合（[TOOL] 行不动、普通行不动）",
    _conv("[TOOL] web_search {\"query\": \"x\"}\n普通话\n[MCP] a.b {\"y\": 2}")
    == '[TOOL] web_search {"query": "x"}\n普通话\n[TOOL] mcp_call {"server": "a", "tool": "b", "args": "{\\"y\\": 2}"}',
    "")
chk("M8f5 转换：句中的 [MCP] 字样不是「[MCP] 行」，不转换（与旧正则同语义）",
    _conv("另外 [MCP] a.b 没动") == "另外 [MCP] a.b 没动",
    _conv("另外 [MCP] a.b 没动"))

chk("M8g 源码级：旧分流条件带 not _tool_loop_on()（收窄）且 sub 剥行分支无条件保留（I3）",
    "if owner and not _tool_loop_on() and _mcp_tools_doc():" in SRC_A
    and "_mcp_agent_loop(daemon, msg, messages, reply)" in SRC_A
    and 'reply = _MCP_ACT_ANY_RE.sub("", reply)' in SRC_A, "")
chk("M8h 源码级：衔接点转换在 parse_calls 之前、条件与进循环一致（owner+循环开）",
    SRC_A.index("if owner and _tool_loop_on() and _MCP_ACT_ANY_RE.search(reply):")
    < SRC_A.index("_tool_calls, _tool_rest = qq_tools.parse_calls(reply)")
    and "reply = _mcp_lines_to_calls(reply)" in SRC_A, "")
chk("M8i 源码级：旧注入条件收窄、注入行原样保留（M11a 的行级证据）",
    "if owner and not _tool_loop_on():" in SRC_A
    and 'messages[0]["content"] += "\\n\\n" + _mcp_txt' in SRC_A
    and SRC_A.index('messages[0]["content"] += "\\n\\n" + _mcp_txt')
    > SRC_A.index("if owner and not _tool_loop_on():")
    < SRC_A.index('messages[0]["content"] += "\\n\\n" + _mcp_txt'), "")

# ============ M9 非主人 ============

chk("M9a tools_for：非主人清单没有 mcp_call，主人有（I4b 的一半）",
    all(s["name"] != "mcp_call" for s in T.tools_for({"is_owner": False}))
    and any(s["name"] == "mcp_call" for s in T.tools_for({"is_owner": True})),
    "")

reset_mcp_state()
with patched(MQ, **_MQ_STUBS):
    QQ_ALLOWED["v"] = False
    _n_off = [s["name"] for s in T.tools_for({"is_owner": True})
              if A._tool_available(s["name"])]
    QQ_ALLOWED["v"] = True
    _n_on = [s["name"] for s in T.tools_for({"is_owner": True})
             if A._tool_available(s["name"])]
chk("M9a2 发回1 _tool_available：总开关关时主人的可用清单也没有 mcp_call；开时有（同 pc_* 先例）",
    "mcp_call" not in _n_off and "mcp_call" in _n_on,
    (("mcp_call" in _n_off, "mcp_call" in _n_on)))
_r9 = T.run_tool(T.get_tool("mcp_call"), {"server": "s", "tool": "t"},
                 {"is_owner": False, "is_gadmin": False}, {})
chk("M9b/I4b 非主人 gate deny：那句拒绝、桩零调用",
    _r9["ok"] is False and "这个工具只有主人能用" in _r9["summary"]
    and MCP_CALLS == [], (_r9["summary"], MCP_CALLS))

reset_mcp_state()
_m9c = non_owner_grp(id=901, message_id="m901", text="外部工具帮个忙")
_dm9c, _ = run_process(
    _m9c,
    ["[TOOL] mcp_call {\"server\": \"s9\", \"tool\": \"t9\"}\n[MCP] s9.t9\n我看看"],
    v2=False, json_mode=False, loop_on=True)
_j9c = joined_of(_dm9c)
chk("M9c/I3/I4 非主人：[TOOL]/[MCP] 行照旧只剥掉、不执行、不进旧循环",
    MCP_CALLS == [] and OLD_LOOP_CALLS == [] and "[TOOL]" not in _j9c
    and "[MCP]" not in _j9c and "我看看" in _j9c,
    (MCP_CALLS, OLD_LOOP_CALLS, _j9c[:120]))

# ============ M10 复述确认（write 级） ============

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：mcp_call 可用（总开关开）才进循环
_m10a = owner_priv(id=1001, message_id="m1001", text="外部工具确认测试")
_dm10a, _ = run_process(
    _m10a,
    [_json_main(["[TOOL] mcp_call {\"server\": \"s10\", \"tool\": \"t10\", "
                 "\"args\": \"{\\\"k\\\": \\\"v\\\"}\"}"]), "占位不该被消费"],
    v2=True, json_mode=True, loop_on=True, confirm_write=True)
_j10a = joined_of(_dm10a)
_ent10 = A._TOOL_PENDING.get("private:%s" % OWNER_QQ)
chk("M10a tool_confirm_write 开：先复述确认（我要做…回复「确认」我就执行），桩零调用、待确认落表",
    "回复「确认」我就执行" in _j10a and "调用 MCP 外部工具" in _j10a
    and MCP_CALLS == [] and _ent10 is not None
    and [c.get("tool") for c in (_ent10.get("calls") or [])] == ["mcp_call"],
    (_j10a[:120], MCP_CALLS, _ent10 and [c.get("tool") for c in _ent10.get("calls") or []]))

_m10b = owner_priv(id=1002, message_id="m1002", text="确认")
_dm10b, _c10b = run_process(
    _m10b,
    ["办好了主人"],
    v2=True, json_mode=True, loop_on=True, confirm_write=True)
_j10b = joined_of(_dm10b)
chk("M10b 确认后续跑：桩收到调用、只执行这一次、不再重复要确认（I4 的确认闸语义）",
    MCP_CALLS == [("s10", "t10", {"k": "v"})]
    and "回复「确认」我就执行" not in _j10b and "办好了主人" in _j10b
    and A._TOOL_PENDING.get("private:%s" % OWNER_QQ) is None,
    (MCP_CALLS, _j10b[:120]))

reset_mcp_state()
QQ_ALLOWED["v"] = True     # 发回1：mcp_call 可用才进循环
_m10c = owner_priv(id=1003, message_id="m1003", text="外部工具免确认测试")
_dm10c, _ = run_process(
    _m10c,
    [_json_main(["[TOOL] mcp_call {\"server\": \"s11\", \"tool\": \"t11\"}"]),
     "直接办好了"],
    v2=True, json_mode=True, loop_on=True, confirm_write=False)
_j10c = joined_of(_dm10c)
chk("M10c 确认开关关：mcp_call 直接执行（与 M8a 同一闸行为）",
    MCP_CALLS == [("s11", "t11", {})] and "直接办好了" in _j10c
    and A._TOOL_PENDING.get("private:%s" % OWNER_QQ) is None,
    (MCP_CALLS, _j10c[:120]))

# ============ M11 + 源码级收尾（逐字一致 + 新函数落点 + I 汇总） ============

chk("M11c/I2 源码级：旧循环调用、剥行、锚标题等关键行原样存在",
    "_mcp_agent_loop(daemon, msg, messages, reply)" in SRC_A
    and 'reply = _MCP_ACT_ANY_RE.sub("", reply)' in SRC_A
    and 'if owner and _mcp_tools_doc()' not in SRC_A, "")

_NEWS = ("_mcp_index_rows", "_tool_mcp_index_block", "_mcp_lines_to_calls",
         "_mcp_tool_call", "_mcp_tool_allowed")
_ih = SRC_A.index("class AdminHandler")
_is = SRC_A.index("def start_admin_server")
chk("M11d 新顶层函数都在 AdminHandler 类之后、start_admin_server 之前",
    all(("def %s" % f) in SRC_A[_ih:_is] for f in _NEWS),
    [f for f in _NEWS if ("def %s" % f) not in SRC_A[_ih:_is]])

chk("I1d 源码级：索引段与接线开关都查 MCP 自己的总开关（工具循环开关不掺和）",
    'if not M.mcp_allowed():' in SRC_A
    and "return M.mcp_allowed()" in SRC_A
    and SRC_A.index("if not M.mcp_allowed():") > SRC_A.index("def _tool_mcp_index_block"),
    "")

chk("I3e 源码级：转换函数对解析不了的行原样返回（不留 [MCP] 变体泄漏路径）",
    "return line" in SRC_A[SRC_A.index("def _mcp_lines_to_calls"):
                           SRC_A.index("def _mcp_tool_call")], "")

# 清理核对：登记表回到只读状态不残留（本进程内断言）
chk("Z 登记表无重复（mcp_call 恰好一个）",
    sum(1 for s in T.tools_for({"is_owner": True})
        if s["name"] == "mcp_call") == 1, "")

print()
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败：%s" % "；".join(fails))
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
sys.exit(1 if fails else 0)
