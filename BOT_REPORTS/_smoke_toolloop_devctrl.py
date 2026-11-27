# -*- coding: utf-8 -*-
"""冒烟：B2a 设备操控八个动作并入工具表（pc_find/pc_read/pc_list/pc_search/
pc_apps/pc_send/pc_open/pc_close），清单按设备操控开关过滤，旧路径按工具循环
开关收窄。对应施工单验收 V1–V10 与不变量 I1–I4：

  V1  八个工具 risk/scope/参数名与派单表一致；register_builtin 不传 devctrl
      两个参数不登记它们、传了才登记；连调两次不抛。
  V2  设备操控开关关：工具清单里没有任何 pc_*；模型写了 pc_read →
      _devctrl_step 桩零调用，结果是「电脑操控没有开启」。
  V3  开关开：清单里有八个 pc_*；八个工具各调一次，桩收到的动作名和参数与
      表一一对应，第二个参数是 ("private", 主人QQ)。
  V4  成败判定：源码里出现过的每一条失败文案（保护目录、敏感文件、发送失败、
      打开被拦、结束失败、不支持的动作等）→ ok=False；成功文案 → ok=True；
      失败前缀集合与 _devctrl_step/_open_guard 源码双向核对。
  V5  pc_open/pc_close：auth_ok 为假时不执行、回喂里是「需要授权码」那句；
      为真时执行。_devctrl_auth_ok 取值：授权开关关→真；开关开且窗口没开→假；
      窗口开着→真；循环级（_tool_loop_run 的 opts 用回调）同验。
  V6  教学替换（四条消息分别构建后按段比对）：①循环开+主人+设备操控开 →
      已开启版不在、清单有 pc_*；②循环开+主人+设备操控关 → 未开启版在、
      清单没有 pc_*；③循环关+主人+设备操控开 → 已开启版照旧在（与源码原文
      同文）；④非主人 → 两种设备开关状态下都没有设备操控教学也没有 pc_*。
  V7  _handle_devctrl 收窄：循环开时关键词不再截走（False、无索码、不进旧
      循环）；索码窗口内 6 位授权码仍被认（窗口打开）；循环关时行为与改前
      一致（关键词照旧触发、窗口内照旧进旧循环、免码照旧直通）。
  V8  [连线] 标记（真实 process 驱动）：循环开时不触发索码、标记照旧剥掉；
      关时照旧触发索码。
  V9  非主人：清单里没有 pc_*；_handle_devctrl 仍然第一闸拒绝；工具闸也拒。
  V10 既有测试通过数不比改前少——由跑法保证：_smoke_arch/_smoke_toolloop_wire/
      _smoke_toolloop_tools/_smoke_authswitch/_smoke_routes/_smoke_uisec 各自
      单跑，与改前基线（281/61→65/51/44/56/78）比对，不在本脚本内复跑。
      （wire 从 61 涨到 65：W2e/W2f 各拆成两条并按 B2a 改写，另加 W2e2/W2f2。）

  不变量钉法：I1＝V3 桩收到的动作与参数全表 + I1a/I1b 源码级（八个 run 全是
  同一个薄包装、qq_tools_builtin 里没有第二套防护实现）；I2＝V2 全组 + V5 循环级
  关窗口拒执行；I3＝V7e/V7f/V7g（循环关时三条旧路逐字照旧）；I4＝V9 全组 +
  W3（wire 测试，非主人逐字一致）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录；_devctrl_step、
模型调用（call_glm_with_fallback）、_devctrl_ask_by_mark、devctrl_agent_loop
全部打桩——零真实文件读取、零真实打开/关闭程序、零真实发送、零联网。
跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_devctrl.py
"""
import contextlib
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_devctrl_iso_")
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
A.out = lambda line: ALOG.append(str(line))
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None
D.out = lambda line: None

OWNER_QQ = "10086"
NOT_OWNER = "99999"

