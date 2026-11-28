# -*- coding: utf-8 -*-
"""冒烟：E1 能力评测集与跑法（eval/set_capability.jsonl + eval/capability_run.py）
——全部离线：不调真实模型、不读生产数据目录里的任何文件。

覆盖（对应派单验收）：
  T1  题库合法：60 行、能力内/能力外各 30、id 唯一、必填字段齐、
      chat_type/is_owner/at_me 组合自洽（私聊恒 is_owner=true、at_me=false）；
      能力内每条 expect 带 tool 或 intent、能力外每条 expect 为
      {forbid_tools, no_claim}。
  T2  题库卫生：没有真实人名/群号/QQ 号——正文里所有 5–11 位数字串必须
      是白名单假号（10001/10002/10003 或 5550xx）；网址只许 example.com /
      example.org（http/https）；不含任何厂商模型名（黑名单词表）。
  T3  switches 里的键都是主程序真有的键（在 qq_bot_agent.py 源码里能找到
      该键名字面量）；"_mcp" 是跑法专用键，单独白名单；app 里的键在
      qq_common.py 源码里能找到。
  T4  参数子集匹配规则（capability_run.args_subset_reason）：数字归一
      （"8" vs 8 过；"8:30" vs "08:30" 过；"9:0" vs "08:30" 不过）、
      期望键缺失算错、多出来的键不算错、非对象参数算错。
  T5  桩登记覆盖 register_builtin 当时全部可选执行函数参数
      （inspect.signature 对照 build_tool_stubs，少一个就红），且登记后
      每个能力内 expect.tool / 指令名都能在登记表/指令表里找到。
  T6  重置清单：reset_state() 之后逐项核对确实被清空/还原（先把各状态
      弄脏再重置）。
  T7  --smoke 端到端：子进程跑
      `python eval/capability_run.py --smoke --tag smoketest --out-dir <临时目录>
       --only cin05,cin11,cin27,cout01,cout20,cout23`
      （3 能力内＋3 能力外），退出码 0、结果文件落在临时目录（不是
      ~/.zcode-qq-bot）、6 条全 pass、能力内 tools_called 有记录、
      能力外 tools_called 为空；跑完核对 ~/.zcode-qq-bot/eval/runs 下
      没有产生 capability_smoketest 文件。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照既有冒烟
的手法）；capability_run 在 import 时零副作用（只有 main() 才建隔离目录），
本冒烟只调它的纯逻辑与桩登记；端到端走子进程，其隔离由 capability_run
自己的 main() 完成。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_capability_run.py
"""
import glob
import inspect
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_capability_smoke_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "eval"))

import qq_bot_agent as A  # noqa: E402
import qq_commands as K   # noqa: E402
import qq_tools as T      # noqa: E402
import qq_tools_builtin as TB  # noqa: E402
import capability_run as CR   # noqa: E402

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


SET_PATH = os.path.join(_REPO, "eval", "set_capability.jsonl")
ROWS = [json.loads(l) for l in open(SET_PATH, encoding="utf-8") if l.strip()]
RUNER_SRC = open(os.path.join(_REPO, "eval", "capability_run.py"),
                 encoding="utf-8").read()
AGENT_SRC = open(os.path.join(_REPO, "plugins", "qq-bot", "scripts",
                              "qq_bot_agent.py"), encoding="utf-8").read()
COMMON_SRC = open(os.path.join(_REPO, "plugins", "qq-bot", "scripts",
                               "qq_common.py"), encoding="utf-8").read()

# ============ T1 题库结构 ============

ins = [r for r in ROWS if r.get("kind") == "in"]
outs = [r for r in ROWS if r.get("kind") == "out"]
ids = [r["id"] for r in ROWS]
chk("T1a 恰好 60 行、能力内 30 + 能力外 30、id 唯一",
    len(ROWS) == 60 and len(ins) == 30 and len(outs) == 30
    and len(set(ids)) == 60, (len(ROWS), len(ins), len(outs)))
REQ = ("id", "kind", "chat_type", "is_owner", "at_me", "text", "switches",
       "expect")
bad_fields = [r["id"] for r in ROWS
              if any(k not in r for k in REQ) or r["kind"] not in ("in", "out")
              or r["chat_type"] not in ("private", "group")]
chk("T1b 每行必填字段齐（id/kind/chat_type/is_owner/at_me/text/switches/"
    "expect）且 kind/chat_type 合法", not bad_fields, bad_fields)
bad_combo = [r["id"] for r in ROWS
             if (r["chat_type"] == "private")
             != (r["is_owner"] is True and r["at_me"] is False)]
chk("T1c 私聊恒主人且不被@、群聊按字段声明（组合自洽）", not bad_combo, bad_combo)
bad_in = [r["id"] for r in ins
          if not ("tool" in r["expect"] or "intent" in r["expect"])]
