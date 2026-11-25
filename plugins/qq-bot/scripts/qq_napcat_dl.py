#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_napcat_dl.py — NapCat Shell 在线下载器（独立模块，被 qq_admin_ui 调用）

安全模型：
  - 仅 https 公网主机（DNS 解析后拒绝环回/私网/保留/链路本地）
  - 下载文件名白名单校验，落点固定在 <DATA_DIR>/downloads/ 内
  - 大小上限 300MB；状态机供 UI 轮询（不落任何配置）
"""

import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import threading
import time
import urllib.parse

import qq_common as C
import qq_services as S

_STATE = {"running": False, "pct": 0, "status": "", "error": "", "started": 0.0}


def status():
    st = dict(_STATE)
    if st.get("running") and st.get("started"):
        st["elapsed"] = int(time.time() - st["started"])
    return st


def _asset_expect(asset):
    """从 GitHub API 的 asset 条目提取校验期望：digest 形如 "sha256:…" 时
    取其哈希值作为期望值——经镜像下载的内容也必须用这个官方来源的值校验，
    不信任镜像自己提供的任何哈希。asset 没给 digest 时 sha256 为空串，
    由调用方决定降级（大小下限+文件头）还是拒绝。"""
    a = asset or {}
    digest = str(a.get("digest") or "").strip()
    return {"sha256": digest[7:] if digest.lower().startswith("sha256:") else "",
            "size": int(a.get("size") or 0)}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _head_matches(path, magic):
    try:
        with open(str(path), "rb") as f:
            return f.read(len(magic)) == magic
    except OSError:
        return False


def verify_download(path, expect):
    """校验已下载文件的完整性——所有下载点落盘后统一调用的纯函数。

    expect 字段：
      sha256         期望哈希（官方来源，如 GitHub API asset 的 digest）。
                     空串 = 官方没给哈希：走 allow_unsigned 降级或直接拒绝，
                     绝不静默放行
      size           期望精确字节数（0=未提供；仅在 sha256 非空时强制）
      min_size       大小下限（sha256 为空时的降级校验之一）
      magic          允许的文件头元组，任一匹配即可，如 (b"MZ",)、(b"PK\\x03\\x04",)
      allow_unsigned sha256 为空时是否允许按 min_size+magic 降级放行
      label          提示里显示的文件名（缺省用 path 的文件名）
    返回 (ok, msg, detail)。detail["reason"] ∈ {"ok", "ok_unsigned",
    "size_mismatch", "hash_mismatch", "magic_mismatch",
    "unsigned_not_allowed", "read_error"}；detail["hash_checked"] 说明
    本次是否做了哈希校验（False 时 msg 会写明未做哈希校验）。"""
    e = expect or {}
    label = str(e.get("label") or pathlib.Path(str(path)).name)
    want = str(e.get("sha256") or "").strip().lower()
    try:
        got_size = pathlib.Path(str(path)).stat().st_size
        got_hash = _sha256_file(path)
    except OSError as ex:
        return False, "校验 %s 失败：读不了文件（%r）" % (label, ex), \
            {"reason": "read_error", "hash_checked": False}
    magic = e.get("magic") or ()
    base = {"size": got_size, "sha256": got_hash}
    det_size = dict(base, reason="size_mismatch", hash_checked=bool(want))
    det_hash = dict(base, reason="hash_mismatch", hash_checked=True)
    det_magic = dict(base, reason="magic_mismatch", hash_checked=bool(want))
    det_unsigned = dict(base, reason="unsigned_not_allowed",
                        hash_checked=False)
    if want:
        exp_size = int(e.get("size") or 0)
        if exp_size and got_size != exp_size:
            return False, ("%s 完整性校验未通过：大小不符（应为 %d 字节，"
                           "实得 %d 字节），已拒绝安装"
                           % (label, exp_size, got_size)), det_size
        if got_hash != want:
            return False, ("%s 完整性校验未通过：sha256 哈希不对"
                           "（期望 %s…，实得 %s…），已拒绝安装"
                           % (label, want[:16], got_hash[:16])), det_hash
        if magic and not any(_head_matches(path, m) for m in magic):
            return False, ("%s 完整性校验未通过：文件头不是预期的类型，"
                           "已拒绝安装" % label), det_magic
        return True, "%s 完整性校验通过（大小 + sha256）" % label, \
            dict(base, reason="ok", hash_checked=True)
    # 官方没给哈希：要么显式允许降级（大小下限 + 文件头，并写明未做哈希
    # 校验），要么拒绝——没有"哈希为空就跳过校验"的放行分支
    if not e.get("allow_unsigned"):
        return False, ("%s 没有官方哈希且不允许降级校验，无法证明内容完整，"
                       "已拒绝安装" % label), det_unsigned
    min_size = int(e.get("min_size") or 0)
    if min_size and got_size < min_size:
        return False, ("%s 完整性校验未通过：文件大小 %d 字节低于下限 %d 字节，"
                       "已拒绝安装" % (label, got_size, min_size)), det_size
    if magic and not any(_head_matches(path, m) for m in magic):
        return False, ("%s 完整性校验未通过：文件头不是预期的类型，已拒绝安装"
                       % label), det_magic
    return True, ("%s 通过大小下限与文件头校验——注意：该文件未做哈希校验"
                  "（官方未提供哈希）" % label), \
        dict(base, reason="ok_unsigned", hash_checked=False)


def github_latest_asset(timeout=15):
    """查 NapCat Shell 最新 release 资产详情（github api）。
    返回 (ver, zip_url, info)：成功时 info 是 _asset_expect 提取的校验期望
    （sha256 来自 asset 的 digest 字段——经镜像下载的内容也必须用它校验）；
    失败时 ver 为 None，info 是中文错误说明。"""
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
            return None, "", "GitHub API 返回 %s（无网络或被墙）" % r.status
        ver = str(data.get("tag_name") or "").strip().lstrip("v")
        asset = None
        for a in data.get("assets") or []:
            n = str(a.get("name") or "")
            if n == "NapCat.Shell.zip":
                asset = a
                break
        if asset is None:
            for a in data.get("assets") or []:
                if str(a.get("name") or "").lower().endswith(".zip")                         and "framework" not in str(a.get("name") or "").lower():
                    asset = a
                    break
        if ver and asset:
            return ver, str(asset.get("browser_download_url") or ""), \
                _asset_expect(asset)
        return None, "", "release 资产里没有 zip"
    except (OSError, ValueError) as e:
        return None, "", "GitHub API 访问失败（%r）" % e


def github_latest(timeout=15):
    """查 NapCat Shell 最新 release：返回 (version, zip_url) 或 (None, err)。"""
    ver, url, info = github_latest_asset(timeout=timeout)
    if not ver:
        return None, info if isinstance(info, str) else "查询失败"
    return ver, url


def _download_public(url, name, state, expect=None):
    """下载到 DATA_DIR/downloads/<name>（公网校验 + 进度 + write_bytes 落盘）。
    expect 非空时落盘后立即做完整性校验（verify_download）：不过关就删掉
    下载的文件、返回 False，state["error"] 写明哪项没过；降级放行时置
    state["unsigned"]=True（提示层据此写明未做哈希校验）。"""
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
        if expect is not None:
            ok_v, msg_v, det_v = verify_download(final, expect)
            if not ok_v:
                try:
                    final.unlink()
                except OSError:
                    pass
                state["error"] = msg_v
                return False
            if det_v.get("reason") == "ok_unsigned":
                state["unsigned"] = True
        state["_dest"] = str(final)
        return True
    except OSError as e:
        state["error"] = "网络错误：%r" % e
        return False


def start(cfg, force=False):
    """后台线程：下载最新 NapCat Shell → 走既有 install_napcat_zip 安装。

    已安装且未 force 时返回 need_confirm=True（由 UI 弹确认再带 force 重试），
    防止手滑重复安装覆盖了正在用的 NapCat。
    已有任务在跑时返回 ok+already（UI 直接进入进度轮询，不算错误——
    旧版返回 error 会让页面只弹一句"已有下载任务"却永远不显示进度）。"""
    if _STATE["running"]:
        return {"ok": True, "already": True}
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
        _STATE.update(running=True, pct=0, status="正在查询最新版本…", error="",
                      started=time.time())
        try:
            ver, url, info = github_latest_asset()
            if not ver:
                _STATE.update(running=False, error=info if isinstance(info, str) else str(url))
                return
            # 完整性校验期望：官方 API 给了 digest（sha256:…）就必须对上，
            # 经镜像下载的内容也用这个官方值；没给 digest 时降级为
            # min_size+zip 文件头，并在结果里写明「未做哈希校验」。
            # min_size=1MB 是防御性下限（挡住镜像返回的错误页/空文件）。
            exp = dict(info) if isinstance(info, dict) else {}
            exp.setdefault("sha256", "")
            exp.setdefault("size", 0)
            exp["magic"] = (b"PK\x03\x04",)
            exp["min_size"] = 1 << 20
            exp["allow_unsigned"] = True
            exp["label"] = "NapCat Shell v%s" % ver
            _STATE["status"] = "NapCat Shell v%s · 下载中…" % ver
            st = {"error": ""}
            ok = _download_public(url, "napcat-shell-%s.zip" % ver, st, exp)
            if not ok and str(url).startswith("https://github.com/"):
                # 镜像顺序：实测可用优先（ghproxy 已停服，放最后减少无效等待）
                for mirror in ("https://gh-proxy.com/",
                               "https://ghfast.top/",
                               "https://mirror.ghproxy.com/"):
                    _STATE["status"] = "直连失败，改走镜像（%s）…" % mirror.split("/")[2]
                    ok = _download_public(mirror + url,
                                          "napcat-shell-%s.zip" % ver, st, exp)
                    if ok:
                        break
            if not ok:
                _STATE.update(
                    running=False,
                    error="%s——在线下载不通时改用手动方式：浏览器打开 "
                          "github.com/NapNeko/NapCatQQ/releases 下载 "
                          "NapCat.Shell.zip，再用「本地 zip 安装」按钮装"
                          % st.get("error", "下载失败"))
                return
            _STATE["status"] = "下载完成，正在安装…" + (
                "（注意：该文件未做哈希校验——官方未提供哈希）"
                if st.get("unsigned") else "")
            ok2, msg = S.install_napcat_zip(cfg, st.get("_dest", ""))
            _STATE.update(
                running=False, pct=100,
                status=("安装成功（v%s）：%s" % (ver, msg)) if ok2 else "",
                error="" if ok2 else "安装失败：%s" % msg)
        except Exception as e:
            _STATE.update(running=False, error="下载任务异常：%r" % e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