EIGHT = {
    "pc_find": ("find", "read", ["query"]),
    "pc_read": ("read", "read", ["path"]),
    "pc_list": ("list", "read", ["path"]),
    "pc_search": ("searchweb", "read", ["query"]),
    "pc_apps": ("apps", "read", []),
    "pc_send": ("send", "write", ["path"]),
    "pc_open": ("open", "danger", ["target"]),
    "pc_close": ("close", "danger", ["name"]),
}

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
    return owner_msg(chat_type="group", chat_id="555001", chat_name="冒烟群",
                     sender_id=NOT_OWNER, sender_name="小张", at_me=True,
                     trigger_reason="at", **kw)


class NullDaemon(object):
    """只提供 send/history/ack/group_names，其余方法无副作用空操作。"""

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
        self.sent.append((chat_type, chat_id, list(texts)))

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


def run_named(name, args, opts=None, msg=None, is_owner=True):
    """按名字跑一个工具（打桩环境；daemon 传 None——桩不用它）。"""
    m = msg or owner_msg()
    ctx = {"daemon": None, "msg": m, "is_owner": is_owner, "is_gadmin": False}
    return T.run_tool(T.get_tool(name), args, ctx, opts or {})


def avail_names():
    """主人视角、过 _tool_available 过滤后的清单名（与 _tool_doc_block/_tool_loop_run
    同一道过滤）。"""
    return [s["name"] for s in T.tools_for({"is_owner": True, "is_gadmin": False})
            if A._tool_available(s.get("name"))]


# ============ V1 登记：表一致、不传不登记、传了才登记、幂等 ============

TB.register_builtin(fake_search)
chk("V1a 不传 devctrl 参数：八个 pc_* 都不登记（web_search 照常）",
    all(T.get_tool(n) is None for n in EIGHT)
    and T.get_tool("web_search") is not None,
    [n for n in EIGHT if T.get_tool(n) is not None])

TB.register_builtin(fake_search, devctrl_step_fn=stub_step,
                    devctrl_enabled_fn=lambda: ENABLED["v"])
bad = []
for n, (act, risk, params) in EIGHT.items():
    spec = T.get_tool(n)
    if spec is None:
        bad.append((n, "缺"))
        continue
    got = ([p["name"] for p in spec["params"]],
           [bool(p["required"]) for p in spec["params"]],
           spec["risk"], spec["scope"])
    want_req = [True] * len(params)
    if n == "pc_list":
        want_req = [False]
    if (got[0] != params or got[1] != want_req or got[2] != risk
            or got[3] != "owner"):
        bad.append((n, got))
chk("V1b 传了才登记；八个的 risk/scope/参数名/必填与派单表一致",
    not bad, bad)

try:
    TB.register_builtin(fake_search, devctrl_step_fn=stub_step,
                        devctrl_enabled_fn=lambda: True)
    TB.register_builtin(fake_search, devctrl_step_fn=stub_step,
                        devctrl_enabled_fn=lambda: True)
    dup_ok = all(T.get_tool(n) is not None for n in EIGHT)
except Exception as exc:
    dup_ok = False
    print("register_builtin 二次调用异常：%r" % exc)
chk("V1c 连调两次不抛、登记原样保留", dup_ok, "")

chk("V1d 派单表之外没有多登记的 pc_ 前缀工具",
    sorted(n for n in ("pc_find", "pc_read", "pc_list", "pc_search", "pc_apps",
                       "pc_send", "pc_open", "pc_close")
           if T.get_tool(n) is not None) == sorted(EIGHT), "")

# ============ V2 设备操控开关关：清单无 pc_*、写了也不执行 ============

ENABLED["v"] = False
STEP_CALLS.clear()
with patched(A, _device_ctrl_enabled=lambda: False):
    v2a = (all(not A._tool_available(n) for n in EIGHT)
           and A._tool_available("web_search") and A._tool_available("react"))
    v2b_names = avail_names()
    doc_off = A._tool_doc_block()
chk("V2a 开关关：_tool_available 对 pc_* 全 False、对其它工具 True",
    v2a, "")
chk("V2b 开关关：主人可用清单里没有任何 pc_",
    not [n for n in v2b_names if n.startswith("pc_")], v2b_names)
