#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_services.py — 组件生命周期服务（UI 电源控制与 qq_app 共用）

能力：
  - 端口探测 + 幂等启动 daemon / agent / 管理台（subprocess 参数列表形式，
    stdout 重定向到日志文件句柄；不再经过 cmd 拼接，无引号注入面）
  - NapCat：快速登录启动；per-account OneBot WS 配置生成（token 自动管理）
  - 停止：daemon/agent 走各自 shutdown 接口，NapCat 尽力终止注入进程
  - 开机自启：HKCU Run 键注册/注销（当前用户级，无需管理员）

安全：
  - 所有子进程参数为静态列表 + 常量推导路径；日志文件位于数据目录
  - NapCat 配置写入 QQNT 目录通常需要管理员权限：失败时返回提示而非崩溃
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
import winreg

import qq_common as C

CREATE_NO_WINDOW = 0x08000000
RUN_KEY = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
AUTOSTART_NAME = "QQBotConsole"

PY_EXE = os.path.join(os.path.dirname(sys.executable), "python.exe")
PYW_EXE = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def port_open(port):
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_json(port, method, path, body=None, timeout=6):
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        return json.loads(conn.getresponse().read().decode("utf-8"))
    finally:
        conn.close()


def read_token_file(name):
    try:
        return (C.DATA_ROOT / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_ui_token():
    """确保 ui_token 文件存在（不存在则生成 32 hex），返回令牌。

    修复首次安装时序：窗口 URL 需要 ?tk=，但旧逻辑里令牌在 UI 进程收到
    第一个 HTTP 请求时才生成——qq_app 先读文件会拿到空串，窗口落锁。"""
    import secrets as _secrets
    p = C.DATA_ROOT / "ui_token"
    try:
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 24:
            return t
    except OSError:
        pass
    t = _secrets.token_hex(16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t, encoding="utf-8")
    except OSError:
        return t
    return t


def napcat_config_dir(cfg):
    """QQNT 目录下 NapCat 的 config 目录（不存在返回空串）。"""
    qqnt = C.qqnt_dir_resolved(cfg)
    if not qqnt:
        return ""
    d = os.path.realpath(os.path.join(qqnt, "config"))
    return d if os.path.isdir(d) else ""


def napcat_console_log():
    """NapCat 控制台输出落盘位置（启动时重定向；含 WebUi Token 与登录二维码）。"""
    return C.DATA_ROOT / "logs" / "napcat_console.log"


def napcat_webui_token(cfg):
    """读取 NapCat WebUI 登录密钥（扫码页要求填的 Token）。

    优先读 QQNT/config/webui.json 的 token；读不到（权限不足/尚未生成）则解析
    重定向下来的控制台日志里的 "WebUi Token:" 行。只读，不写日志、不外发。
    """
    cfg_dir = napcat_config_dir(cfg)
    if cfg_dir:
        p = os.path.join(cfg_dir, "webui.json")
        try:
            real = os.path.realpath(p)
            if os.path.dirname(real) == cfg_dir and os.path.isfile(real):
                data = json.loads(pathlib.Path(real).read_text(encoding="utf-8"))
                tok = str(data.get("token") or "").strip()
                if tok:
                    return tok
        except (OSError, ValueError, AttributeError):
            pass
    log = napcat_console_log()
    try:
        if log.is_file() and log.stat().st_size <= 8 * 1024 * 1024:
            hits = re.findall(r"WebUi Token:\s*(\S+)", log.read_text(
                encoding="utf-8", errors="replace"))
            if hits:
                return hits[-1]
    except OSError:
        pass
    return ""


def napcat_webui_url(cfg):
    """NapCat WebUI 地址；能取到密钥时带 ?token= 免输密钥直接进。"""
    base = "http://127.0.0.1:6099/webui"
    tok = napcat_webui_token(cfg)
    return (base + "?token=" + urllib.parse.quote(tok, safe="")) if tok else base


def _napcat_log_handle():
    """NapCat 控制台日志句柄（超过 4MB 轮转到 .old）。"""
    path = napcat_console_log()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
            old = path.with_suffix(".log.old")
            try:
                old.unlink()
            except OSError:
                pass
            path.replace(old)
    except OSError:
        pass
    return open(path, "a", encoding="utf-8", errors="replace")


def _spawn(script, args, log_name):
    """以参数列表启动组件脚本，stdout/stderr 追加重定向到数据目录日志。
    frozen（exe 发布版）：组件由主 exe 分身承载（qq_app --service <名>）。
    日志超过 20MB 时先轮转到 .old（组件重启时生效）。"""
    log_path = C.DATA_ROOT / "logs" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.exists() and log_path.stat().st_size > 20 * 1024 * 1024:
            old = log_path.with_suffix(".log.old")
            try:
                old.unlink()
            except OSError:
                pass
            log_path.replace(old)
    except OSError:
        pass
    logf = open(log_path, "a", encoding="utf-8", errors="replace")
    try:
        if getattr(sys, "frozen", False):
            # 打包模式：本 exe 即组件载体（qq_app main 里的 --service 分支）
            exe = os.path.abspath(sys.executable)
            base = os.path.basename(script).replace("qq_", "").replace(".py", "")
            service = {"daemon": "daemon", "bot_agent": "agent",
                       "admin_ui": "ui"}.get(base, base)
            subprocess.Popen(
                [exe, "--service", service] + args,
                cwd=C.SCRIPTS_DIR, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        else:
            subprocess.Popen(
                [PY_EXE, script] + args,
                cwd=C.SCRIPTS_DIR, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
    finally:
        # Popen 已继承句柄；父进程立即关闭自己的引用
        logf.close()


def wait_port(port, timeout_sec, step=1.5):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(step)
    return False


# ---------------- 组件启停 ----------------

def ensure_ui(cfg):
    port = cfg["ui_port"]
    if port_open(port):
        return "管理台已在运行"
    _spawn("qq_admin_ui.py", ["--port", str(port)], "ui.log")
    return "管理台已启动" if wait_port(port, 12) else "管理台启动失败（看 logs/ui.log）"


def ensure_daemon(cfg):
    port = cfg["daemon_port"]
    if port_open(port):
        return "daemon 已在运行"
    ws_token = read_token_file("ws_token")
    ctl_token = read_token_file("token")
    if not ctl_token:
        return "daemon 未启动：缺少控制 token（首次请用控制台初始化）"
    _spawn("qq_daemon.py", [
        "--ws-url", "ws://127.0.0.1:%d" % cfg["napcat_ws_port"],
        "--ws-token", ws_token,
        "--port", str(port),
        "--ctl-token", ctl_token], "daemon.log")
    return "daemon 已启动" if wait_port(port, 20) else "daemon 启动失败（看 logs/daemon.log）"


def ensure_agent(cfg):
    port = cfg["agent_admin_port"]
    if port_open(port):
        return "agent 已在运行"
    _spawn("qq_bot_agent.py", [
        "--port", str(cfg["daemon_port"]),
        "--admin-port", str(port)], "agent.log")
    return "agent 已启动" if wait_port(port, 20) else "agent 启动失败（看 logs/agent.log）"


def stop_agent(cfg):
    port = cfg["agent_admin_port"]
    if not port_open(port):
        return "agent 未在运行"
    try:
        http_json(port, "POST", "/admin/shutdown", {})
        return "agent 已停止"
    except OSError:
        return "agent 停止指令失败（进程可能已退出）"


def stop_daemon(cfg):
    port = cfg["daemon_port"]
    if not port_open(port):
        return "daemon 未在运行"
    token = read_token_file("token")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=6)
        conn.request("POST", "/shutdown", body=b"{}",
                     headers={"Content-Type": "application/json",
                              "X-QQBot-Token": token})
        conn.getresponse().read()
        conn.close()
        return "daemon 已停止"
    except OSError:
        return "daemon 停止指令失败"


def stop_napcat():
    """尽力终止 NapCat 注入的 QQ 进程（权限不足时部分进程可能残留）。"""
    killed = 0
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
             "Where-Object { $_.CommandLine -like '*enable-logging*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
             "$_.ProcessId }"],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW)
        killed = len([ln for ln in out.stdout.splitlines() if ln.strip().isdigit()])
    except (OSError, subprocess.SubprocessError):
        pass
    return "NapCat 已停止（%d 进程）" % killed if killed else "NapCat 无运行进程或已停止"


