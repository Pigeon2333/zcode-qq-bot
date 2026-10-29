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
# 国内直连 GitHub 很慢/不稳：auto 模式下失败会改用这些加速前缀重试。
# 加速站不可信也没关系——内容必须通过 GitHub 提供的 SHA256 才被接受。
MIRRORS = ["https://gh-proxy.com/", "https://ghfast.top/"]
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
           sink=None, redirect_left=4, on_chunk=None, total_hint=0, allow=None):
    """GET 指定主机路径；返回 (status, headers, size|body)。
    sink 给出时把响应体写入该二进制句柄（须由调用方在数据目录内打开）；
    on_chunk(got, total) 用于汇报下载进度。"""
    if host not in (allow or ALLOW_HOSTS):
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
                          sink=sink, redirect_left=redirect_left - 1,
                          on_chunk=on_chunk, total_hint=total_hint, allow=allow)
        if sink is not None:
            total = int(resp.getheader("Content-Length") or 0) or int(total_hint or 0)
            got = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_ZIP:
                    raise RuntimeError("文件超过大小上限")
                sink.write(chunk)
                if on_chunk:
                    on_chunk(got, total)   # 允许回调抛异常中止（如"速度过慢"）
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
    cur_v = _ver_tuple(current_version())
    # 聚合「当前版本之后」的全部更新日志：/releases/latest 只有一条，
    # 0.1.5 的用户升级到 0.1.7 时应同时看到 0.1.6/0.1.7 的内容
    pending = []
    try:
        st2, _h2, body2 = _https(API_HOST, "/repos/%s/releases?per_page=30" % repo,
                                 token=_token(cfg))
        if st2 == 200:
            for rel in json.loads(body2.decode("utf-8")):
                t = str(rel.get("tag_name") or "").lstrip("vV")
                note = str(rel.get("body") or "").strip()
                if t and note and _ver_tuple(t) > cur_v:
                    pending.append({"ver": t, "notes": note[:900]})
            pending.sort(key=lambda x: _ver_tuple(x["ver"]), reverse=True)
    except Exception:
        pass  # 列表拉取失败不影响主流程，退回单条 notes
    if not pending:
        pending = [{"ver": tag, "notes": str(data.get("body") or "")[:900]}]
    return {
        "ok": True, "repo": repo, "current": current_version(), "latest": tag,
        "newer": _ver_tuple(tag) > cur_v,
        "name": asset.get("name"), "size": int(asset.get("size") or 0),
        "asset_api": asset.get("url") or "",
        "browser_url": asset.get("browser_download_url") or "",
        "sha256": digest[7:] if digest.startswith("sha256:") else "",
        "notes": str(data.get("body") or "")[:1500],
        "pending": pending[:10],
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


def _mirror_bases(cfg):
    m = str((cfg or {}).get("update_mirror") or "auto").strip()
    if m == "off":
        return []
    if m in ("", "auto"):
        return list(MIRRORS)
    return [m if m.endswith("/") else m + "/"]


def _fetch_to(url, token, part, expect, allow=None, tag=""):
    """把 url 下到 part；返回实际字节数。进度写入 STATE。
    30 秒内不足 2MB 视为直连过慢，抛异常以触发镜像重试。"""
    up = urllib.parse.urlsplit(url or "")
    if not up.hostname or up.scheme != "https":
        raise RuntimeError("下载地址异常")
    started = time.time()
    def _prog(got, total):
        total = total or expect
        pct = int(min(90, got * 90.0 / total)) if total else 0
        _set(phase="download", pct=pct,
             msg="下载中 %.1f/%s" % (got / 1048576.0,
                                     ("%.1fMB" % (total / 1048576.0)) if total else "?"))
        if time.time() - started > 30 and got < 4 * 1024 * 1024:
            raise RuntimeError("速度过慢（30 秒 %d KB）" % (got // 1024))
    with part.open("wb") as sink:
        st, _h, size = _https(up.hostname,
                              up.path + ("?" + up.query if up.query else ""),
                              token=token, accept="application/octet-stream",
                              timeout=60, sink=sink, on_chunk=_prog,
                              total_hint=expect, allow=allow)
    if st != 200:
        raise RuntimeError("下载失败 HTTP %s" % st)
    if expect and int(size) != expect:
        raise RuntimeError("大小不符（%s != %s）" % (size, expect))
    return size


def _sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def download(info, cfg=None):
    """下载并校验资产到数据目录 updates/；返回 zip 路径。

    先直连 GitHub；失败（超时/被墙/非 200）时按配置走加速镜像重试。
    只有 SHA256 与 GitHub 提供值一致才接受镜像内容。
    """
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
    expect = int(info.get("size") or 0)
    want = str(info.get("sha256") or "")
    _set(phase="download", pct=0, msg="连接下载服务器…", version=tag)
    # 私有仓库用资产 API（带 Accept: octet-stream），公开仓库直接用下载地址
    target = info.get("asset_api") if token else info.get("browser_url")
    errors = []
    try:
        _fetch_to(target, token, part, expect, tag=tag)
    except Exception as e:
        errors.append("直连失败：%s" % e)
        if token:
            # 私有仓库的资产需要 Authorization 头，第三方镜像无法携带
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError("；".join(errors) +
                               "（私有仓库不支持镜像加速：镜像无法携带 Token，请检查网络或代理）")
        if not want:
            # 没有官方摘要就不接受第三方镜像（无法证明内容未被篡改）
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError("；".join(errors))
        got_ok = False
        for base in _mirror_bases(cfg):
            try:
                _set(phase="download", pct=0,
                     msg="直连较慢/失败，改用加速镜像 %s" % base)
                murl = base + str(info.get("browser_url") or "")
                mhost = urllib.parse.urlsplit(murl).hostname or ""
                _fetch_to(murl, "", part, expect,
                          allow={mhost} | ALLOW_HOSTS, tag=tag)
                got_ok = True
                break
            except Exception as e2:
                errors.append("镜像 %s 失败：%s" % (base, e2))
        if not got_ok:
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError("；".join(errors))
    if want:
        _set(phase="download", pct=95, msg="校验文件完整性…")
        if _sha256(part) != want.lower():
            try:
                part.unlink()
            except OSError:
                pass
            raise RuntimeError("SHA256 校验失败（已丢弃下载内容）")
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
        # 无控制台启动时 timeout 会直接报错跳过，用 ping 计时
        "ping -n 5 127.0.0.1 >nul",
        "taskkill /IM ZCodeQQBot.exe /F >nul 2>&1",
        "ping -n 3 127.0.0.1 >nul",
        'robocopy "%s" "%s" /E /R:3 /W:2 /NFL /NDL /NJH /NJS >> "%s" 2>&1'
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
