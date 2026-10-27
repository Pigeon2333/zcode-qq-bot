#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_update.py — 从 GitHub Release 检查/下载/安装新版本（发布版自更新）

设计要点：
  - 只走 https，且只允许 GitHub 官方几个域名（防被改配置指向任意站点）
  - Release 元数据用 REST API；私有仓库可用 fine-grained token 读取
    （token 存 app_config.update_token，只留在本机数据目录）
  - 资产下载做大小校验 + 可选 sha256 校验；解压前逐个校验路径不越界
  - 安装 = 生成一个批处理：等旧进程退出 → 覆盖程序目录（不删除用户文件）
    → 重新启动 → 清理临时目录。**绝不触碰数据目录 ~/.zcode-qq-bot**
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
import http.client

import qq_common as C

API_HOST = "api.github.com"
# 资产下载会 302 到这些域名之一；只放行 GitHub 自己的下载域
ALLOW_HOSTS = {
    "api.github.com", "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "codeload.github.com",
}
MAX_ZIP = 300 * 1024 * 1024
STATE = {"phase": "idle", "pct": 0, "msg": "", "version": "", "zip": "",
         "error": "", "ts": 0.0}
_LOCK = threading.Lock()
_UA = "ZCodeQQBot-Updater/1.0"
_SAFE_VER = re.compile(r"[^0-9A-Za-z._-]")


def current_version():
    return str(getattr(C, "APP_VERSION", "0.0.0"))