chk("V2c 开关关：工具教学清单里没有 pc_read（pc_* 不列出）",
    "pc_read" not in doc_off and "你能用的工具" in doc_off, doc_off[:120])
r2 = run_named("pc_read", {"path": "C:\\a.txt"}, opts={"auth_ok": True})
chk("V2d/I2 开关关：pc_read 不执行（桩零调用）、结果=「电脑操控没有开启」",
    r2["ok"] is False and "电脑操控没有开启" in r2["summary"]
    and STEP_CALLS == [], (r2["summary"], STEP_CALLS))
r2b = run_named("pc_open", {"target": "记事本"}, opts={"auth_ok": True})
chk("V2e/I2 开关关：danger 的 pc_open 同样不执行、同样那句话",
    r2b["ok"] is False and "电脑操控没有开启" in r2b["summary"]
    and STEP_CALLS == [], (r2b["summary"], STEP_CALLS))

# ============ V3 开关开：清单有八个、桩收到的动作/参数/reply_to 全表 ============

ENABLED["v"] = True
with patched(A, _device_ctrl_enabled=lambda: True):
    v3a_names = avail_names()
    doc_on = A._tool_doc_block()
chk("V3a 开关开：主人可用清单里有八个 pc_*",
    set(EIGHT) <= set(v3a_names), v3a_names)
chk("V3b 开关开：工具教学清单里列出了八个 pc_*",
    all((n in doc_on) for n in EIGHT), [n for n in EIGHT if n not in doc_on])

CASES = [
    ("pc_find", {"query": "报告"}, ("报告",)),
    ("pc_read", {"path": "C:\\a.txt"}, ("C:\\a.txt",)),
    ("pc_list", {}, ("",)),
    ("pc_list", {"path": "D:\\x"}, ("D:\\x",)),
    ("pc_search", {"query": "天气"}, ("天气",)),
    ("pc_apps", {}, ("",)),
    ("pc_send", {"path": "C:\\a.txt"}, ("C:\\a.txt",)),
    ("pc_open", {"target": "记事本"}, ("记事本",)),
    ("pc_close", {"name": "qq"}, ("qq",)),
]
STEP_CALLS.clear()
STEP_TEXT["t"] = "find「x」→ 0 条：无匹配"
for name, args, _want_arg in CASES:
    run_named(name, args, opts={"auth_ok": True})
got_actions = [(c[1], c[2]) for c in STEP_CALLS]
want_actions = [(EIGHT[name][0], w[0]) for name, _args, w in CASES]
chk("V3c 九次调用（pc_list 两种）桩收到的动作名与参数一一对应",
    len(STEP_CALLS) == len(CASES) and got_actions == want_actions,
    (got_actions, want_actions))
chk("V3d/I4 每次第二个参数都是 (\"private\", 主人QQ)",
    all(c[0] == ("private", OWNER_QQ) for c in STEP_CALLS), STEP_CALLS)
STEP_CALLS.clear()
for name, args, _w in CASES:
    r = run_named(name, args, opts={"auth_ok": True})
    if r["ok"] is not True:
        chk("V3e %s 成功文案 → ok=True" % name, False, r["summary"])
        break
else:
    chk("V3e 成功文案 → 八个工具全部 ok=True", True, "")

# ============ V4 成败判定：源码失败文案逐条 → False，成功文案 → True ============