# ---------------- NapCat ----------------

def napcat_ws_config_path(cfg):
    qq = str(cfg.get("bot_qq") or "").strip()
    qqnt = C.qqnt_dir_resolved(cfg)
    if not qq or not qqnt:
        return ""
    return os.path.join(qqnt, "config", "onebot11_%s.json" % qq)


_NAPCAT_REQUIRED = ("napcat.mjs", "launcher-user.bat")
_NAPCAT_FILES = ("napcat.mjs", "loadNapCat.js", "NapCatWinBootHook.dll",
                 "NapCatWinBootMain.exe", "launcher-user.bat", "qqnt.json")


def install_napcat_zip(cfg, zip_path):
    """从本地 NapCat Shell 压缩包安装到 QQNT 目录。
    流程：zip 结构校验 → 备份现有 NapCat 文件（数据目录 napcat-backups/<ts>/）
    → 释放 → 返回 (ok, msg)。Program Files 无权限时给出指引而不是崩溃。"""
    import shutil
    import zipfile
    zp = str(zip_path or "").strip()
    qqnt = C.qqnt_dir_resolved(cfg)
    if not zp or not os.path.isfile(zp) or not zp.lower().endswith(".zip"):
        return False, "请选择 NapCat Shell 的 zip 压缩包"
    if not qqnt or not os.path.isfile(os.path.join(qqnt, "QQ.exe")):
        return False, "未找到 QQNT 目录（先在设置里填写或确认自动探测）"
    try:
        zf = zipfile.ZipFile(zp)
    except (OSError, zipfile.BadZipFile) as e:
        return False, "压缩包读取失败：%r" % e
    names = zf.namelist()
    # 兼容 zip 内多一层目录的情况
    base = ""
    if not any(n == "napcat.mjs" or n.startswith("./napcat.mjs") for n in names):
        tops = set(n.split("/")[0] for n in names if n.strip() and n != "/")
        for t in tops:
            if any(n in (t + "/napcat.mjs", t + "/launcher-user.bat") for n in names):
                base = t + "/"
                break
    for req in _NAPCAT_REQUIRED:
        if not any(n == base + req for n in names):
            zf.close()
            return False, "压缩包缺少 %s（请下载 NapCat Shell 完整包，不是源码包）" % req
    # 备份现有文件
    bdir = os.path.join(C.DATA_DIR, "napcat-backups", time.strftime("%Y%m%d-%H%M%S"))
    backed = []
    try:
        os.makedirs(bdir, exist_ok=True)
        for f in _NAPCAT_FILES:
            src = os.path.join(qqnt, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(bdir, f))
                backed.append(f)
        # 释放（覆盖）
        installed = []
        installed = []
        _safe_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,59}$")
        for n in names:
            if n.strip() in ("", "/") or n.endswith("/"):
                continue
            rel = n[len(base):] if base and n.startswith(base) else n
            # 严格文件名白名单：无路径分隔/盘符/点开头（防 zip 条目名穿越）
            if not _safe_name.match(rel) or rel in (".", ".."):
                continue
            if not rel.lower().endswith((".js", ".mjs", ".dll", ".exe", ".bat", ".json")):
                continue  # 只释放根目录的 NapCat 文件（根目录布局）
            # pathlib 规范化并限制在 QQNT 目录内
            target = (pathlib.Path(qqnt) / rel).resolve()
            try:
                target.relative_to(pathlib.Path(qqnt).resolve())
            except ValueError:
                continue
            with zf.open(n) as srcf:
                target.write_bytes(srcf.read())
            installed.append(rel)
        zf.close()
        return True, ("安装成功：%d 个文件（备份 %d 个旧文件 → %s）。"
                      "若 QQ 更新后失效，请到 NapCat 官网下载对应新版本重装。"
                      % (len(installed), len(backed), bdir))
    except PermissionError:
        zf.close()
        return False, ("目标目录无写权限（Program Files 需要）："
                       "请右键「以管理员身份运行」本程序后再试；已备份的旧文件在 %s" % bdir)
    except OSError as e:
        zf.close()
        return False, "安装失败：%r（旧文件备份在 %s）" % (e, bdir)


