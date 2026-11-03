# -*- coding: utf-8 -*-
"""qq_napcat_runtime.py — NapCat 独立运行时（分离布局，根治 QQ 自动升级破坏）。

方案（对齐 NapCat 官方 OneKey）：
  - 在数据目录下维护一份便携 QQ + NapCat Shell：~/.zcode-qq-bot/napcat-runtime/
  - QQ 安装包来自官方 CDN（qqdl.gtimg.cn，OneKey 同源）+ GitHub 镜像兜底；
    以 NSIS 静默模式 /S /D=<目录> 装进运行时目录——不碰用户的 QQ 安装
  - NapCat Shell 装进运行时目录（复用 install_napcat_zip，含 versions 双摆）
  - 锁定受控版本对（与 OneKey 认证组合一致），版本随应用发版更新
  - 用户自己的 QQ 升级/重装/卸载都与机器人无关

安全模型：仅 https 公网（qqdl.gtimg.cn / github.com / gh-proxy 镜像白名单），
下载文件名校验 + 600MB 上限，落点固定在运行时目录内。
"""

import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

import qq_common as C

# 受控版本对（QQ 构建 ↔ NapCat Shell）——9.9.32-51246 + NapCat 4.18.42
# 为实机验证可用组合（2026-11-03：QQ 9.9.32 + versions app 双摆 = WS 正常）。
# 注：OneKey 配对 9.9.33-52230，但该组合在本机复现上游 #2149/#2153
# 「注入成功核心不启动」，故不用。
RUNTIME_QQ_VERSION = "9.9.32-51246"
RUNTIME_QQ_DATE = "260716"
RUNTIME_QQ_HASH = ""
RUNTIME_NAPCAT_MIN = "4.18.42"

_QQ_EXE_NAME = "QQ_9.9.32_260716_x64_01.exe"
_QQ_SOURCES = (
    "https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260720-ffedf7c6/QQ_9.9.32_260716_x64_01.exe",
    "https://gh-proxy.com/https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260720-ffedf7c6/QQ_9.9.32_260716_x64_01.exe",
    "https://ghfast.top/https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260720-ffedf7c6/QQ_9.9.32_260716_x64_01.exe",
)
_SHELL_MIRROR_PREFIXES = ("", "https://gh-proxy.com/", "https://ghfast.top/")

_STATE = {"running": False, "pct": 0, "stage": "", "error": "", "started": 0.0}


def runtime_dir():
    return os.path.join(C.DATA_DIR, "napcat-runtime")


def is_installed():
    """运行时就绪 = 便携 QQ.exe + NapCat 启动器都在。"""
    d = pathlib.Path(runtime_dir())
    return (d / "QQ.exe").is_file() and (d / "launcher-user.bat").is_file()


def status():
    st = dict(_STATE)
    st["installed"] = is_installed()
    st["dir"] = runtime_dir()
    st["qq_version"] = RUNTIME_QQ_VERSION
    if st.get("running") and st.get("started"):
        st["elapsed"] = int(time.time() - st["started"])
    return st


def _public_https_ok(url):
    u = urllib.parse.urlsplit(str(url))
    if u.scheme != "https" or not u.hostname:
        return False
    try:
        a = ipaddress.ip_address(socket.gethostbyname(u.hostname))
        return not (a.is_private or a.is_loopback or a.is_reserved
                    or a.is_link_local)
    except (OSError, ValueError):
        return False


def _download(url, dest, state, timeout=90):
    if not _public_https_ok(url):
        state["error"] = "下载地址非法（仅 https 公网：%s）" % url[:60]
        return False
    # 落点必须解析在数据目录内 + 文件名白名单（防穿越）
    dp = pathlib.Path(dest)
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,80}$", dp.name):
        state["error"] = "文件名非法"
        return False
    try:
        inside = str(dp.resolve()).startswith(
            str(pathlib.Path(C.DATA_DIR).resolve()))
    except OSError:
        inside = False
    if not inside:
        state["error"] = "落点必须在数据目录内"
        return False
    u = urllib.parse.urlsplit(url)
    port = u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    try:
        conn = http.client.HTTPSConnection(
            u.hostname, port, timeout=timeout,
            context=ssl.create_default_context())
        conn.request("GET", path, headers={"User-Agent": "zcode-qq-bot"})
        r = conn.getresponse()
        if r.status != 200:
            conn.close()
            state["error"] = "HTTP %s（%s）" % (r.status, u.hostname)
            return False
        total = int(r.getheader("Content-Length") or 0)
        if total > 600 * 1024 * 1024:
            conn.close()
            state["error"] = "文件过大（>600MB）"
            return False
        chunks, got = [], 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if total:
                state["pct"] = min(99, int(got * 100 / total))
        conn.close()
        if total and got < total:
            state["error"] = "下载不完整"
            return False
        pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(dest).write_bytes(b"".join(chunks))
        return True
    except OSError as e:
        state["error"] = "网络错误：%r" % e
        return False