FAIL_TEXTS = [
    # 保护目录拒绝（read/send/list + open 的 _open_guard）
    "read 拒绝：C:\\Windows 在保护目录内",
    "send 拒绝：C:\\Windows\\x 在保护目录内",
    "list 拒绝：C:\\Windows 在保护目录内",
    "open 拒绝：C:\\Windows\\x 在保护目录内",
    # 敏感文件拒绝
    "read 拒绝：C:\\a.pem 属于敏感文件（密钥/凭据/浏览器数据），设备操控通道不读取",
    "send 拒绝：C:\\id_rsa 属于敏感文件（密钥/凭据类），设备操控通道不发送",
    # 读写列失败（OSError / 不存在）
    "read 失败：FileNotFoundError(2, 'x')",
    "send 失败：文件不存在 C:\\nope（路径要原样复制 find 给的结果，别自己猜）",
    "send 失败：OSError(13, 'x')",
    "list 失败：目录不存在 C:\\nope",
    "list 失败：OSError(13, 'x')",
    # 发送失败（daemon 返回失败）
    "send 发送失败：超时 ——必须如实告诉主人失败了，不能说已发送",
    # 打开被拦（动作执行未开启 / 协议 / 不存在 / 类型白名单 / 解析失败）
    "open 拒绝：动作执行未开启——请主人到控制台「设备操控」页开启"
    "「动作执行（打开/关闭程序）」后重试；把这句话如实告诉主人",
    "open 拒绝：只打开 http 和 https 网页",
    "open 拒绝：目标不存在 C:\\nope",
    "open 拒绝：出于安全，不打开这一类文件",
    "open 失败：未找到「xx」。可用 apps 动作列出已装应用，或给完整路径/网址。"
    "附近的应用：a、b",
    "open 失败：OSError(2, 'x')",
    # 结束失败 / 拒绝 / 部分成功
    "close 拒绝：动作执行未开启——请主人到控制台「设备操控」页开启"
    "「动作执行（打开/关闭程序）」后重试；把这句话如实告诉主人",
    "close 拒绝：explorer.exe 是系统关键进程，不允许关闭",
    "close 失败：OSError(5, 'x')",
    "close 部分成功：已发送关闭请求但 x.exe 仍在运行"
    "（可能有未保存内容弹窗挡着），建议主人手动确认",
    # 不支持的动作
    "不支持的动作 foo",
]
bad4 = [(t, TB._devctrl_ok(t)) for t in FAIL_TEXTS if TB._devctrl_ok(t)]
chk("V4a %d 条源码失败文案全部 ok=False" % len(FAIL_TEXTS),
    not bad4, bad4)

OK_TEXTS = [
    "find「报告」→ 0 条：无匹配",
    "read C:\\a.txt 前2000字：\n正文内容",
    "send 发送成功（QQ 已确认送达）：a.txt",
    "list C:\\Users\\x：a.txt；b.txt",
    "searchweb「天气」(bing)：今天晴",
    "apps「」→ 3 个：a、b、c",
    "open 成功（url）：https://example.com",
    "open 成功（path）：C:\\a.txt",
    "close：x.exe 当前没有在运行",
    "close 成功：x.exe 已关闭",
]
bad4b = [(t, TB._devctrl_ok(t)) for t in OK_TEXTS if not TB._devctrl_ok(t)]
chk("V4b %d 条成功/已达成文案全部 ok=True" % len(OK_TEXTS),
    not bad4b, bad4b)

# 源码双向核对：_devctrl_step / _open_guard 里出现的每个「动作词+失败标记」
# 组合都被 _DEVCTRL_FAIL_PREFIXES 覆盖；每个前缀也确实出自源码。
step_src = SRC_A[SRC_A.index("def _devctrl_step"):
                 SRC_A.index("\ndef devctrl_agent_loop")]
guard_src = SRC_A[SRC_A.index("def _open_guard"):
                  SRC_A.index("\ndef _list_start_apps")]
pairs = set(re.findall(r"\b(?:read|send|list|open|close) "
                       r"(?:拒绝|失败|发送失败|部分成功)", step_src))
pairs |= set(re.findall(r"\b(?:read|send|list|open|close) "
                        r"(?:拒绝|失败|发送失败|部分成功)", guard_src))
uncovered = [p for p in pairs if p not in TB._DEVCTRL_FAIL_PREFIXES]
chk("V4c 源码里出现过的全部 %d 种「动作词+失败标记」组合都在判定前缀里"
    % len(pairs), not uncovered and "不支持的动作" in step_src,
    (sorted(pairs), uncovered))
ghost = [p for p in TB._DEVCTRL_FAIL_PREFIXES
         if p != "不支持的动作" and p not in step_src and p not in guard_src]
chk("V4d 判定前缀表里没有源码里不存在的条目", not ghost, ghost)