def napcat_ws_token_of(path, want_port=None):
    """读 NapCat onebot11 配置里正向 WS 的 token；找不到返回 None。
    优先 name=qqbot 的条目，其次第一个 enable 且端口匹配的。"""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        servers = (((data or {}).get("network") or {}).get("websocketServers")) or []
    except (OSError, ValueError):
        return None
    pick = None
    for s in servers:
        if not isinstance(s, dict):
            continue
        if str(s.get("name", "")) == "qqbot":
            pick = s
            break
        if want_port is not None and s.get("enable") \
                and int(s.get("port") or 0) == int(want_port) and pick is None:
            pick = s
    if pick is None:
        return None
    return str(pick.get("token", ""))


def write_token_file(name, value):
    """写数据目录下的令牌文件（原子替换）。"""
    p = C.DATA_ROOT / name
    tmp = C.DATA_ROOT / (name + ".qqbot.tmp")
    tmp.write_text(str(value), encoding="utf-8")
    os.replace(str(tmp), str(p))


def ensure_napcat_config(cfg):
    """为当前 bot_qq 生成 OneBot WS 配置（若缺失）；已存在时校验 token：
    NapCat 侧 token 变了（重登/WebUI 改配置后会重新生成）就采纳它的值写回
    ws_token 文件——NapCat 是活着的一侧，采纳它无需重启 QQ，下次重连即通。"""
    path = napcat_ws_config_path(cfg)
    if not path:
        return False, "缺少 bot_qq 或 QQNT 目录，无法定位 NapCat 配置"
    if os.path.isfile(path):
        theirs = napcat_ws_token_of(path, cfg.get("napcat_ws_port"))
        if theirs is not None and theirs != read_token_file("ws_token"):
            try:
                write_token_file("ws_token", theirs)
                return True, "NapCat 已更换 token，已自动同步（机器人将自动重连）"
            except OSError:
                pass
        return True, "NapCat 配置已存在"
    qqnt = C.qqnt_dir_resolved(cfg)
    config_dir = os.path.realpath(os.path.join(qqnt, "config"))
    if os.path.realpath(os.path.dirname(path)) != config_dir:
        return False, "配置路径异常，拒绝写入"
    ws_token = read_token_file("ws_token")
    config = {
        "network": {"websocketServers": [{
            "name": "qqbot", "enable": True, "host": "127.0.0.1",
            "port": cfg["napcat_ws_port"], "messagePostFormat": "array",
            "reportSelfMessage": True, "enableForcePushEvent": True,
            "token": ws_token, "debug": False}],
            "httpServers": [], "httpSseServers": [], "httpClients": [],
            "websocketClients": [], "plugins": []},
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {"baseTimeout": 10000, "uploadSpeedKBps": 256,
                    "downloadSpeedKBps": 256, "maxTimeout": 1800000}}
    try:
        tmp = pathlib.Path(path + ".qqbot.tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), path)
        return True, "NapCat 配置已生成"
    except OSError:
        return False, ("NapCat 配置写入失败（%s 目录需管理员权限）——请手动在 "
                       "NapCat WebUI 建立正向 WS：端口 %d，token 见 ws_token 文件"
                       % (config_dir, cfg["napcat_ws_port"]))


def start_napcat(cfg, wait_sec=150, on_progress=None):
    """快速登录启动 NapCat；返回 (ok, message)。

    on_progress(message)：启动过程中的阶段提示回调（配置/启动/等待登录/就绪），
    供管理台实时展示，不传则行为与旧版一致。
    """
    def _p(msg):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    qq = str(cfg.get("bot_qq") or "").strip()
    qqnt = C.qqnt_dir_resolved(cfg)
    if not qq:
        return False, "未设置机器人 QQ 号（控制台 → 设置）"
    if not qqnt or not os.path.isfile(os.path.join(qqnt, "QQ.exe")):
        return False, "未找到 QQNT/QQ.exe（控制台 → 设置里填写安装目录）"
    launcher = os.path.join(qqnt, "launcher-user.bat")
    if not os.path.isfile(launcher):
        return False, "QQNT 目录下无 launcher-user.bat：请先安装 NapCat Shell"
    # 先跑 ensure（含 token 采纳自愈），再判断是否已在运行——
    # 否则「NapCat 在跑但 token 已变」的场景永远走不到同步逻辑
    ok_cfg, msg_cfg = ensure_napcat_config(cfg)
    _p("NapCat 配置：" + msg_cfg)
    if not ok_cfg and not os.path.isfile(napcat_ws_config_path(cfg)):
        return False, msg_cfg
    if port_open(cfg["napcat_ws_port"]):
        return True, "NapCat 已在运行"
    if port_open(6099):
        _p("NapCat 已在运行（WebUI 在线），无需重复启动 QQ，直接等待账号登录…")
    else:
        _p("正在启动 QQNT + NapCat（快速登录 %s），登录可能需要扫码…" % qq)
        # 控制台输出落盘：WebUi Token 与二维码都只在这里出现（无窗口启动看不到）
        logf = _napcat_log_handle()
        try:
            subprocess.Popen(["cmd", "/c", launcher, qq], cwd=qqnt,
                             stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
        finally:
            logf.close()
    deadline = time.time() + wait_sec
    webui_seen = False
    while time.time() < deadline:
        if port_open(cfg["napcat_ws_port"]):
            _p("NapCat WS 已就绪（端口 %d 监听中）" % cfg["napcat_ws_port"])
            return True, "NapCat 已就绪"
        if port_open(6099):
            if not webui_seen:
                webui_seen = True
                url = napcat_webui_url(cfg)
                _p("NapCat WebUI 已启动，但 WS 尚未开放 —— 正在等待账号登录；"
                   "即将自动打开登录页面"
                   + ("（已自动带上登录密钥）" if "?token=" in url else "")
                   + "，请扫码或快速登录")
                try:
                    import webbrowser
                    webbrowser.open(url)
                except Exception:
                    pass
        time.sleep(2)
    if port_open(6099):
        return False, ("NapCat 已运行但账号未登录（WS 未开放）：请到「状态」页"
                       "「NapCat 登录密钥」卡片点「打开登录页（免密钥）」，"
                       "或打开 http://127.0.0.1:6099/webui 手动填密钥，完成扫码/快速登录；"
                       "登录成功后 WS 会自动开放，daemon 将自动连上")
    return False, ("NapCat 启动超时（%d 秒内 WS 未就绪）：若登录凭据失效，请到「状态」页"
                   "「NapCat 登录密钥」卡片打开登录页重新扫码（%d 秒）"
                   % (wait_sec, wait_sec))


def power_on(cfg, on_note=None):
    """一键启动全部组件（幂等）；on_note(step_dict) 实时回调每步结果。"""
    notes = []

    def _emit(note):
        notes.append(note)
        if on_note:
            try:
                on_note(note)
            except Exception:
                pass

    ok, msg = start_napcat(cfg, on_progress=lambda m: _emit(
        {"component": "napcat", "ok": None, "message": m, "progress": True}))
    _emit({"component": "napcat", "ok": ok, "message": msg})

    def _ok_note(m):
        # ensure_* 失败时返回文案含「失败/未启动」——据此标红，不再一律打绿勾
        return ("失败" not in str(m)) and ("未启动" not in str(m))
    dmsg, amsg = ensure_daemon(cfg), ensure_agent(cfg)
    _emit({"component": "daemon", "ok": _ok_note(dmsg), "message": dmsg})
    _emit({"component": "agent", "ok": _ok_note(amsg), "message": amsg})
    return notes


def power_off(cfg, include_napcat=True):
    """停止组件（管理台保持运行）。"""
    notes = [{"component": "agent", "message": stop_agent(cfg)},
             {"component": "daemon", "message": stop_daemon(cfg)}]
    if include_napcat:
        notes.append({"component": "napcat", "message": stop_napcat()})
    return notes


# ---------------- 开机自启 ----------------

def autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def set_autostart(enable):
    """注册/注销当前用户级开机自启（启动 qq_app 静默窗口模式）。"""
    app_entry = os.path.join(C.SCRIPTS_DIR, "qq_app.py")
    launcher = PYW_EXE if os.path.isfile(PYW_EXE) else PY_EXE
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (launcher, app_entry))
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except OSError:
                    pass
        finally:
            winreg.CloseKey(key)
    except OSError as e:
        return False, "注册表写入失败: %r" % e
    cfg = C.load_app_config()
    cfg["autostart_with_windows"] = enable
    C.save_app_config(cfg)
    return True, ("已开启开机自启" if enable else "已关闭开机自启")