bad_out = [r["id"] for r in outs
           if r["expect"] != {"forbid_tools": True, "no_claim": True}]
chk("T1d 能力内 expect 带 tool/intent；能力外 expect 恰为 forbid_tools+no_claim",
    not bad_in and not bad_out, (bad_in, bad_out))

# 覆盖面：能力内工具/指令清单（派单点名的每项至少两条，指令类允许单条覆盖）
tool_counts = {}
for r in ins:
    e = r["expect"]
    key = e.get("tool") or ("intent:" + e["intent"])
    tool_counts[key] = tool_counts.get(key, 0) + 1
WANT_COVER = ("web_search", "react", "send_group", "draw", "chat_search",
              "web_fetch", "mcp_call")
missing_cover = [k for k in WANT_COVER if tool_counts.get(k, 0) < 2]
intent_names = {r["expect"]["intent"] for r in ins if "intent" in r["expect"]}
chk("T1e 覆盖面：工具类各 ≥2 条（联网搜索/表情回应/发到群/画图/检索/网页/MCP）",
    not missing_cover, (missing_cover, tool_counts))
chk("T1f 覆盖面：指令覆盖定时新增与列表、查询类（状态/用量/群列表/日志/安静/"
    "继续）与六条会改设置的指令（帮助、人设列表两条让位给跨群检索与 MCP 的"
    "第二条——每项≥2 优先）",
    {"cron_add", "cron_list", "status", "usage", "group_list", "log",
     "quiet", "unquiet", "group_add", "group_remove", "feature_toggle",
     "pause", "resume", "persona_switch"} <= intent_names,
    sorted(intent_names))
n_owner_at = sum(1 for r in ins if r["chat_type"] == "group")
chk("T1g 能力内以主人私聊为主、少数群里@（2–8 条）",
    30 - n_owner_at >= 20 and 2 <= n_owner_at <= 8, n_owner_at)

# ============ T2 题库卫生 ============

NUM_RE = re.compile(r"(?<!\d)(\d{5,11})(?!\d)")
ALLOWED_NUM = {"10001", "10002", "10003"}
BAD_NUM = []
URL_RE = re.compile(r"(https?://[^\s「」，。！？]+)")
BAD_URL = []
for r in ROWS:
    blob = json.dumps(r, ensure_ascii=False)
    for m in NUM_RE.finditer(blob):
        n = m.group(1)
        if n not in ALLOWED_NUM and not re.fullmatch(r"5550[0-9]{2}", n):
            BAD_NUM.append((r["id"], n))
    for m in URL_RE.finditer(str(r.get("text") or "")):
        u = m.group(1)
        if "example.com" not in u and "example.org" not in u:
            BAD_URL.append((r["id"], u))
chk("T2a 正文里 5–11 位数字串只出现在白名单假号（10001/10002/10003、5550xx）",
    not BAD_NUM, BAD_NUM)
chk("T2b 网址只许 example.com / example.org（http/https）", not BAD_URL, BAD_URL)
VENDOR_TOKENS = ("glm", "gpt", "claude", "gemini", "deepseek", "qwen",
                 "kimi", "doubao", "hunyuan", "moonshot", "zhipu", "bigmodel",
                 "openai", "anthropic")
hits = []
for r in ROWS:
    low = json.dumps(r, ensure_ascii=False).lower()
    for tk in VENDOR_TOKENS:
        if tk in low:
            hits.append((r["id"], tk))
chk("T2c 题库不含任何厂商模型名（黑名单词表）", not hits, hits)

# ============ T3 开关键都是主程序真有的键 ============

MCP_KEY_OK = "_mcp"
bad_sw = []
for r in ROWS:
    for k in (r.get("switches") or {}):
        if k == MCP_KEY_OK:
            continue
        if ('"%s"' % k) not in AGENT_SRC and ("'%s'" % k) not in AGENT_SRC:
            bad_sw.append((r["id"], k))
bad_app = []
for r in ROWS:
    for k in (r.get("app") or {}):
        if ('"%s"' % k) not in COMMON_SRC and ("'%s'" % k) not in COMMON_SRC:
            bad_app.append((r["id"], k))
chk("T3a switches 键都在 qq_bot_agent.py 源码里（_mcp 为跑法专用键）",
    not bad_sw, bad_sw)
chk("T3b app 键都在 qq_common.py 源码里", not bad_app, bad_app)
sw_off = [r["id"] for r in outs if "tool_web_fetch_enabled" in r["switches"]
          or "tool_cross_chat_search" in r["switches"]]
chk("T3c 开关关着的能力外条目显式带 false 开关", len(sw_off) >= 2, sw_off)