# ============ V5 danger 授权：auth_ok 回调 + 循环级 ============

STEP_CALLS.clear()
with patched(A, cfg_2fa_on=lambda: False):
    v5a = A._devctrl_auth_ok()
A._DEVCTRL_AUTH["ok_until"] = 0.0
with patched(A, cfg_2fa_on=lambda: True):
    v5b = A._devctrl_auth_ok()
A._DEVCTRL_AUTH["ok_until"] = time.time() + 120
with patched(A, cfg_2fa_on=lambda: True):
    v5c = A._devctrl_auth_ok()
A._DEVCTRL_AUTH["ok_until"] = 0.0
chk("V5a auth_ok 取值：授权开关关→真；开关开且窗口没开→假；窗口开着→真",
    v5a is True and v5b is False and v5c is True, (v5a, v5b, v5c))

r5 = run_named("pc_open", {"target": "记事本"}, opts={})
n5 = len(STEP_CALLS)
chk("V5b pc_open auth_ok 假（没给）：不执行、回喂是「需要授权码」那句",
    r5["ok"] is False and "需要授权码" in r5["summary"]
    and "6 位授权码" in r5["summary"] and len(STEP_CALLS) == n5,
    (r5["summary"], len(STEP_CALLS)))
r5b = run_named("pc_close", {"name": "qq"}, opts={"auth_ok": False})
chk("V5c pc_close auth_ok 显式假：同样拒、不执行",
    r5b["ok"] is False and "需要授权码" in r5b["summary"]
    and len(STEP_CALLS) == n5, (r5b["summary"],))
r5c = run_named("pc_open", {"target": "记事本"}, opts={"auth_ok": True})
chk("V5d pc_open auth_ok 真：执行（桩收到 open）",
    r5c["ok"] is True and len(STEP_CALLS) == n5 + 1
    and STEP_CALLS[-1][1] == "open", (r5c["ok"], STEP_CALLS[-1:]))

# 循环级：_tool_loop_run 的 opts["auth_ok"] 是回调——窗口没开时 danger 拒、
# 结果按「需要授权码」回喂；窗口开着时真执行。
def loop_case(ok_until):
    STEP_CALLS.clear()
    A._DEVCTRL_AUTH["ok_until"] = ok_until
    seen_feedback = {"c": ""}

    def _chain_cap(messages, text_ctx="", budget_sec=200, **kwargs):
        # 循环里回喂之后的续写：模型直接给最终回复（不再带工具行）。
        if messages and isinstance(messages[-1], dict):
            seen_feedback["c"] = str(messages[-1].get("content") or "")
        return "mx", "开好了"

    with patched(A, call_glm_with_fallback=_chain_cap,
                 cfg_2fa_on=lambda: True,
                 _device_ctrl_enabled=lambda: True):
        res = A._tool_loop_run(None, owner_msg(), [],
                               "[TOOL] pc_open {\"target\": \"记事本\"}")
    return res, seen_feedback["c"], list(STEP_CALLS)


res5, feed5, steps5 = loop_case(0.0)
chk("V5e/I2 循环级·窗口没开：pc_open 不执行、回喂里有「需要授权码」",
    steps5 == [] and res5["actions"] == [] and "需要授权码" in feed5,
    (steps5, feed5[-120:]))
res5b, feed5b, steps5b = loop_case(time.time() + 120)
chk("V5f 循环级·窗口开着：pc_open 真执行（桩收到 open）",
    len(steps5b) == 1 and steps5b[0][1] == "open"
    and steps5b[0][0] == ("private", OWNER_QQ)
    and [a["tool"] for a in res5b["actions"]] == ["pc_open"],
    (steps5b, res5b["actions"]))

# ============ V6 教学替换（四条消息分别构建，按段比对） ============

def build_sys(msg, loop_on, dev_on):
    with patched(A, _tool_loop_on=lambda: loop_on,
                 _device_ctrl_enabled=lambda: dev_on,
                 PROMPT_V2_FORCE=True, owner_qq=lambda: OWNER_QQ):
        msgs, _pname = A.build_messages(NullDaemon(), msg, ctx_k=32)
    return msgs[0]["content"]