def _download_with_fallback(urls, dest, state, label):
    for i, u in enumerate(urls):
        state["stage"] = "%s（源 %d/%d）…" % (label, i + 1, len(urls))
        if _download(u, dest, state):
            return True
        err = state.get("error", "")
        if i + 1 < len(urls):
            state["error"] = ""  # 还有源，继续
    state["error"] = state.get("error") or err or "全部下载源失败"
    return False


def _shell_latest():
    """NapCat 最新版号与 Shell.zip 地址（github api，gh-proxy 兜底）。"""
    for host_prefix in ("", "https://gh-proxy.com/"):
        url = host_prefix + \
            "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
        if host_prefix and not _public_https_ok(url):
            continue
        try:
            u = urllib.parse.urlsplit(url)
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=15,
                context=ssl.create_default_context())
            conn.request("GET", u.path,
                         headers={"User-Agent": "zcode-qq-bot",
                                  "Accept": "application/vnd.github+json"})
            r = conn.getresponse()
            data = json.loads(r.read().decode("utf-8", "replace"))
            conn.close()
            ver = str(data.get("tag_name") or "").lstrip("v")
            for a in data.get("assets") or []:
                if str(a.get("name")) == "NapCat.Shell.zip":
                    return ver, a.get("browser_download_url") or ""
        except (OSError, ValueError):
            continue
    return None, ""


