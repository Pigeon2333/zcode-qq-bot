# -*- coding: utf-8 -*-
"""冒烟：语音引擎归属判定按引擎端口占用（Z-2），不再只看本进程句柄。

全离线：QQBOT_DATA_DIR 指到系统临时目录（qq_common 在 import 时读它）；
qq_tts._gsv_port_open（TCP 探测）与 _gsv_port_listener（占用者查询）全程
打桩，subprocess.run / subprocess.Popen 打桩（不真跑 taskkill / powershell、
不真连任何端口）；不启动、不停止任何真实引擎，不碰生产目录与
~/.zcode-qq-bot。
覆盖（派单四条 + 发回 1）：
  - 路径在引擎 runtime 目录内 → running/ours 真，gsv_stop 按该 PID 结束
  - 路径在目录外 → ours 假，gsv_stop 一律不动（返回原有说明）
  - 端口没人听 → running 假，且不触发占用者查询
  - 取路径失败（空路径）→ ours 假
  - running 为真时 3 秒内连续两次状态查询只查一次占用者（缓存）
  - running 由真变假 → 缓存里的占用者清掉
  - 保活检查那条路径不查占用者、不起任何子进程
  - gsv_start 端口被占不重复启动（开头判断零占用者查询）；runtime 外但
    gsv 目录内不算 ours（钉住「gsv 目录下的 runtime」这个范围）；路径
    大小写/斜杠不敏感。
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "qq-bot" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qqbot-smoke-gsvowner-"))
os.environ["QQBOT_DATA_DIR"] = str(_TMP / "data")   # 必须在 import qq_common 前
import qq_common as QC  # noqa: E402
import qq_tts as T  # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    if cond:
        n_pass += 1
        print("PASS " + name)
    else:
        fails.append(name)
        tail = (" | " + str(extra)) if extra else ""
        print("FAIL " + name + tail)


# 假引擎目录：<tmp>/gsv/runtime/python.exe（文件只是占位，供 dir_ok 判断）
GSV = _TMP / "gsv"
(GSV / "runtime").mkdir(parents=True, exist_ok=True)
(GSV / "runtime" / "python.exe").write_bytes(b"")
(GSV / "api_v2.py").write_bytes(b"")
CFG = {"gsv_dir": str(GSV), "gsv_port": 9880}
IN_PATH = str(GSV / "runtime" / "python.exe")
OUT_PATH = r"C:\Windows\System32\notepad.exe"
NOTE = "外部启动的进程不受管理"


class FakeCompleted:
    def __init__(self, returncode=0):
        self.stdout = b""
        self.stderr = b""
        self.returncode = returncode


def stub_listener(results):
    """_gsv_port_listener 的假身：按调用次序返回 (pid, path)，末项兜底。"""
    state = {"calls": 0}

    def fake(port):
        i = state["calls"]
        state["calls"] += 1
        return results[i] if i < len(results) else results[-1]
    return state, fake


def const_open(value):
    """_gsv_port_open 的假身：恒返回 value（running 的来源），记调用次数。"""
    state = {"calls": 0}

    def fake(port, timeout=0.3):
        state["calls"] += 1
        return value
    return state, fake


def reset_cache():
    T._GSV_OURS_CACHE.clear()


def make_run_guard():
    calls = []

    def fake_run(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs})
        return FakeCompleted(returncode=0)
    return calls, fake_run


def patched(open_value, results, guard):
    ostate, ostub = const_open(open_value)
    lstate, lstub = stub_listener(results)
    return ostate, lstate, \
        mock.patch.object(T, "_gsv_port_open", ostub), \
        mock.patch.object(T, "_gsv_port_listener", lstub), \
        mock.patch.object(subprocess, "run", side_effect=guard)


# --- 场景 A：占用进程可执行文件在引擎 runtime 内 → ours 真，stop 按 PID 结束 ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH)], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("A1 端口连得上 → running 真（TCP 探测）；端口取配置值 9880",
    st["running"] is True and st["port"] == 9880, st)
chk("A2 可执行文件在 runtime 内 → ours 真", st["ours"] is True, st)
chk("A3 dir_ok 判定不受影响", st["dir_ok"] is True, st)
chk("A3b running 真时才查占用者（查询恰 1 次，写入缓存）",
    lstate["calls"] == 1 and ostate["calls"] == 1,
    (lstate["calls"], ostate["calls"]))
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH), (0, "")], guard)
reset_cache()
with p1, p2, p3:
    r = T.gsv_stop(CFG)
tk = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("A4 stop：返回 ok 真、stopped 真", r.get("ok") is True and r.get("stopped") is True, r)
chk("A5 stop：按 PID 结束（taskkill /PID 4242 /T /F），不按映像名",
    len(tk) == 1 and tk[0]["args"] == ["taskkill", "/PID", "4242", "/T", "/F"],
    tk)
chk("A6 stop：不拼 shell（shell=False）",
    all(c["kwargs"].get("shell") is False for c in calls), calls)
chk("A7 stop：占用者查询共 2 次（结束前 1 次 + 等端口释放 1 次）",
    lstate["calls"] == 2, lstate["calls"])

# --- 场景 B：可执行文件在目录外 → ours 假，stop 一律不动 ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, OUT_PATH)], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("B1 路径在引擎目录外 → running 真、ours 假",
    st["running"] is True and st["ours"] is False, st)
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, OUT_PATH)], guard)
reset_cache()
with p1, p2, p3:
    r = T.gsv_stop(CFG)
tk = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("B2 stop：目录外不动（无 taskkill、返回原说明、不谎报 stopped）",
    not tk and r.get("ok") is True and r.get("stopped") is False
    and NOTE in str(r.get("note")), (r, tk))
chk("B3 stop：只查 1 次占用者（没进等待循环）", lstate["calls"] == 1, lstate["calls"])

# --- 场景 C：端口没人听 → running 假，不查占用者，stop 无事可做 ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(False, [(0, "")], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("C1 端口没人听 → running 假、ours 假",
    st["running"] is False and st["ours"] is False, st)
chk("C1b running 为假 → 不查占用者（零 listener 调用），缓存被清",
    lstate["calls"] == 0 and 9880 not in T._GSV_OURS_CACHE,
    (lstate["calls"], dict(T._GSV_OURS_CACHE)))
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(False, [(0, "")], guard)
reset_cache()
with p1, p2, p3:
    r = T.gsv_stop(CFG)
tk = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("C2 stop：无监听则不动（无 taskkill、返回原说明）",
    not tk and r.get("stopped") is False and NOTE in str(r.get("note")),
    (r, tk))

# --- 场景 D：取路径失败（空路径）→ ours 假，stop 不动 ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, "")], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("D1 取路径失败 → running 真、ours 假（拿不准就当不是自己的）",
    st["running"] is True and st["ours"] is False, st)
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, "")], guard)
reset_cache()
with p1, p2, p3:
    r = T.gsv_stop(CFG)
tk = [c for c in calls if c["args"] and c["args"][0] == "taskkill"]
chk("D2 取路径失败 stop 不动", not tk and r.get("stopped") is False
    and NOTE in str(r.get("note")), (r, tk))

# --- 场景 E：runtime 外但 gsv 目录内 → 仍不算 ours（范围=gsv 目录下的 runtime） ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, str(GSV / "python.exe"))], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("E1 gsv 目录内但不在 runtime 下 → ours 假", st["ours"] is False, st)

# --- 场景 F：路径比较大小写/斜杠不敏感（Windows 路径事实） ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, str(GSV / "runtime" / "PYTHON.EXE"))], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("F1 路径大小写不同仍判 ours 真", st["ours"] is True, st)
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH.replace("\\", "/"))], guard)
reset_cache()
with p1, p2, p3:
    st = T.gsv_status(CFG)
chk("F2 正斜杠路径仍判 ours 真", st["ours"] is True, st)

# --- 场景 G：端口被占 → gsv_start 不重复启动，开头判断零占用者查询 ---
def boom(*a, **k):
    raise AssertionError("端口被占还尝试启动引擎")


calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH)], guard)
reset_cache()
with p1, p2, p3, mock.patch.object(subprocess, "Popen", boom):
    r = T.gsv_start(CFG)
chk("G1 端口被占 → gsv_start 返回 already，不启动新引擎",
    r.get("ok") is True and r.get("already") is True
    and r.get("running") is True, r)
chk("G2 start 开头只要 running（零占用者查询、零 taskkill/powershell）",
    lstate["calls"] == 0 and calls == [],
    (lstate["calls"], calls))

# --- 场景 H：running 真 → 3 秒内连续两次状态查询只查一次占用者（缓存） ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH)], guard)
reset_cache()
with p1, p2, p3:
    st1 = T.gsv_status(CFG)
    st2 = T.gsv_status(CFG)
chk("H1 两次状态查询只触发一次占用者查询（3 秒缓存命中）",
    lstate["calls"] == 1, lstate["calls"])
chk("H2 两次 ours 都为真且与缓存一致",
    st1["ours"] is True and st2["ours"] is True, (st1["ours"], st2["ours"]))

# --- 场景 I：running 由真变假 → 缓存里的占用者清掉 ---
calls, guard = make_run_guard()
ostate, lstate, p1, p2, p3 = patched(True, [(4242, IN_PATH)], guard)
reset_cache()
with p1, p2, p3:
    st1 = T.gsv_status(CFG)
chk("I1 预置：running 真，缓存已写入", st1["ours"] is True
    and 9880 in T._GSV_OURS_CACHE, dict(T._GSV_OURS_CACHE))
ostate, lstate, p1, p2, p3 = patched(False, [(4242, IN_PATH)], guard)
with p1, p2, p3:
    st2 = T.gsv_status(CFG)
chk("I2 running 变假 → ours 假且缓存 pid 项已清",
    st2["running"] is False and st2["ours"] is False
    and 9880 not in T._GSV_OURS_CACHE,
    (st2, dict(T._GSV_OURS_CACHE)))

# --- 场景 J：保活检查不查占用者、不起任何子进程 ---
calls, guard = make_run_guard()
ostate, ostub = const_open(True)
lstate, lstub = stub_listener([(4242, IN_PATH)])
reset_cache()
T._GSV_KEEPALIVE.clear()
T._GSV_KEEPALIVE["last"] = time.time() - 400   # 超过 5 分钟保活窗口


def popen_boom(*a, **k):
    raise AssertionError("保活路径不应启动进程")


try:
    with mock.patch.object(T, "_gsv_port_open", ostub), \
         mock.patch.object(T, "_gsv_port_listener", lstub), \
         mock.patch.object(subprocess, "run", side_effect=guard), \
         mock.patch.object(subprocess, "Popen", popen_boom):
        T.gsv_keepalive_tick(CFG)   # CFG 无参考音频：走到状态判断后自然收尾
    chk("J1 保活：只要 running，零占用者查询", lstate["calls"] == 0,
        lstate["calls"])
    chk("J2 保活：全程零子进程调用（taskkill/powershell 都没起）",
        calls == [], calls)
finally:
    T._GSV_KEEPALIVE.clear()

# --- 清理：只删本脚本建的前缀临时目录 ---
for _i in range(3):          # Windows 上刚写完的文件可能被杀软短暂锁住
    try:
        shutil.rmtree(_TMP)
        break
    except OSError:
        time.sleep(1.0)
else:
    print("清理临时目录失败（残留 %s）" % _TMP)
print()
print("%d 项通过，%d 项失败" % (n_pass, len(fails)))
sys.exit(1 if fails else 0)
