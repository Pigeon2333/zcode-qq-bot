# -*- coding: utf-8 -*-
"""Z-1 离线演练：安装脚本第 3 步（Get-Process 确认旧进程退出）真机跑一遍。

安全：
- 全部文件在系统临时目录（tempfile.mkdtemp）里；QQBOT_DATA_DIR 在 import 前
  重定向，绝不读写 ~/.zcode-qq-bot，不碰 D:\\ZCode\\qq-bot-local。
- 假目标进程 = 把 C:\\Windows\\System32\\ping.exe 复制成 ZxUpdDrill_1126.exe，
  只从临时目录启动。**不叫 ZCodeQQBot.exe**：安装脚本按映像名结束进程，万一
  真应用此刻正在运行，同名进程会被误杀——「绝不结束任何运行中的 ZCodeQQBot」
  优先于派单里「复制成 ZCodeQQBot.exe」的字面示例；proc_name 参数本来就
  支持任意名字，生成的脚本逻辑与真实安装完全一致。
- 演练前先核对系统里没有同名进程；演练结束的清理只按 PID，不按名字。
- 绝不结束、绝不 attach 任何真实 QQ / ZCodeQQBot / 系统进程。

覆盖：
  B 片段级验证：确认片段的三种退出码——进程存在 → 1；不存在 → 0；
     PowerShell 报错 / 起不来 → 非 0。「结束不掉」的路径在本机模拟不出来
     （需要杀不掉的进程，要管理员或内核保护，不造），这一条是片段级验证，
     不是整脚本演练。
  A 整脚本演练（正常路径）：假进程在跑 → 生成的 apply-update.bat 把它结束、
     完成拷贝、退出码 0；更新包/锁/暂存清理干净；.prev 是执行前的程序目录。
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

DRILL_NAME = "ZxUpdDrill_1126.exe"      # 假目标进程（独占假名，绝不撞真进程）
PROC_BASE = "ZxUpdDrill_1126"           # 不带 .exe 的映像名（脚本里 PROCBASE 的值）
CHECK_SNIPPET = ("if (Get-Process -Name '%s' -ErrorAction SilentlyContinue)"
                 " { exit 1 } else { exit 0 }" % PROC_BASE)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="z1_drill_"))
os.environ["QQBOT_DATA_DIR"] = str(TMP / "data")   # 必须在 import qq 模块之前

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "qq-bot" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_update as U  # noqa: E402
import qq_common as C  # noqa: E402

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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


def wf(p, content):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes)
                  else content.encode("utf-8"))


def run_ps(script, timeout=30):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=CREATE_NO_WINDOW)


def name_alive():
    """系统里有没有叫 DRILL_NAME 的进程（只读查询）。"""
    try:
        r = run_ps("(Get-Process -Name '%s' -ErrorAction SilentlyContinue).Id"
                   % PROC_BASE)
        return (r.stdout or "").strip()
    except Exception:
        return "?"


def pid_alive(pid):
    """按 PID 查进程是否还在（只读查询）。返回映像名或空串。"""
    try:
        r = run_ps("(Get-Process -Id %d -ErrorAction SilentlyContinue).Name"
                   % int(pid))
        return (r.stdout or "").strip()
    except Exception:
        return "?"


def launch_fake(seconds):
    """从临时目录启动假目标进程（ping 改名），返回 Popen。"""
    return subprocess.Popen([str(TMP / "fake" / DRILL_NAME),
                             "-n", str(seconds), "127.0.0.1"],
                            creationflags=CREATE_NO_WINDOW)


def kill_by_pid(pid):
    """清理只按 PID，不按名字（派单安全条款）。"""
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def snap(d):
    out = {}
    for p in sorted(pathlib.Path(d).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(d)).replace("\\", "/")] = p.read_bytes()
    return out


pre_launch_scan = name_alive()
chk("Z0 系统里没有同名进程（假名独占，按名结束不会伤及任何真实进程）",
    pre_launch_scan == "", pre_launch_scan)

fake_exe = TMP / "fake" / DRILL_NAME
try:
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(r"C:\Windows\System32\ping.exe", fake_exe)
except OSError as e:
    print("FAIL 无法从 System32 复制 ping.exe（演练无法进行）：%r" % (e,))
    sys.exit(1)

# ---------- B 片段级验证：确认片段的三种退出码 ----------
pB = launch_fake(15).pid
time.sleep(1.0)
aliveB = pid_alive(pB)
rc_exist = run_ps(CHECK_SNIPPET).returncode
chk("B1 片段：进程存在 → 退出码 1（=还在运行，脚本会继续等）",
    rc_exist == 1 and PROC_BASE in aliveB, (rc_exist, aliveB))
kill_by_pid(pB)
time.sleep(0.6)
goneB = pid_alive(pB)
rc_gone = run_ps(CHECK_SNIPPET).returncode
chk("B2 片段：进程不存在 → 退出码 0（=已退出，唯一放行码）",
    rc_gone == 0 and PROC_BASE not in goneB, (rc_gone, goneB))
rc_parse = run_ps("if (Get-Process").returncode      # 片段损坏/解析失败
chk("B3a 片段：PowerShell 报错（解析失败）→ 非 0（按还在运行处理）",
    rc_parse != 0, rc_parse)
rc_nops = subprocess.run(["cmd", "/c", "powershll-not-exist-9Q -Command echo hi"],
                         capture_output=True,
                         creationflags=CREATE_NO_WINDOW).returncode
chk("B3b 片段：PowerShell 起不来 → 非 0（按还在运行处理）", rc_nops != 0, rc_nops)

# ---------- A 整脚本演练：正常路径（假进程被结束、拷贝完成、退出码 0） ----------
udir = C.DATA_ROOT / "updates"
udir.mkdir(parents=True, exist_ok=True)
shutil.rmtree(udir / "stage", ignore_errors=True)
shutil.rmtree(udir / "apply.lock", ignore_errors=True)
staged = udir / "stage" / "ZCodeQQBot"
zipf = udir / "update-z1drill.zip"
app = TMP / "app"
wf(staged / "a.txt", "new-a")
wf(staged / "sub" / "b.txt", "new-b")
wf(app / "a.txt", "old-a")
wf(zipf, "zip-bytes-z1drill")
pA = launch_fake(60)
pidA = pA.pid
time.sleep(1.0)
chk("A0 假目标进程已启动并存活（PID %d）" % pidA,
    PROC_BASE in pid_alive(pidA), pid_alive(pidA))
want_staged = snap(staged)     # 演练成功后暂存目录会被脚本删掉，先存快照
try:
    bat = U.write_installer(staged, zipf, app_dir=app, proc_name=DRILL_NAME)
    text = bat.read_text(encoding="utf-8-sig")
    pb = bat.with_name("apply-update.drill.bat")
    pb.write_text(text.replace('start "" ', 'echo drill-skip-start '),
                  encoding="utf-8-sig")
    t0 = time.time()
    r = subprocess.run(["cmd", "/c", str(pb)], cwd=str(udir),
                       capture_output=True, timeout=300)
    dur = time.time() - t0
    chk("A1 演练退出码 0（%.1f 秒）" % dur,
        r.returncode == 0, (r.returncode, r.stdout[-200:], r.stderr[-200:]))
    time.sleep(0.6)
    chk("A2 假目标进程已被脚本结束（按名查询无残留，PID 复用也不误判）",
        name_alive() == "", name_alive())
    pollA = pA.poll()
    chk("A3 Popen.poll() 确认进程退出", pollA is not None, pollA)
    want = want_staged
    chk("A4 程序目录 == 暂存目录（拷贝完成）", snap(app) == want)
    chk("A5 .prev == 执行前的程序目录", snap(TMP / "app.prev") ==
        {"a.txt": b"old-a"})
    chk("A6 更新包已删、锁已释放、暂存目录已清理",
        not zipf.exists() and not (udir / "apply.lock").exists()
        and not staged.exists())
finally:
    if pA.poll() is None:
        kill_by_pid(pidA)
    time.sleep(0.5)
    leftover = pid_alive(pidA)
    chk("A7 收尾：演练进程无残留（按 PID 核对）", PROC_BASE not in leftover,
        leftover)
    for _i in range(3):
        try:
            shutil.rmtree(TMP)
            break
        except OSError:
            time.sleep(1.0)
    print("清理临时目录 %s：存在=%s" % (TMP, TMP.exists()))

print()
print("%d 项通过，%d 项失败" % (n_pass, len(fails)))
sys.exit(1 if fails else 0)
