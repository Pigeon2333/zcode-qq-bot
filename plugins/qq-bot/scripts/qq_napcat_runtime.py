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
import threading
import time
import urllib.parse

import qq_common as C

# 受控版本对（QQ 构建 ↔ NapCat Shell）——与官方 OneKey 4.18.42 同源认证
RUNTIME_QQ_VERSION = "9.9.33-52230"
RUNTIME_QQ_DATE = "260813"
RUNTIME_QQ_HASH = "497e2f1f"
RUNTIME_NAPCAT_MIN = "4.18.42"

_QQ_EXE_NAME = "QQ_9.9.33_260813_x64_01.exe"
_QQ_SOURCES = (
    "https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/497e2f1f/QQ_9.9.33_260813_x64_01.exe",
    "https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260813-1d08f1d4/QQ_9.9.33_260813_x64_01.exe",
    "https://gh-proxy.com/https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260813-1d08f1d4/QQ_9.9.33_260813_x64_01.exe",
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
                st = {"error": ""}
                if not _download_with_fallback(_QQ_SOURCES, str(qq_exe), st,
                                               "下载 QQ %s 安装包" % RUNTIME_QQ_VERSION):
                    _STATE.update(running=False, error=st.get("error", ""))
                    return
            _STATE.update(stage="静默安装便携 QQ（约 1-2 分钟）…", pct=40)
            # NSIS 静默安装到运行时目录（不碰注册表开始菜单以外的东西；
            # /D 必须无引号最后参数）
            rt.mkdir(parents=True, exist_ok=True)
            cmd = [str(qq_exe), "/S", "/D=" + str(rt)]
            try:
                subprocess.run(cmd, timeout=600,
                               capture_output=True)
            except subprocess.TimeoutExpired:
                _STATE.update(running=False,
                              error="QQ 静默安装超时（10 分钟）")
                return
            if not (rt / "QQ.exe").is_file():
                _STATE.update(running=False,
                              error="QQ 安装后未找到 QQ.exe（安装器行为变化？）")
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
