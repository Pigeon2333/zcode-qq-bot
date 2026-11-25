# -*- coding: utf-8 -*-
"""冒烟：应用内更新器加固（qq_update.py 生成的 apply-update.bat：锁/备份/核对/回滚）。

规矩：
- 全部假目录搭在系统临时目录（tempfile）里；QQBOT_DATA_DIR 在 import 前重定向，
  绝不读写 ~/.zcode-qq-bot（生产数据目录，updates/ 里是真实用户的更新包），
  也不碰 D:\\ZCode\\qq-bot-local；不做 git 提交。
- 测试进程名一律用不存在的 NoSuchProc_9Q.exe，绝不碰真实 ZCodeQQBot.exe 进程。
- 实测：start 指向不存在的 exe 多数快速失败，但偶发挂起约 25 秒（疑似桌面弹窗，
  stderr 还会出 libpng 警告）。所以真跑场景把生成 bat 的 `start ""` 行替换成
  echo 占位后执行；start 行的位置与条数另有文本断言覆盖（S1）。
- 场景与主会话裁决口径一致（选 (a)，不改设计命令）：
  * 原表第 3/4 行（暂存缺文件 / 大小不同）→ 一条正向用例：/MIR 镜像语义照常
    安装成功（S3，用例名已写明）。
  * 表中要求的「拷贝没写上 → 退出码 5」用真实占用构造：测试进程持句柄挡住
    robocopy 删除暂存里没有的文件（CPython open 不给 FILE_SHARE_DELETE，实测
    能挡；只读属性挡不住覆盖——robocopy 会直接覆盖只读文件，实测）。核对发现
    程序目录与暂存不一致 → 回滚 → exit 5（S4）。
  * 能真跑的都真跑：S2 成功 / S3 镜像语义 / S4 回滚 / S5 备份挡住 / S6 锁三态。
    exit 6（回滚也不完整）真跑不可行：挡写必然也挡住回滚的恢复写（字节锁会让
    拷贝把文件清零且回滚写不进；挡读则备份先失败），只做文本断言（S1f）。
"""
import io
import os
import sys
import stat
import shutil
import tempfile
import subprocess
import pathlib

TMP = pathlib.Path(tempfile.mkdtemp(prefix="smoke_update_"))
os.environ["QQBOT_DATA_DIR"] = str(TMP / "data")   # 必须在 import qq 模块之前

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_update as U  # noqa: E402
import qq_common as C  # noqa: E402

PROC = "NoSuchProc_9Q.exe"
COPY_SW = "/MIR /R:2 /W:2"
LIST_SW = "/MIR /L /NJH /NJS /NP /NFL /NDL /NS /NC"
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
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def snap(d):
    out = {}
    for p in sorted(pathlib.Path(d).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(d)).replace("\\", "/")] = p.read_bytes()
    return out


def world(tag, staged_files, app_extra=()):
    """搭一套假环境：app（旧程序）、staged/ZCodeQQBot（新程序）、zip、updates 目录。"""
    udir = C.DATA_ROOT / "updates"
    udir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(udir / "stage", ignore_errors=True)
    shutil.rmtree(udir / "apply.lock", ignore_errors=True)
    w = {"udir": udir,
         "app": TMP / tag / "app",
         "staged": udir / "stage" / "ZCodeQQBot",
         "zip": udir / ("update-%s.zip" % tag),
         "lock": udir / "apply.lock",
         "prev": TMP / tag / "app.prev"}
    for rel, content in staged_files:
        wf(w["staged"] / rel, content)
    wf(w["app"] / "a.txt", "app-a-" + tag)
    wf(w["app"] / "sub" / "b.txt", "app-b-" + tag)
    for rel, content in app_extra:
        wf(w["app"] / rel, content)
    wf(w["zip"], "zip-bytes-" + tag)
    w["bat"] = U.write_installer(w["staged"], w["zip"], app_dir=w["app"],
                                 proc_name=PROC)
    return w