OPEN_MARK = "「设备操控」功能已开启"
# 注意：语音教学段里也有「但当前未开启」字样，标记要用设备操控段特有的句子
CLOSED_MARK = "你具备「设备操控」能力（帮主人操作电脑找/读/发文件），但当前未开启"
s1 = build_sys(owner_msg(text="帮我看看电脑"), True, True)
chk("V6-1 循环开+主人+设备操控开：已开启版教学不在、工具清单里有 pc_*",
    OPEN_MARK not in s1 and "pc_read(path)" in s1 and "pc_open(target)" in s1,
    (OPEN_MARK in s1, "pc_read(path)" in s1))
s2 = build_sys(owner_msg(text="帮我看看电脑"), True, False)
chk("V6-2 循环开+主人+设备操控关：未开启版教学在、清单里没有 pc_*",
    CLOSED_MARK in s2 and "pc_read(path)" not in s2,
    (CLOSED_MARK in s2, "pc_read(path)" in s2))
s3 = build_sys(owner_msg(text="帮我看看电脑"), False, True)
_LINK_SPAN = ("（系统会自动开启授权码流程，标记不会显示给主人；"
              "正文用你的口吻先应一声，")
chk("V6-3/I3 循环关+主人+设备操控开：已开启版教学照旧在（与源码原文同文）",
    OPEN_MARK in s3 and _LINK_SPAN in s3 and _LINK_SPAN in SRC_A,
    (OPEN_MARK in s3, _LINK_SPAN in s3))
s4a = build_sys(non_owner_msg(text="帮我看看电脑"), True, True)
s4b = build_sys(non_owner_msg(text="帮我看看电脑"), True, False)
chk("V6-4/I4 非主人：两种设备开关状态下都没有设备操控教学也没有 pc_*",
    OPEN_MARK not in s4a and CLOSED_MARK not in s4a and "pc_read(path)" not in s4a
    and OPEN_MARK not in s4b and CLOSED_MARK not in s4b
    and "pc_read(path)" not in s4b, "")
chk("V6-5 循环关+主人+设备操控关：未开启版照旧（与从前一致）",
    CLOSED_MARK in build_sys(owner_msg(text="帮我看看电脑"), False, False), "")

# ============ V7 _handle_devctrl 收窄 ============

def dev_run(text, loop_on, ask_open=False, code="654321", ok_until=0.0,
            twofa=True, sender=OWNER_QQ):
    dm = NullDaemon()
    loops = []
    A._DEVCTRL_AUTH["ok_until"] = ok_until
    A._DEVCTRL_ASK["until"] = time.time() + 300 if ask_open else 0.0
    A._DEVCTRL_CODE.update({"code": code, "expires": time.time() + 300})
    A._DEVCTRL_SESSION.update({"msgs": [], "noticed": False})
    msg = owner_msg(text=text, sender_id=sender, chat_id=sender)
    with patched(A, owner_qq=lambda: OWNER_QQ, out=lambda line: None,
                 cfg_2fa_on=lambda: twofa,
                 _device_ctrl_enabled=lambda: True,
                 _tool_loop_on=lambda: loop_on,
                 devctrl_agent_loop=lambda d, m, r, f: loops.append(
                     str(m.get("text")))):
        handled = A._handle_devctrl(dm, msg)
    return (handled, dm.sent, loops, A._DEVCTRL_AUTH["ok_until"],
            A._DEVCTRL_ASK["until"])


h, sent, loops, ok_u, ask_u = dev_run("设备", True)
chk("V7a 循环开：关键词「设备」不再被截走（False、无话术、无索码、不进旧循环）",
    h is False and not sent and not loops and ask_u == 0.0 and ok_u == 0.0,
    (h, sent, loops, ask_u))
h, sent, loops, ok_u, ask_u = dev_run("654321", True, ask_open=True)
chk("V7b 循环开：索码窗口内 6 位授权码仍被认（窗口打开、发固定话术、不进旧循环）",
    h is True and ok_u > time.time() and loops == []
    and any("连线成功" in t for _c, _i, ts in sent for t in ts),
    (h, ok_u > time.time(), loops, [ts for _c, _i, ts in sent]))