def _ver_tuple(s):
    out = []
    for part in str(s or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def _https(host, path, token="", accept="application/json", timeout=20,
           sink=None, redirect_left=4):
    """GET 指定主机路径；返回 (status, headers, size|body)。
    sink 给出时把响应体写入该二进制句柄（须由调用方在数据目录内打开）。"""
    if host not in ALLOW_HOSTS:
        raise ValueError("非白名单域名：%s" % host)
    conn = http.client.HTTPSConnection(host, timeout=timeout,
                                       context=ssl.create_default_context())
    headers = {"User-Agent": _UA, "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location") or ""
            conn.close()
            if redirect_left <= 0 or not loc:
                raise RuntimeError("重定向过多")
            up = urllib.parse.urlsplit(loc)
            if up.scheme != "https":
                raise RuntimeError("拒绝非 https 跳转")
            return _https(up.hostname or "", up.path + ("?" + up.query if up.query else ""),
                          token=token, accept=accept, timeout=timeout,
                          sink=sink, redirect_left=redirect_left - 1)
        if sink is not None:
            got = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_ZIP:
                    raise RuntimeError("文件超过大小上限")
                sink.write(chunk)
            return resp.status, dict(resp.getheaders()), got
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _token(cfg):
    return str((cfg or {}).get("update_token") or "").strip()


def _repo(cfg):
    repo = str((cfg or {}).get("update_repo") or C.DEFAULT_UPDATE_REPO).strip()
    if repo.count("/") != 1 or any(c in repo for c in " \t"):
        return ""
    if any(c in repo for c in "\\?#"):
        return ""
    return repo


def check(cfg=None):
    """查询最新 Release。返回 dict：ok/current/latest/newer/notes/asset..."""
    cfg = cfg or C.load_app_config()
    repo = _repo(cfg)
    if not repo:
        return {"ok": False, "error": "update_repo 格式应为 owner/name"}
    try:
        st, _h, body = _https(API_HOST, "/repos/%s/releases/latest" % repo,
                              token=_token(cfg))
    except Exception as e:
        return {"ok": False, "error": "连接 GitHub 失败：%s" % e}
    if st == 404:
        return {"ok": False, "error": "仓库或 Release 不存在（私有仓库请填 update_token）"}
    if st != 200:
        return {"ok": False, "error": "GitHub 返回 HTTP %s" % st}
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return {"ok": False, "error": "Release 信息解析失败"}
    tag = str(data.get("tag_name") or "").lstrip("vV")
    asset = None
    for a in (data.get("assets") or []):
        name = str(a.get("name") or "")
        if name.lower().endswith(".zip") and "win" in name.lower():
            asset = a
            break
    if not asset:
        return {"ok": False, "error": "该 Release 没有 Windows 安装包（*win-x64.zip）"}
    digest = str(asset.get("digest") or "")
    return {
        "ok": True, "repo": repo, "current": current_version(), "latest": tag,
        "newer": _ver_tuple(tag) > _ver_tuple(current_version()),
        "name": asset.get("name"), "size": int(asset.get("size") or 0),
        "asset_api": asset.get("url") or "",
        "browser_url": asset.get("browser_download_url") or "",
        "sha256": digest[7:] if digest.startswith("sha256:") else "",
        "notes": str(data.get("body") or "")[:1500],
        "published": str(data.get("published_at") or ""),
        "skip": bool(tag and tag == str(cfg.get("update_skip_version") or "")),
    }


def _set(**kw):
    with _LOCK:
        STATE.update(kw)
        STATE["ts"] = time.time()


def status():
    with _LOCK:
        return dict(STATE)


def _updates_dir():
    d = (C.DATA_ROOT / "updates").resolve()
    d.mkdir(parents=True, exist_ok=True)
    root = C.DATA_ROOT.resolve()
    d.relative_to(root)
    return d


def download(info, cfg=None):
    """下载并校验资产到数据目录 updates/；返回 zip 路径。"""
    cfg = cfg or C.load_app_config()
    if not info.get("ok"):
        raise RuntimeError(info.get("error") or "更新信息无效")
    token = _token(cfg)
    udir = _updates_dir()
    tag = _SAFE_VER.sub("", str(info.get("latest") or "new")) or "new"
    dest = udir / ("update-%s.zip" % tag)
    part = udir / ("update-%s.part" % tag)
    dest.relative_to(udir)
    part.relative_to(udir)
    _set(phase="download", pct=0, msg="连接下载服务器…", version=tag)
    # 私有仓库用资产 API（带 Accept: octet-stream），公开仓库直接用下载地址
    target = info.get("asset_api") if token else info.get("browser_url")
    up = urllib.parse.urlsplit(target or "")
    if not up.hostname or up.scheme != "https":
        raise RuntimeError("下载地址异常")
    with part.open("wb") as sink:
        st, _h, size = _https(up.hostname,
                              up.path + ("?" + up.query if up.query else ""),
                              token=token, accept="application/octet-stream",
                              timeout=60, sink=sink)
    if st != 200:
        try:
            part.unlink()
        except OSError:
            pass
        raise RuntimeError("下载失败 HTTP %s" % st)
    expect = int(info.get("size") or 0)
    if expect and int(size) != expect:
        try:
            part.unlink()
        except OSError:
            pass
        raise RuntimeError("大小不符（%s != %s）" % (size, expect))
    want = str(info.get("sha256") or "")
    if want:
        _set(phase="download", pct=95, msg="校验文件完整性…")
        h = hashlib.sha256()
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest().lower() != want.lower():
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError("SHA256 校验失败")
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    _set(phase="downloaded", pct=100, msg="下载完成，可安装", zip=str(dest))
    return dest


def _safe_extract(zip_path, dest_dir):
    """解压 zip，逐条校验目标路径不越界（防 Zip Slip）。返回解压出的文件数。"""
    dest = pathlib.Path(dest_dir).resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError("压缩包含越界路径：%s" % name[:80])
            target = (dest / name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise RuntimeError("压缩包含越界路径：%s" % name[:80])
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > 200 * 1024 * 1024:
                raise RuntimeError("压缩包内单文件过大")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            count += 1
    return count


def _app_dir():
    """当前程序目录（frozen=exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(os.path.dirname(os.path.abspath(sys.executable))).resolve()
    return pathlib.Path(C.SCRIPTS_DIR).resolve()


def stage(zip_path):
    """解压到 updates/stage，返回 (staged_app_dir, 说明)。"""
    udir = _updates_dir()
    zpath = pathlib.Path(zip_path).resolve()
    zpath.relative_to(udir)          # 只允许安装自己下载到 updates/ 的包
    if not zpath.is_file():
        raise RuntimeError("安装包不存在，请先下载")
    base = udir / "stage"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    _set(phase="stage", pct=0, msg="解压安装包…")
    n = _safe_extract(zpath, base)
    cand = base / "ZCodeQQBot"
    if not (cand / "ZCodeQQBot.exe").is_file():
        if (base / "ZCodeQQBot.exe").is_file():
            cand = base
        else:
            raise RuntimeError("安装包里找不到 ZCodeQQBot.exe（文件数 %d）" % n)
    _set(phase="staged", pct=100, msg="已解压，可安装", zip=str(zpath))
    return cand


def write_installer(staged_dir, zip_path, app_dir=None):
    """生成更新批处理（不执行）。返回批处理路径。路径全部做数据目录/程序目录约束。"""
    udir = _updates_dir()
    app = pathlib.Path(app_dir).resolve() if app_dir else _app_dir()
    staged = pathlib.Path(staged_dir).resolve()
    staged.relative_to(udir)
    zpath = pathlib.Path(zip_path).resolve()
    zpath.relative_to(udir)
    bat = udir / "apply-update.bat"
    logf = udir / "apply-update.log"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "timeout /t 4 /nobreak >nul",
        "taskkill /IM ZCodeQQBot.exe /F >nul 2>&1",
        "timeout /t 2 /nobreak >nul",
        'robocopy "%s" "%s" /E /R:2 /W:1 /NFL /NDL /NJH /NJS >> "%s" 2>&1'
        % (staged, app, logf),
        'del /q "%s" >nul 2>&1' % zpath,
        'rmdir /s /q "%s" >nul 2>&1' % staged,
        'start "" "%s"' % (app / "ZCodeQQBot.exe"),
        'del /q "%~f0"',
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return bat


def make_installer(staged_dir, zip_path):
    """生成并启动更新批处理。调用后本进程应在数秒内退出（由批处理结束旧进程）。"""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("源码模式不支持自动更新，请 git pull")
    bat = write_installer(staged_dir, zip_path)
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(_updates_dir()),
                     creationflags=0x00000008 | 0x00000200,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    _set(phase="applying", pct=100, msg="正在安装，程序即将重启")
    return str(bat)


def run_update(cfg=None):
    """后台任务：检查 → 下载 → 解压 → 安装（供 UI 一键更新调用）。"""
    try:
        cfg = cfg or C.load_app_config()
        _set(phase="check", pct=0, msg="检查新版本…", error="")
        info = check(cfg)
        if not info.get("ok"):
            raise RuntimeError(info.get("error") or "检查失败")
        if not info.get("newer"):
            _set(phase="uptodate", pct=100, msg="已是最新版本 %s" % info.get("current"))
            return False
        zpath = download(info, cfg)
        staged = stage(zpath)
        make_installer(staged, zpath)
        return True
    except Exception as e:
        _set(phase="error", pct=0, msg=str(e)[:300], error=str(e)[:300])
        return False