def run_bat(w):
    """真跑生成的 bat：复制一份、把 start 行换成 echo 占位（理由见文件头）。"""
    text = w["bat"].read_text(encoding="utf-8-sig")
    if 'start "" ' not in text:
        return None
    pb = w["bat"].with_name("apply-update.smoke.bat")
    pb.write_text(text.replace('start "" ', 'echo smoke-skip-start '),
                  encoding="utf-8-sig")
    try:
        return subprocess.run(["cmd", "/c", str(pb)], cwd=str(w["udir"]),
                              capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None


def robocopy_rc(src, dst, sw):
    cmd = 'robocopy "%s" "%s" %s' % (src, dst, sw)
    return subprocess.run("cmd /c " + cmd, capture_output=True,
                          timeout=60).returncode


try:
    # ---------- S8 robocopy /L 语义实测（步骤 2/5/6 的前提） ----------
    x, y = TMP / "rc8" / "x", TMP / "rc8" / "y"
    (x / "sub").mkdir(parents=True)
    wf(x / "a.txt", "same")
    wf(x / "sub" / "b.txt", "same2")
    robocopy_rc(x, y, COPY_SW)   # robocopy 真拷一遍（带源文件时间戳）
    r0 = robocopy_rc(x, y, LIST_SW)
    chk("S8a /L 两目录完全一致(含时间戳) 返回 0", r0 == 0, r0)
    wf(x / "extra.txt", "e")
    r1 = robocopy_rc(x, y, LIST_SW)
    chk("S8b /L 源多一个文件 返回非 0 且 <8（待拷贝，不是失败）",
        0 < r1 < 8, r1)
    (x / "extra.txt").unlink()
    wf(y / "gone.txt", "g")
    r2 = robocopy_rc(x, y, LIST_SW)
    chk("S8c /L 目标多一个文件 返回非 0 且 <8（待删除，不是失败）",
        0 < r2 < 8, r2)
    (y / "gone.txt").unlink()
    yro = y / "ro.txt"
    wf(yro, "R")
    os.chmod(yro, stat.S_IREAD)
    r3 = robocopy_rc(x, y, COPY_SW)
    chk("S8d robocopy /MIR 连只读文件都能删（只读挡不住安装，也不能挡回滚）",
        (not yro.exists()) and r3 < 8, (yro.exists(), r3))

    # ---------- S1 生成的脚本：纯 ASCII、1→8 顺序、三条路径释放锁 ----------
    udir = C.DATA_ROOT / "updates"
    udir.mkdir(parents=True, exist_ok=True)
    app1 = TMP / "s1app"
    staged1 = udir / "stage" / "ZCodeQQBot"
    zip1 = udir / "s1.zip"
    bat = U.write_installer(staged1, zip1, app1)   # 位置参数调用保持兼容
    raw = bat.read_bytes()
    text = bat.read_text(encoding="utf-8-sig")
    chk("S1a 脚本纯 ASCII 且无 BOM（首字节是 @echo off）",
        all(ord(ch) < 128 for ch in text) and raw[:9] == b"@echo off")
    idx = text.index
    ordered = ('mkdir "%LOCK%"',
               'robocopy "%APP%" "%PREV%" /MIR /R:2',
               'robocopy "%APP%" "%PREV%" /MIR /L',
               'taskkill /IM "%PROC%"',
               'robocopy "%STAGED%" "%APP%" /MIR /R:2',
               'robocopy "%STAGED%" "%APP%" /MIR /L',
               'robocopy "%PREV%" "%APP%" /MIR /R:2',
               'del /q "%ZIP%"')
    pos = [idx(m) for m in ordered]
    chk("S1b 步骤按 1 抢锁→2 备份→3 等退出→4 拷贝→5 核对→6 回滚→7 清理 顺序",
        pos == sorted(pos), list(zip(pos, sorted(pos))))
    chk("S1c 备份拷贝后才做备份核对、拷贝后才做拷贝核对（/L 核对紧随其后）",
        pos[1] < pos[2] < pos[3] < pos[4] < pos[5])
    rel = 'rmdir /s /q "%LOCK%"'
    chk("S1d 三条结束路径（成功/回滚/中止）都释放锁（共 5 处释放 + 1 处清旧锁）",
        text.count(rel) == 6, text.count(rel))
    for code in ("exit /b 3", "exit /b 4", "exit /b 5", "exit /b 6",
                 "exit /b 0"):
        i = idx(code)
        if rel not in text[max(0, i - 200):i]:
            chk("S1e %s 之前有释放锁" % code, False, text[max(0, i - 200):i])
            break
    else:
        chk("S1e 每条 exit 路径之前都有释放锁", True)
    chk("S1f 回滚也不完整：释放锁、打印 .prev 路径、不启动任何程序、exit 6",
        ":rollback_fail" in text and "echo %PREV%" in text
        and idx("echo %PREV%") < idx("exit /b 6")
        and 'start "" ' not in text[idx(":rollback_fail"):idx("exit /b 6")])
    chk("S1g 锁忙只打印提示并 exit 2，不释放别人的锁",
        "another update is running" in text
        and idx("another update is running") < idx("exit /b 2")
        and rel not in text[idx(":lock_busy"):idx("exit /b 2")])
    chk("S1h 更新包只在成功路径删除（del ZIP 恰一次且在 install_ok 内）",
        text.count('del /q "%ZIP%"') == 1
        and idx(":install_ok") < idx('del /q "%ZIP%"') < idx("exit /b 0"))
    chk("S1i 锁目录在 updates 下，owner.pid/owner.txt 写入锁内",
        'set "LOCK=' in text and "\\apply.lock" in text
        and 'owner.pid' in text and 'owner.txt' in text)
    chk("S1j 旧进程最多等 20 秒（ping 计时 + GEQ 20）",
        "ping -n 2 127.0.0.1 >nul" in text and "GEQ 20" in text)
    chk("S1k start 行恰两处（回滚后启旧版 + 成功后启新版）",
        text.count('start "" ') == 2, text.count('start "" '))
    chk("S1l 默认进程名保持 ZCodeQQBot.exe（写入 PROC，taskkill 引用 %PROC%）",
        'set "PROC=ZCodeQQBot.exe"' in text and 'taskkill /IM "%PROC%"' in text)
    bat2 = U.write_installer(staged1, zip1, app_dir=app1, proc_name=PROC)
    t2 = bat2.read_text(encoding="utf-8-sig")
    chk("S1m proc_name 参数生效", 'set "PROC=%s"' % PROC in t2)
    try:
        U.write_installer(staged1, zip1, proc_name='a"b')
        chk("S1n 非法 proc_name 被拒", False)
    except ValueError:
        chk("S1n 非法 proc_name 被拒", True)
    try:
        U.make_installer(staged1, zip1)
        chk("S1o 源码模式 make_installer 仍拒绝执行", False)
    except RuntimeError:
        chk("S1o 源码模式 make_installer 仍拒绝执行", True)
    chk("S1p status() 返回结构不变",
        set(U.status().keys()) == {"phase", "pct", "msg", "version", "zip",
                                   "error", "ts"})

    # ---------- S2 暂存完整：真跑，退出码 0 ----------
    w = world("s2", [("a.txt", "new-a"), ("sub/b.txt", "new-b"),
                     ("added.txt", "brand-new")],
              app_extra=[("extra_old.txt", "should-disappear")])
    pre = snap(w["app"])
    want = snap(w["staged"])
    r = run_bat(w)
    chk("S2-真跑退出码 0", r is not None and r.returncode == 0,
        "" if r is None else r.returncode)
    chk("S2 程序目录 == 暂存目录（多出的 extra_old.txt 被清、added.txt 就位）",
        snap(w["app"]) == want)
    chk("S2 .prev == 执行前的程序目录（含 extra_old.txt）",
        snap(w["prev"]) == pre)
    chk("S2 更新包被删、锁已释放、暂存目录已清理", not w["zip"].exists()
        and not w["lock"].exists() and not w["staged"].exists())

    # ---------- S3 原表第3/4行正向用例：镜像语义，暂存缺文件/大小不同照常安装 ----------
    w = world("s3", [("a.txt", "n"),          # a.txt 大小不同
                     ("added.txt", "brand-new")])   # 没有 sub/b.txt
    pre = snap(w["app"])
    want = snap(w["staged"])
    r = run_bat(w)
    chk("S3-真跑退出码 0（镜像语义：暂存缺文件/大小不同不算失败）",
        r is not None and r.returncode == 0, "" if r is None else r.returncode)
    chk("S3 程序目录 == 暂存目录（sub/b.txt 被镜像删除、a.txt 按暂存覆盖）",
        snap(w["app"]) == want)
    chk("S3 .prev == 执行前、更新包被删、锁已释放",
        snap(w["prev"]) == pre and not w["zip"].exists()
        and not w["lock"].exists())

    # ---------- S4 拷贝没写上（暂存缺的文件被占用删不掉）：回滚成功，退出码 5 ----------
    w = world("s4", [("a.txt", "new-a"), ("added.txt", "x")])   # 缺 sub/b.txt
    pre = snap(w["app"])
    # io.FileIO 与内置 open(path, "rb") 拿到的是同一种句柄（不给 FILE_SHARE_DELETE）
    held = io.FileIO(str(w["app"] / "sub" / "b.txt"), "rb")   # CPython 的文件对象不给
    # FILE_SHARE_DELETE：robocopy 的 /MIR 删不掉这个文件 → 核对发现不一致 → 回滚
    try:
        r = run_bat(w)
    finally:
        held.close()
    chk("S4-真跑退出码 5（拷贝没写上→核对发现→回滚成功）",
        r is not None and r.returncode == 5, "" if r is None else r.returncode)
    chk("S4 程序目录 == 执行前（sub/b.txt 原样、a.txt 回滚为旧版）",
        snap(w["app"]) == pre)
    chk("S4 更新包保留、锁已释放", w["zip"].exists()
        and not w["lock"].exists())

    # ---------- S5 备份目录无法建立（只读文件占住 .prev）：真跑，退出码 3 ----------
    w = world("s5", [("a.txt", "new-a"), ("sub/b.txt", "new-b")])
    pre = snap(w["app"])
    blocker = pathlib.Path(str(w["app"]) + ".prev")
    wf(blocker, "blocker")
    os.chmod(blocker, stat.S_IREAD)
    r = run_bat(w)
    chk("S5-真跑退出码 3（备份不完整）", r is not None and r.returncode == 3,
        "" if r is None else r.returncode)
    chk("S5 程序目录不变", snap(w["app"]) == pre)
    chk("S5 更新包保留、锁已释放", w["zip"].exists()
        and not w["lock"].exists())

    # ---------- S6 锁目录三态：真跑 ----------
    # S6a 锁在 + owner.pid 指向活进程（本测试进程）→ 直接退出码 2，什么都不动
    w = world("s6a", [("a.txt", "new-a"), ("sub/b.txt", "new-b")])
    pre = snap(w["app"])
    wf(w["lock"] / "owner.pid", str(os.getpid()))
    r = run_bat(w)
    chk("S6a-真跑退出码 2（another update is running）",
        r is not None and r.returncode == 2, "" if r is None else r.returncode)
    chk("S6a 程序目录和更新包都不变，锁原样保留", snap(w["app"]) == pre
        and w["zip"].exists() and (w["lock"] / "owner.pid").exists()
        and (w["lock"] / "owner.pid").read_bytes() == str(os.getpid()).encode())
    # S6b 锁在但 pid 是死进程（断电旧锁）→ 清锁重抢，更新正常完成
    w = world("s6b", [("a.txt", "new-b"), ("sub/b.txt", "new-b2")])
    pre = snap(w["app"])
    want = snap(w["staged"])
    wf(w["lock"] / "owner.pid", "999999")
    r = run_bat(w)
    chk("S6b-真跑退出码 0（旧锁被清、更新完成）",
        r is not None and r.returncode == 0, "" if r is None else r.returncode)
    chk("S6b 程序目录 == 暂存、更新包已删、锁已释放",
        snap(w["app"]) == want and not w["zip"].exists()
        and not w["lock"].exists())
    # S6c 锁在但没有 owner.pid（写 pid 前崩溃）→ 保守当作有更新在跑
    w = world("s6c", [("a.txt", "new-a"), ("sub/b.txt", "new-b")])
    pre = snap(w["app"])
    w["lock"].mkdir(parents=True, exist_ok=True)
    r = run_bat(w)
    chk("S6c-真跑退出码 2（无 pid 文件不放行）",
        r is not None and r.returncode == 2, "" if r is None else r.returncode)
    chk("S6c 程序目录和更新包都不变", snap(w["app"]) == pre
        and w["zip"].exists())

    # ---------- S7 Python 侧 run_update：锁存在时返回失败、不启动安装脚本 ----------
    udir = C.DATA_ROOT / "updates"
    udir.mkdir(parents=True, exist_ok=True)
    orig = (U.check, U.download, U.stage, U.make_installer)
    sentinel = []

    def fake_make(s, z, proc_name=U.PROC_NAME_DEFAULT):
        sentinel.append((str(s), str(z)))
        return "fake-bat"

    try:
        U.check = lambda cfg: {"ok": True, "newer": True}
        U.download = lambda info, cfg: udir / "fake.zip"
        U.stage = lambda zp: udir / "stage" / "ZCodeQQBot"
        U.make_installer = fake_make
        (udir / "apply.lock").mkdir(parents=True, exist_ok=True)
        rc = U.run_update(cfg={"update_repo": "x/y"})
        st = U.status()
        chk("S7a 锁存在 → run_update 返回 False", rc is False, rc)
        chk("S7b STATE phase=error", st.get("phase") == "error", st.get("phase"))
        chk("S7c msg 说明另一个更新正在进行",
            "另一个更新正在进行" in (st.get("msg") or ""), st.get("msg"))
        chk("S7d 没有启动安装脚本", sentinel == [])
        chk("S7e STATE 字段不变",
            set(st.keys()) == {"phase", "pct", "msg", "version", "zip",
                               "error", "ts"})
        shutil.rmtree(udir / "apply.lock")
        rc2 = U.run_update(cfg={"update_repo": "x/y"})
        chk("S7f 无锁 → 正常走到 make_installer 且返回 True",
            rc2 is True and len(sentinel) == 1, (rc2, len(sentinel)))
    finally:
        U.check, U.download, U.stage, U.make_installer = orig
finally:
    for p in TMP.rglob("*"):
        try:
            if p.is_file():
                os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print()
    print("清理临时目录 %s ：存在=%s" % (TMP, TMP.exists()))

print()
print("%d 项通过，%d 项失败" % (n_pass, len(fails)))
sys.exit(1 if fails else 0)