h, sent, loops, ok_u, ask_u = dev_run("000000", True, ask_open=True)
chk("V7c 循环开：码不对 → 窗口不开、发失败话术",
    h is True and ok_u == 0.0 and loops == []
    and any(("不对" in t or "重新" in t) for _c, _i, ts in sent for t in ts),
    (h, ok_u, [ts for _c, _i, ts in sent]))
h, sent, loops, ok_u, ask_u = dev_run("帮我看看电脑", True,
                                      ok_until=time.time() + 120)
chk("V7d 循环开：授权窗口内的普通消息不再转进旧循环（False、旧循环桩零调用）",
    h is False and loops == [] and not sent, (h, loops, sent))
h, sent, loops, ok_u, ask_u = dev_run("设备", False)
chk("V7e/I3 循环关：关键词照旧触发索码（行为与改前一致）",
    h is True and ask_u > time.time() and ok_u == 0.0 and loops == []
    and any("授权码" in t for _c, _i, ts in sent for t in ts),
    (h, ask_u > time.time(), [ts for _c, _i, ts in sent]))
h, sent, loops, ok_u, ask_u = dev_run("帮我看看电脑", False,
                                      ok_until=time.time() + 120)
chk("V7f/I3 循环关：授权窗口内照旧转进旧循环（与改前一致）",
    h is True and loops == ["帮我看看电脑"], (h, loops))
h, sent, loops, ok_u, ask_u = dev_run("设备", False, twofa=False)
chk("V7g/I3 循环关：免码直通照旧（窗口打开 + 进旧循环，与改前一致）",
    h is True and ok_u > time.time() and loops == ["设备"],
    (h, ok_u > time.time(), loops))

# ============ V8 [连线] 标记（真实 process 驱动） ============

def process_mark(loop_on):
    """主人消息 + 模型回复带 [连线]，走真实 process()，返回 (发出的文字组,
    _devctrl_ask_by_mark 桩收到的次数)。"""
    dm = NullDaemon()
    A.PLUGIN_HOST.daemon = dm
    ask_calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        return "mx", "好嘞，我先连一下线\n[连线]"

    A._DEVCTRL_AUTH["ok_until"] = 0.0
    A._DEVCTRL_ASK["until"] = 0.0
    m8 = owner_msg(text="今天天气怎么样呀", message_id="v8-%s" % loop_on)
    with patched(A, PROMPT_V2_FORCE=False, REPLY_V2_FORCE=False,
                 REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
                 VCAP_FORCE=False, vision_enabled=lambda: False,
                 chat_anti_interrupt_enabled=lambda: True,
                 lean_ctx_on=lambda: True,
                 _cap_chat_allowed=lambda ct, cid: False,
                 call_glm_with_fallback=_chain,
                 owner_qq=lambda: OWNER_QQ,
                 _device_ctrl_enabled=lambda: True,
                 _tool_loop_on=lambda: loop_on,
                 _devctrl_ask_by_mark=lambda d, m: ask_calls.append(
                     str(m.get("chat_id")))):
        ALOG.clear()
        try:
            A.process(dm, m8)
        except Exception as exc:
            dm.sent = [(None, None, ["EXC:%r" % exc])]
    return dm.sent, ask_calls

sent8a, ask8a = process_mark(True)
joined8a = "".join(t for _c, _i, ts in sent8a for t in ts)
chk("V8a 循环开：[连线] 不触发索码（桩零调用）、标记照旧剥掉不漏进聊天",
    not ask8a and "[连线]" not in joined8a and "好嘞" in joined8a
    and not [l for l in ALOG if "AGENT_TOOL_STRIP_ERR" in l],
    (ask8a, joined8a[:80]))
sent8b, ask8b = process_mark(False)
joined8b = "".join(t for _c, _i, ts in sent8b for t in ts)
chk("V8b/I3 循环关：[连线] 照旧触发索码（桩 1 次）、标记照旧剥掉",
    len(ask8b) == 1 and "[连线]" not in joined8b, (ask8b, joined8b[:80]))

