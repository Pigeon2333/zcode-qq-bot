#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_napcat_dl.py — NapCat Shell 在线下载器（独立模块，被 qq_admin_ui 调用）

安全模型：
  - 仅 https 公网主机（DNS 解析后拒绝环回/私网/保留/链路本地）
  - 下载文件名白名单校验，落点固定在 <DATA_DIR>/downloads/ 内
  - 大小上限 300MB；状态机供 UI 轮询（不落任何配置）
"""

import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import threading
import urllib.parse

import qq_common as C
import qq_services as S

_STATE = {"running": False, "pct": 0, "status": "", "error": ""}


def status():
    return dict(_STATE)


def github_latest(timeout=15):
    """查 NapCat Shell 最新 release：返回 (version, zip_url) 或 (None, err)。"""
    try:
        conn = http.client.HTTPSConnection(
            "api.github.com", 443, timeout=timeout,
            context=ssl.create_default_context())
        conn.request("GET", "/repos/NapNeko/NapCatQQ/releases/latest",
                     headers={"User-Agent": "zcode-qq-bot",
                              "Accept": "application/vnd.github+json"})
        r = conn.getresponse()
        data = json.loads(r.read().decode("utf-8", "replace"))
        conn.close()
        if r.status != 200:
            return None, "GitHub API 返回 %s（无网络或被墙）" % r.status
        ver = str(data.get("tag_name") or "").strip().lstrip("v")
        url = ""
        for a in data.get("assets") or []:
            n = str(a.get("name") or "")
            if n == "NapCat.Shell.zip":
                url = a.get("browser_download_url") or ""
                break
        if not url:
            for a in data.get("assets") or []:
                if str(a.get("name") or "").lower().endswith(".zip")                         and "framework" not in str(a.get("name") or "").lower():
                    url = a.get("browser_download_url") or ""
                    break
        if ver and url:
            return ver, url
        return None, "release 资产里没有 zip"
    except (OSError, ValueError) as e:
        return None, "GitHub API 访问失败（%r）" % e


def _download_public(url, name, state):
    """下载到 DATA_DIR/downloads/<name>（公网校验 + 进度 + write_bytes 落盘）。"""
    u = urllib.parse.urlsplit(str(url))
    if u.scheme != "https" or not u.hostname:
        state["error"] = "下载地址非法（仅 https）"
        return False
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,80}$", str(name)) \
            or ".." in str(name):
        state["error"] = "文件名非法"
        return False
    host = u.hostname
    try:
        a = ipaddress.ip_address(socket.gethostbyname(host))
        if a.is_private or a.is_loopback or a.is_reserved or a.is_link_local:
            state["error"] = "下载主机被拒绝（内网地址）"
            return False
    except (OSError, ValueError):
        state["error"] = "下载主机无法解析"
        return False
    port = u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    try:
        conn = http.client.HTTPSConnection(
            host, port, timeout=60, context=ssl.create_default_context())
        conn.request("GET", path, headers={"User-Agent": "zcode-qq-bot"})
        r = conn.getresponse()
        if r.status != 200:
            conn.close()
            state["error"] = "HTTP %s" % r.status
            return False
        total = int(r.getheader("Content-Length") or 0)
        if total > 300 * 1024 * 1024:
            conn.close()
            state["error"] = "文件过大"
            return False
        chunks = []
        got = 0
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
        ddir = pathlib.Path(C.DATA_DIR) / "downloads"
        ddir.mkdir(parents=True, exist_ok=True)
        final = ddir / str(name)      # name 已白名单 → 必在 ddir 内
        final.write_bytes(b"".join(chunks))
        state["_dest"] = str(final)
        return True
    except OSError as e:
        state["error"] = "网络错误：%r" % e
        return False


def start(cfg, force=False):
    """后台线程：下载最新 NapCat Shell → 走既有 install_napcat_zip 安装。

    已安装且未 force 时返回 need_confirm=True（由 UI 弹确认再带 force 重试），
    防止手滑重复安装覆盖了正在用的 NapCat。"""
    if _STATE["running"]:
        return {"ok": False, "error": "已有下载任务进行中"}
    import qq_services as _S
    installed = False
    try:
        qqnt = C.qqnt_dir_resolved(cfg)
        installed = bool(qqnt) and os.path.isfile(
            os.path.join(qqnt, "launcher-user.bat"))
    except Exception:
        installed = False
    if installed and not force:
        return {"ok": False, "need_confirm": True,
                "error": ("检测到本机已安装 NapCat（QQ 目录里已有启动器）。"
                          "重复安装会先自动备份现有文件再覆盖，"
                          "且正在运行的 QQ 可能需要重启——确定要重装吗？")}

    def _run():
        _STATE.update(running=True, pct=0, status="正在查询最新版本…", error="")
        try:
            ver, url = github_latest()
            if not ver:
                _STATE.update(running=False, error=url)
                return
            _STATE["status"] = "NapCat Shell v%s · 下载中…" % ver
            st = {"error": ""}
            ok = _download_public(url, "napcat-shell-%s.zip" % ver, st)
            if not ok and str(url).startswith("https://github.com/"):
                for mirror in ("https://mirror.ghproxy.com/",
                               "https://gh-proxy.com/",
                               "https://ghfast.top/"):
                    _STATE["status"] = "直连失败，改走镜像（%s）…" % mirror.split("/")[2]
                    ok = _download_public(mirror + url,
                                          "napcat-shell-%s.zip" % ver, st)
                    if ok:
                        break
            if not ok:
                _STATE.update(
                    running=False,
                    error="%s——请改用本地 zip 安装（github.com/NapNeko/"
                          "NapCat-Shell 手动下载）" % st.get("error", "下载失败"))
                return
            _STATE["status"] = "下载完成，正在安装…"
            ok2, msg = S.install_napcat_zip(cfg, st.get("_dest", ""))
            _STATE.update(
                running=False, pct=100,
                status=("安装成功（v%s）：%s" % (ver, msg)) if ok2 else "",
                error="" if ok2 else "安装失败：%s" % msg)
        except Exception as e:
            _STATE.update(running=False, error="下载任务异常：%r" % e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