# ============ T4 参数子集匹配 ============

def sub(exp, got):
    return CR.args_subset_reason(exp, got)


chk("T4a 数字归一：\"8\" 与 8 相等、\"8:30\" 与 \"08:30\" 相等",
    sub({"time": "08:30"}, {"time": "8:30"}) == ""
    and sub({"n": 8}, {"n": "8"}) == "",
    (sub({"time": "08:30"}, {"time": "8:30"}), sub({"n": 8}, {"n": "8"})))
chk("T4b 时间差不算过：\"9:0\"→09:00 与 \"08:30\" 不等",
    sub({"time": "08:30"}, {"time": "9:0"}) != "",
    sub({"time": "08:30"}, {"time": "9:0"}))
chk("T4c 期望键缺失算错", sub({"target": "555002"}, {"text": "x"}) != "", "")
chk("T4d 多出来的键不算错",
    sub({"target": "555002"}, {"target": "555002", "text": "x", "extra": 1}) == "",
    "")
chk("T4e 值不相等算错、非对象参数算错",
    sub({"query": "abc"}, {"query": "xyz"}) != ""
    and sub({}, "not-a-dict") != "", "")

# ============ T5 桩登记覆盖 register_builtin 全部可选执行函数参数 ============

sig_names = [p.name for p in
             inspect.signature(TB.register_builtin).parameters.values()
             if p.name.endswith("_fn") and p.name != "search_web_fn"]
# 不真正登记（登记留给 capability_run.main）——只对照参数名集合
CR._load_modules()
CR._install_owner_stubs()
stubs = CR.build_tool_stubs()
chk("T5a build_tool_stubs 覆盖 register_builtin 当时全部可选执行函数参数",
    set(sig_names) <= set(stubs),
    [n for n in sig_names if n not in stubs])
SW_FN_ATTR = {"devctrl_enabled_fn": "_device_ctrl_enabled",
              "mcp_allowed_fn": "_mcp_tool_allowed",
              "gadmin_enabled_fn": "_gadmin_tool_enabled",
              "web_fetch_enabled_fn": "_web_fetch_tool_enabled",
              "group_files_enabled_fn": "_group_files_tool_enabled"}
chk("T5b 开关类参数绑定主程序自己的读取函数（读取路径真实）",
    all(stubs[k] is getattr(A, attr) for k, attr in SW_FN_ATTR.items()
        if k in stubs), "")
TB.register_builtin(CR._stub_search_web, **stubs)
reg_names = {s["name"] for s in T.tools_for({"is_owner": True})}
need_tools = {r["expect"]["tool"] for r in ins if "tool" in r["expect"]}
chk("T5c 登记后能力内期望的每个工具都在表里", need_tools <= reg_names,
    sorted(need_tools - reg_names))
chk("T5d 能力内期望的每条指令都在指令表且开放",
    all(K.by_name(r["expect"]["intent"]) is not None
        and K.by_name(r["expect"]["intent"])["intent"] is True
        for r in ins if "intent" in r["expect"]), "")

# ============ T6 重置清单逐项核对 ============

# 先把各状态弄脏
A._TOOL_PENDING["private:10001"] = {"kind": "cmd"}
A._DONE_RECENT["m1"] = 1.0
A._DONE_SKIP_LOGGED.add("m1")
A._INFLIGHT.add("m1")
A._DEVCTRL_CODE.update({"code": "123456", "expires": 9e9})
A._DEVCTRL_AUTH.update({"ok_until": 9e9})
A._DEVCTRL_ASK.update({"until": 9e9})
A._DEVCTRL_SESSION.update({"msgs": [{"x": 1}], "noticed": True})
A._TOPIC_LAST["group:555001"] = {"topic": "x"}
A._NUDGE_STATE["group:555001"] = {"strikes": 3}
A._MCP_IDX_CACHE.update({"ts": 9e9, "rows": ["stale"]})
A._task_hist.append(1.0)
A._task_busy["flag"] = True
A._VISION_PROBE.update({"cls": {"m": 1}, "ts": 9e9})
A._NO_VISION_MODELS.add("m")
A._VISION_OK_MODELS.add("m")
with A._CAP["lock"]:
    A._CAP["by_mid"]["x"] = {"texts": ["x"], "ts": 1.0}
    A._CAP["pending"].add("x")
with A.RT.lock:
    A.RT.cooldowns["main:m"] = 9e9
    A.RT.processed = 5
mem_path = getattr(A, "MEMORY_STREAM_PATH", "")
with open(mem_path, "a", encoding="utf-8") as f:
    f.write('{"t": "脏数据"}\n')