# ============ V9 非主人 ============

chk("V9a/I4 非主人 tools_for 清单里没有 pc_*",
    not [s["name"] for s in T.tools_for({"is_owner": False})
         if s["name"].startswith("pc_")],
    [s["name"] for s in T.tools_for({"is_owner": False})])
STEP_CALLS.clear()
r9 = run_named("pc_read", {"path": "C:\\a.txt"}, opts={"auth_ok": True},
               msg=non_owner_msg(), is_owner=False)
chk("V9b/I4 非主人 run_tool 被权限闸拒：桩零调用",
    r9["ok"] is False and "主人" in r9["summary"] and STEP_CALLS == [],
    (r9["summary"], STEP_CALLS))
h, sent, loops, ok_u, ask_u = dev_run("设备", True, sender=NOT_OWNER)
chk("V9c/I4 非主人：_handle_devctrl 仍然第一闸拒绝（循环开也一样）",
    h is False and not sent and not loops and ok_u == 0.0,
    (h, sent, loops))
h, sent, loops, ok_u, ask_u = dev_run("设备", False, sender=NOT_OWNER)
chk("V9d/I4 非主人：循环关时同样第一闸拒绝（与从前一致）",
    h is False and not sent and not loops, (h, sent, loops))

# ============ I1/I4 源码级钉住 ============

chk("I1a 源码级：八个 run 全是同一个薄包装（只调 step_fn，一处调用点）",
    SRC_TB.count("def _devctrl_run") == 1
    and SRC_TB.count("step_fn(daemon, reply_to, action, arg_of(args))") == 1
    and SRC_TB.count('_devctrl_run(step_fn, enabled_fn, "') == 8, "")
chk("I1b 源码级：qq_tools_builtin 里没有第二套防护实现（不开程序/不杀进程/"
    "不做保护目录判断）",
    all(m not in SRC_TB for m in ("os.startfile", "taskkill", "tasklist",
                                  "webbrowser", "_is_protected",
                                  "_DEVCTRL_READ_DENY", "_SYS_PROC_BLOCK")), "")
chk("I1c 源码级：四道防护仍在 qq_bot_agent（没有被搬走/删除）",
    all(m in SRC_A for m in ("def _open_guard", "def _is_protected",
                             "_SYS_PROC_BLOCK", "_DEVCTRL_READ_DENY")), "")
chk("I1d main() 把 _devctrl_step 与 _device_ctrl_enabled 接进 register_builtin",
    "devctrl_step_fn=_devctrl_step" in SRC_A
    and "devctrl_enabled_fn=_device_ctrl_enabled" in SRC_A, "")
chk("I2a 源码级：pc_* 的 run 第一行就是开关检查（关着时任何路径都执行不了）",
    "if not enabled_fn():" in SRC_TB
    and SRC_TB.index("if not enabled_fn():")
    < SRC_TB.index("reply_to = (\"private\""), "")
chk("I3a 源码级：循环关时 _handle_devctrl 走原路径（收窄只在 _tool_loop_on() "
    "为真时接管，判断在最前面、尽早返回）",
    "if _tool_loop_on():\n        # B2a 收窄" in SRC_A
    and "return _handle_devctrl_link_only(daemon, msg)" in SRC_A, "")
chk("I3b 源码级：[连线] 只在工具循环关时触发索码（标记剥除无条件照旧）",
    "and not _tool_loop_on():" in SRC_A
    and SRC_A.count("reply = _DEVC_MARK_ANY_RE.sub(\"\", reply)") == 1, "")
chk("I3c 源码级：教学替换只在 _tool_teach（循环开+主人）且设备开着时跳过已开启版",
    "elif _tool_teach and _device_ctrl_enabled():" in SRC_A, "")
chk("I4b 源码级：清单与循环两处都过 _tool_available 过滤",
    SRC_A.count("if _tool_available(s.get(\"name\"))") == 2, "")

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
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