def _find_7zr():
    """定位 7zr.exe：冻结包 assets / 源码 assets / 数据目录 tools（依次）。"""
    cands = []
    try:
        cands.append(os.path.join(getattr(sys, "_MEIPASS", ""),
                                  "assets", "7zr.exe"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "assets", "7zr.exe"))
    cands.append(os.path.join(C.DATA_DIR, "tools", "7zr.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _extract_portable_qq(installer_path, rt_dir, state):
    """从 QQ 安装包（7z SFX）纯解压出便携 QQ → rt_dir。免 UAC。
    结构：exe = [7z 头(license 等小文件)] + 'P' + 标准 7z 载荷（完整 QQ）。
    返回 (ok, err)。"""
    z7 = _find_7zr()
    if not z7:
        return False, "未找到 7zr.exe（打包资产缺失）"
    try:
        data = open(str(installer_path), "rb").read()
    except OSError as e:
        return False, "读安装包失败：%r" % e
    magic = b"P\x37\x7a\xbc\xaf\x27\x1c"  # 'P' + 7z 签名
    k = data.rfind(magic)
    if k < 0:
        return False, "安装包结构变化（未找到载荷魔数）"
    pathlib.Path(rt_dir).mkdir(parents=True, exist_ok=True)
    # 工作目录放数据目录（载荷约 1.1GB，避免撑爆系统盘）
    import tempfile
    tmp_root = pathlib.Path(C.DATA_DIR) / "tools"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="qq-portable-", dir=str(tmp_root)))
    try:
        (tmp / "payload.7z").write_bytes(data[k + 1:])
        state["stage"] = "解压便携 QQ（约 1-3 分钟）…"
        r = subprocess.run([z7, "x", "-y", "-o" + str(tmp / "px"),
                            str(tmp / "payload.7z")],
                           capture_output=True, timeout=900)
        files_dir = tmp / "px" / "Files"
        # 7zr 对"尾部冗余数据"返回 2（警告级）但解压完整——以 QQ.exe 在场为准
        if not (files_dir / "QQ.exe").is_file():
            return False, "解压失败或布局变化（rc=%s %s）" % (
                r.returncode,
                (r.stderr or r.stdout).decode("utf-8", "replace")[-120:])
        # Files/ 即便携 QQ 根布局 → 移入运行时目录
        import shutil
        state["stage"] = "组装运行时目录…"
        for item in files_dir.iterdir():
            dst = pathlib.Path(rt_dir) / item.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    dst.unlink()
            shutil.move(str(item), str(dst))
        if not (pathlib.Path(rt_dir) / "QQ.exe").is_file():
            return False, "组装后未找到 QQ.exe"
        # 引导配置：payload 不带 versions/config.json（安装器运行时才写），
        # 补上让 QQ.exe 启动器与 NapCat 版本适配都指向唯一版本
        vcfg = pathlib.Path(rt_dir) / "versions" / "config.json"
        if not vcfg.is_file():
            vcfg.write_text(json.dumps(
                {"baseVersion": RUNTIME_QQ_VERSION,
                 "curVersion": RUNTIME_QQ_VERSION},
                ensure_ascii=False), encoding="utf-8")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "解压超时（15 分钟）"
    except OSError as e:
        return False, "解压异常：%r" % e
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _run():
    _STATE.update(running=True, pct=0, stage="准备下载…", error="",
                  started=time.time())
    try:
        import qq_services as S
        ddir = pathlib.Path(C.DATA_DIR) / "downloads"
        ddir.mkdir(parents=True, exist_ok=True)
        rt = pathlib.Path(runtime_dir())

        # ① 便携 QQ（已装则跳过——重装运行时用 reset 先清）
        if not (rt / "QQ.exe").is_file():
            qq_exe = ddir / _QQ_EXE_NAME
            if not qq_exe.is_file():
                # 进度直接写 _STATE（此前误传局部 st → UI 永远 0%）
                if not _download_with_fallback(_QQ_SOURCES, str(qq_exe),
                                               _STATE,
                                               "下载 QQ %s 安装包" % RUNTIME_QQ_VERSION):
                    _STATE.update(running=False, error=_STATE.get("error", ""))
                    return
            _STATE.update(stage="解压便携 QQ（无需管理员权限）…", pct=40)
            rt.mkdir(parents=True, exist_ok=True)
            ok_x, err_x = _extract_portable_qq(qq_exe, rt, _STATE)
            if not ok_x:
                _STATE.update(running=False, error=err_x)
                return

        # ② NapCat Shell（复用在线下载器与安装器）
        _STATE.update(stage="下载 NapCat Shell…", pct=65)
        import qq_napcat_dl as ND
        ver, url = ND.github_latest()
        if not ver:
            _STATE.update(running=False,
                          error="查询 NapCat 版本失败：%s" % url)
            return
        zpath = pathlib.Path(C.DATA_DIR) / "downloads" / \
            ("napcat-shell-%s.zip" % ver)
        if not zpath.is_file():
            st2 = {"error": ""}
            ok = ND._download_public(url, zpath.name, st2)
            if not ok and str(url).startswith("https://github.com/"):
                for m in _SHELL_MIRROR_PREFIXES[1:]:
                    ok = ND._download_public(m + url, zpath.name, st2)
                    if ok:
                        break
            if not ok:
                _STATE.update(
                    running=False,
                    error="%s——NapCat Shell 下载失败，可稍后重试"
                          % st2.get("error", ""))
                return
        _STATE.update(stage="安装 NapCat 进运行时…", pct=88)
        cfg_rt = dict(C.load_app_config())
        cfg_rt["qqnt_dir"] = str(rt)
        ok, msg = S.install_napcat_zip(cfg_rt, str(zpath))
        if not ok:
            _STATE.update(running=False,
                          error="NapCat 安装失败：%s" % msg[:160])
            return
        _STATE.update(running=False, pct=100,
                      stage="独立运行时就绪（QQ %s + NapCat %s）"
                      % (RUNTIME_QQ_VERSION, ver))
    except Exception as e:
        _STATE.update(running=False, error="运行时安装异常：%r" % e)


def install():
    """后台线程安装/修复独立运行时。已在装时返回 ok+already。"""
    if _STATE["running"]:
        return {"ok": True, "already": True}
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


def delete():
    """删除整个运行时目录（模式切回跟随用户 QQ 时清理用）。"""
    import shutil
    if _STATE["running"]:
        return {"ok": False, "error": "安装进行中，不能删除"}
    d = pathlib.Path(runtime_dir())
    if d.is_dir():
        # 防误删：确认目录里确实是这套便携布局
        if not (d / "QQ.exe").is_file() and not (d / "launcher-user.bat").is_file():
            return {"ok": False, "error": "目录布局不符，拒绝删除"}
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}