CR.reset_state()
chk("T6a 待确认表/防重复表/在途表已清空",
    A._TOOL_PENDING == {} and A._DONE_RECENT == {}
    and not A._DONE_SKIP_LOGGED and not A._INFLIGHT, "")
chk("T6b 设备操控授权码/索码窗口/会话已还原",
    A._DEVCTRL_CODE["code"] == "" and A._DEVCTRL_AUTH["ok_until"] == 0.0
    and A._DEVCTRL_ASK["until"] == 0.0 and A._DEVCTRL_SESSION["msgs"] == [],
    "")
chk("T6c 话题缓存/追聊状态/MCP 索引缓存/任务桥状态已清空",
    A._TOPIC_LAST == {} and A._NUDGE_STATE == {}
    and A._MCP_IDX_CACHE["rows"] is None and A._task_hist == []
    and A._task_busy["flag"] is False, "")
chk("T6d 识图缓存与模型冷却/计数已清空",
    A._VISION_PROBE["cls"] == {} and not A._NO_VISION_MODELS
    and not A._VISION_OK_MODELS and A.RT.cooldowns == {}
    and A.RT.processed == 0, "")
with A._CAP["lock"]:
    cap_clean = not A._CAP["by_mid"] and not A._CAP["pending"] \
        and A._CAP["n"] == 0 and not A._CAP["main_busy"]
chk("T6e 链接/速写去重表已清空", cap_clean, "")
chk("T6f 记忆流水文件已删（临时目录内，跨场景不注入）",
    not os.path.isfile(mem_path), mem_path)
chk("T6g CURRENT 收集器已清空",
    CR.CURRENT["calls"] == [] and CR.CURRENT["logs"] == []
    and CR.CURRENT["rec"] == [], "")

# ============ T7 --smoke 端到端（子进程，3 能力内 + 3 能力外） ============

out_dir = tempfile.mkdtemp(prefix="qqbot_capability_smoke_out_")
env = dict(os.environ)
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONIOENCODING"] = "utf-8"
sel = "cin05,cin11,cin27,cout01,cout20,cout23"
proc = subprocess.run(
    [sys.executable, os.path.join(_REPO, "eval", "capability_run.py"),
     "--smoke", "--tag", "smoketest", "--out-dir", out_dir, "--only", sel],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=_REPO, env=env, timeout=600)
tail = (proc.stdout or "").strip().splitlines()[-12:]
print("---- capability_run --smoke 输出尾部 ----")
for ln in tail:
    print("  " + ln)
chk("T7a --smoke 子进程退出码 0", proc.returncode == 0,
    (proc.returncode, (proc.stderr or "")[-300:]))
out_file = os.path.join(out_dir, "capability_smoketest.jsonl")
rows_out = []
if os.path.isfile(out_file):
    rows_out = [json.loads(l) for l in open(out_file, encoding="utf-8")
                if l.strip()]
chk("T7b 结果文件落在 --out-dir（6 行）", len(rows_out) == 6, len(rows_out))
chk("T7c 六条全 pass、无 NEEDS_USER、无 error",
    rows_out and all(r.get("pass") and not r.get("needs_user")
                     and not r.get("error") for r in rows_out),
    [(r["id"], r.get("pass"), r.get("fail_reason") or r.get("error"))
     for r in rows_out])
in_out = [r for r in rows_out if r["kind"] == "in"]
out_out = [r for r in rows_out if r["kind"] == "out"]
chk("T7d 工具型能力内有执行记录、指令型能力内有 HIT 日志、能力外 tools_called 为空",
    all(r["tools_called"] for r in in_out if "tool" in r["expect"])
    and all(any(l.startswith("AGENT_INTENT_HIT")
                for l in r["intent_or_cmd_logs"])
            for r in in_out if "intent" in r["expect"])
    and all(not r["tools_called"] and not r["intent_or_cmd_logs"]
            for r in out_out),
    [(r["id"], [c["tool"] for c in r["tools_called"]],
      r["intent_or_cmd_logs"][:1]) for r in rows_out])
chk("T7e 能力内有回复、能力外判定 NO_CLAIM 留档",
    all(r["replies"] for r in in_out)
    and all((r.get("judge_raw") or "") == "NO_CLAIM" for r in out_out),
    [(r["id"], r.get("judge_raw")) for r in out_out])
prod_hits = glob.glob(os.path.join(os.path.expanduser("~"), ".zcode-qq-bot",
                                   "eval", "runs", "capability_smoketest*"))
chk("T7f 默认输出目录（生产评测目录）没有产生 smoketest 文件",
    not prod_hits, prod_hits)
chk("T7g 隔离目录清理无残留告警（输出里没有「没能删净」）",
    "没能删净" not in (proc.stdout or ""), "")

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
shutil.rmtree(out_dir, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
