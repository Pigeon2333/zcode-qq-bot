# -*- coding: utf-8 -*-
"""冒烟：审计 B15——停止 LLOneBot 不再依赖 wmic。

全离线：QQBOT_DATA_DIR 指到系统临时目录（qq_common 在 import 时读它）；
subprocess.run / subprocess.Popen 全程打桩，不真跑 powershell/taskkill/
wmic；不连任何端口、不启停任何生产进程、不碰生产目录。
覆盖：Popen 在手直接结束（不走 CIM）、Popen 不在时 PowerShell 参数列表
形状（固定脚本常量、无外部输入、shell=False）、样例输出解析出 PID 后逐个
taskkill /PID … /T /F（多 PID 逐个处理、指纹不中不放行）、PowerShell 缺失/
超时不杀并记 LLONEBOT_STOP_FAIL、没筛到/输出解析失败也不杀、句柄已退出
落 CIM 扫尾、start_llonebot 记录拉起句柄且启动命令保持原逻辑。"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "qq-bot" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qqbot-smoke-llobstop-"))
os.environ["QQBOT_DATA_DIR"] = str(_TMP / "data")   # 必须在 import qq_common 前
import qq_common as QC  # noqa: E402
import qq_services as S  # noqa: E402

fails = []
ps_scripts = []          # 全程见过的 PowerShell 脚本文本（钉「固定无外部输入」）


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def llob_cfg(name):
    d = _TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return {"llonebot_dir": str(d), "llonebot_ws_port": 3002}


def log_text():
    p = pathlib.Path(QC.DATA_DIR) / "logs" / "llonebot.log"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class FakeCompleted:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


class FakeProc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


def make_runner(ps_stdout=b"", ps_exc=None, tk_rc=0):
    calls = []

    def fake_run(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs})
        head = args[0] if args else ""
        if head == "powershell":
            ps_scripts.append(args[4] if len(args) > 4 else None)
            if ps_exc is not None:
                raise ps_exc
            return FakeCompleted(stdout=ps_stdout)
        if head == "taskkill":
            return FakeCompleted(returncode=tk_rc)
        raise AssertionError("意外的进程调用：%r" % (args,))

    return calls, fake_run


# --- 场景 A：Popen 在手 → 直接 taskkill /T /F，不走 CIM ---
cfgA = llob_cfg("a")
S._LLOB_PROC = FakeProc(pid=4242, returncode=None)
calls, runner = make_runner()
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgA = S.stop_llonebot(cfgA)
tk = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
psA = [c for c in calls if c["args"] and c["args"][0] == "powershell"]
chk("A1 只调用一次 taskkill，不查 CIM", len(tk) == 1 and not psA)
chk("A2 参数为 taskkill /PID 4242 /T /F",
    tk and tk[0]["args"] == ["taskkill", "/PID", "4242", "/T", "/F"])
chk("A3 shell=False", all(c["kwargs"].get("shell") is False for c in calls))
chk("A4 返回成功文案", "已停止" in str(msgA) and "失败" not in str(msgA))
chk("A5 句柄用后清空", S._LLOB_PROC is None)

# --- 场景 B：Popen 不在 → PowerShell 参数形状 + 解析 PID + 逐个 taskkill ---
cfgB = llob_cfg("b")
base_b = str(cfgB["llonebot_dir"])
sample = json.dumps([
    {"ProcessId": 111, "CommandLine": "node D:\\other\\app.js",
     "ExecutablePath": "C:\\Program Files\\nodejs\\node.exe"},
    {"ProcessId": 222,
     "CommandLine": 'node "%s\\dist\\llbot.js" --port 3002' % base_b,
     "ExecutablePath": "C:\\Program Files\\nodejs\\node.exe"},
    {"ProcessId": 333, "CommandLine": "",
     "ExecutablePath": base_b + "\\node.exe"},
    {"ProcessId": 444, "CommandLine": "node C:\\else\\llonebot\\run.js",
     "ExecutablePath": "C:\\n\\node.exe"},
], ensure_ascii=True).encode("ascii")
calls, runner = make_runner(ps_stdout=sample)
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgB = S.stop_llonebot(cfgB)
psB = [c for c in calls if c["args"] and c["args"][0] == "powershell"]
tkB = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("B1 powershell 为参数列表且带 -NoProfile -NonInteractive -Command",
    len(psB) == 1 and psB[0]["args"][:4] ==
    ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
chk("B2 脚本=固定常量，不含外部输入（目录指纹未拼进脚本）",
    psB and psB[0]["args"][4] == S._LLOB_PS_QUERY
    and base_b not in psB[0]["args"][4])
chk("B3 shell=False", all(c["kwargs"].get("shell") is False for c in calls))
chk("B4 只对指纹命中的 PID（222/333/444）执行 taskkill，放过无关 111",
    sorted(c["args"][2] for c in tkB) == ["222", "333", "444"])
chk("B5 每个 PID 都是 taskkill /PID <pid> /T /F",
    all(c["args"] == ["taskkill", "/PID", c["args"][2], "/T", "/F"]
        for c in tkB))
chk("B6 多 PID 逐个处理且返回成功文案",
    len(tkB) == 3 and "已停止" in str(msgB) and "失败" not in str(msgB))

# --- 场景 C：PS 只筛到一个时输出裸对象（非数组）也要能解析 ---
cfgC = llob_cfg("c")
single = json.dumps({"ProcessId": 555,
                     "CommandLine": 'node "%s\\dist\\llbot.js"'
                                    % cfgC["llonebot_dir"],
                     "ExecutablePath": ""},
                    ensure_ascii=True).encode("ascii")
calls, runner = make_runner(ps_stdout=single)
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgC = S.stop_llonebot(cfgC)
tkC = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("C1 裸对象输出解析出 PID 并结束",
    [c["args"][2] for c in tkC] == ["555"] and "已停止" in str(msgC))

# --- 场景 D：PowerShell 不可用（FileNotFoundError）→ 不杀 + 记日志 + 返回失败 ---
cfgD = llob_cfg("d")
calls, runner = make_runner(
    ps_exc=FileNotFoundError(2, "系统找不到指定的文件。"))
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgD = S.stop_llonebot(cfgD)
tkD = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
logD = log_text()
chk("D1 不调用 taskkill", not tkD)
chk("D2 记 LLONEBOT_STOP_FAIL 日志",
    "LLONEBOT_STOP_FAIL" in logD and "PowerShell 不可用" in logD)
chk("D3 返回失败文案", "失败" in str(msgD))

# --- 场景 E：PowerShell 查询超时 → 同样不杀 + 记日志 + 返回失败 ---
cfgE = llob_cfg("e")
calls, runner = make_runner(
    ps_exc=subprocess.TimeoutExpired(cmd="powershell", timeout=10))
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgE = S.stop_llonebot(cfgE)
tkE = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
logE = log_text()
chk("E1 超时不调用 taskkill", not tkE)
chk("E2 记 LLONEBOT_STOP_FAIL 超时原因",
    "LLONEBOT_STOP_FAIL" in logE and "超时" in logE)
chk("E3 返回失败文案", "失败" in str(msgE))

# --- 场景 F：筛到 0 个 PID → 不杀，记日志说明原因，不装作已停止 ---
cfgF = llob_cfg("f")
calls, runner = make_runner(ps_stdout=b"[null]")
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgF = S.stop_llonebot(cfgF)
tkF = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
logF = log_text()
chk("F1 没筛到不调用 taskkill", not tkF)
chk("F2 记日志说明原因（含安装目录）",
    "LLONEBOT_STOP_FAIL" in logF and "未筛到" in logF
    and str(cfgF["llonebot_dir"]) in logF)
chk("F3 不谎报已停止", "已停止" not in str(msgF) and "未发现" in str(msgF))

# --- 场景 G：输出解析失败（非 JSON）→ 不杀 + 记日志 + 返回失败 ---
cfgG = llob_cfg("g")
calls, runner = make_runner(ps_stdout=b"NOT_JSON")
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgG = S.stop_llonebot(cfgG)
tkG = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
logG = log_text()
chk("G1 输出异常不调用 taskkill", not tkG)
chk("G2 记 LLONEBOT_STOP_FAIL 解析失败",
    "LLONEBOT_STOP_FAIL" in logG and "解析" in logG)
chk("G3 返回失败文案", "失败" in str(msgG))

# --- 场景 H：start_llonebot 记录拉起句柄，启动命令保持原逻辑 ---
cfgH = llob_cfg("h")
h_dir = pathlib.Path(cfgH["llonebot_dir"])
(h_dir / "dist").mkdir(parents=True, exist_ok=True)
(h_dir / "dist" / "llbot.js").write_bytes(b"")
(h_dir / "node.exe").write_bytes(b"")
captured = {}


class FakePopen:
    def __init__(self, args, **kwargs):
        captured["args"] = list(args)
        kw = dict(kwargs)
        if hasattr(kw.get("stdout"), "name"):
            # 只留文件名：钉住文件对象会让日志句柄漏到进程结束，rmtree 失败
            kw["stdout"] = kw["stdout"].name
        captured["kwargs"] = kw
        self.pid = 9999
        self.returncode = None

    def poll(self):
        return None


with mock.patch.object(S.subprocess, "Popen", FakePopen):
    okH, _msgH = S.start_llonebot(cfgH)
chk("H1 启动成功且句柄被记录", okH and isinstance(S._LLOB_PROC, FakePopen))
chk("H2 启动命令保持原逻辑（自带 node + dist/llbot.js 入口）",
    captured["args"][0] == str(h_dir / "node.exe")
    and captured["args"][1] == str(h_dir / "dist" / "llbot.js"))
chk("H3 cwd=安装目录，CREATE_NO_WINDOW 保持",
    captured["kwargs"]["cwd"] == str(h_dir)
    and captured["kwargs"]["creationflags"] == S.CREATE_NO_WINDOW)

# --- 场景 I：句柄在手但进程已退出 → 落 CIM 扫尾 ---
cfgI = llob_cfg("i")
S._LLOB_PROC = FakeProc(pid=777, returncode=1)
calls, runner = make_runner(ps_stdout=b"[null]")
with mock.patch.object(S.subprocess, "run", side_effect=runner):
    msgI = S.stop_llonebot(cfgI)
heads = [c["args"][0] for c in calls]
chk("I1 已退出的句柄不直接杀，落 CIM 扫尾",
    heads == ["powershell"] and "未发现" in str(msgI))
chk("I2 句柄清空", S._LLOB_PROC is None)

# --- 全局不变量 ---
import inspect  # noqa: E402
src = inspect.getsource(S.stop_llonebot) + inspect.getsource(
    S._llonebot_fingerprint_pids)
chk("Y1 停止逻辑不再调用 wmic、不用 shell=True",
    '"wmic"' not in src and "'wmic'" not in src
    and "shell=True" not in src)
chk("Y2 全程 PowerShell 脚本唯一且等于固定常量",
    ps_scripts and set(ps_scripts) == {S._LLOB_PS_QUERY})

# --- 清理：只删本脚本建的前缀临时目录；句柄不得残留 ---
chk("Z1 测试结束句柄不残留", S._LLOB_PROC is None)
for _i in range(3):          # Windows 上刚写完的文件可能被杀软短暂锁住
    try:
        shutil.rmtree(_TMP)
        break
    except OSError as _e:
        if _i == 2:
            print("清理临时目录失败（残留 %s）：%r" % (_TMP, _e))
print()
print("%d 项通过，%d 项失败" % (32 - len(fails), len(fails)))
sys.exit(1 if fails else 0)
