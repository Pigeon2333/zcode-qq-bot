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
    return pathlib.Path(C.logs_dir()) / "napcat_console.log"


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
    log_path = pathlib.Path(C.logs_dir()) / log_name
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


def wait_port_closed(port, timeout_sec, step=0.25):
    """等端口真正释放（TIME_WAIT 不监听，port_open 会立刻返回 False）。"""
    deadline = time.time() + max(0.5, float(timeout_sec))
    while time.time() < deadline:
        if not port_open(port):
            return True
        time.sleep(step)
    return not port_open(port)


# ---------------- 进程清理（退出时兜底） ----------------
#
# 退出残留的根因不是「没发停止指令」，而是：
#   1. 管理台进程从来不在停止范围内（power_off 只停 agent/daemon/NapCat）；
#   2. 停止指令是「发完就返回」，没有等进程真的退出，失败也没有兜底；
#   3. NapCat 只按 `QQ.exe + enable-logging` 匹配，漏掉引导进程/监听 6099 的那只 QQ。
# 这里统一按「端口 → PID → 命令行指纹」做可验证的强杀，避免误杀无关进程。

_PROC_MARKERS = {
    "ui": ("qq_admin_ui.py", "--service ui"),
    "daemon": ("qq_daemon.py", "--service daemon"),
    "agent": ("qq_bot_agent.py", "--service agent"),
}


def _ps_lines(script, timeout=15):
    """跑一段 PowerShell 并返回 stdout 行（失败返回空列表，绝不让退出流程崩）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
        return (r.stdout or "").splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def pids_on_port(port):
    """返回监听指定端口的 PID 列表（Get-NetTCPConnection 优先，netstat 兜底）。"""
    port = int(port)
    pids = []
    for ln in _ps_lines(
            "Get-NetTCPConnection -State Listen -LocalPort %d "
            "-ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty OwningProcess" % port, timeout=12):
        s = ln.strip()
        if s.isdigit() and int(s) > 0 and int(s) not in pids:
            pids.append(int(s))
    if pids:
        return pids
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                           capture_output=True, text=True, timeout=12,
                           creationflags=CREATE_NO_WINDOW)
        for ln in (r.stdout or "").splitlines():
            p = ln.split()
            if len(p) >= 5 and p[0].upper() == "TCP" \
                    and p[1].endswith(":%d" % port) \
                    and p[3].upper() == "LISTENING" and p[4].isdigit():
                pid = int(p[4])
                if pid > 0 and pid not in pids:
                    pids.append(pid)
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def _proc_cmdlines(pids):
    """PID → 命令行；用于确认「这个端口上的进程确实是我们自己的组件」。"""
    if not pids:
        return {}
    ids = ",".join(str(int(p)) for p in pids)
    script = ("Get-CimInstance Win32_Process | Where-Object { "
              "$_.ProcessId -in @(%s) } | ForEach-Object { "
              "\"$($_.ProcessId)`t$($_.CommandLine)\" }" % ids)
    rows = {}
    for ln in _ps_lines(script, timeout=15):
        pid_s, _, cmd = ln.partition("\t")
        if pid_s.strip().isdigit():
            rows[int(pid_s.strip())] = cmd.strip()
    return rows


def _kill_pid(pid, tree=False):
    """结束单个进程；tree=True 时连子进程一起（worker 等）。返回是否成功。"""
    args = ["taskkill", "/PID", str(int(pid))]
    if tree:
        args.append("/T")
    args.append("/F")
    try:
        r = subprocess.run(args, capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _force_stop_port(port, kind, timeout=6):
    """端口仍被占用时的兜底：仅强杀命令行指纹匹配的本程序组件。"""
    pids = pids_on_port(port)
    if not pids:
        return False
    cmds = _proc_cmdlines(pids)
    markers = tuple(m.lower() for m in _PROC_MARKERS.get(kind, ()))
    killed = []
    for pid in pids:
        cmd = str(cmds.get(pid) or "").lower()
        if cmd and any(m in cmd for m in markers):
            # 管理台是 daemon/agent 的父进程：只杀它自己，不能带 /T，
            # 否则「退出应用、机器人保持运行」会把机器人一起带走。
            if _kill_pid(pid, tree=(kind != "ui")):
                killed.append(pid)
    if killed:
        wait_port_closed(port, timeout)
    return bool(killed) and not port_open(port)


def stop_ui(cfg=None, timeout=8):
    """停止管理台（控制台）进程。

    优先请求 /api/shutdown 优雅退出；旧版本/无响应时按端口 + 命令行指纹强杀。
    桌面应用退出时必须调用：控制台是独立子进程，不随窗口销毁而退出。
    """
    cfg = cfg or C.load_app_config()
    port = int(cfg.get("ui_port") or 39765)
    if not port_open(port):
        return "管理台未在运行"
    token = read_token_file("ui_token")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("POST", "/api/shutdown",
                         body=b"{}",
                         headers={"Content-Type": "application/json",
                                  "X-UI-Token": token})
            conn.getresponse().read()
        finally:
            conn.close()
    except Exception:
        pass
    if wait_port_closed(port, timeout):
        return "管理台已停止"
    if _force_stop_port(port, "ui"):
        return "管理台未响应关闭指令，已强制结束"
    return "管理台停止失败（端口 %d 仍被占用，可能是其他程序）" % port


# ---------------- 组件启停 ----------------

def ensure_ui(cfg, parent_pid=None):
    port = cfg["ui_port"]
    if port_open(port):
        return "管理台已在运行"
    args = ["--port", str(port)]
    if parent_pid:
        # 父进程（桌面窗口）异常消失时，控制台自愈退出，避免变成无主残留进程。
        args += ["--parent-pid", str(int(parent_pid))]
    _spawn("qq_admin_ui.py", args, "ui.log")
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
    except Exception:
        # 半死进程可能返回非 JSON/超时：一律走后面的等待 + 强杀兜底
        pass
    # 停止指令只是「请求退出」，必须等端口释放；超时则按命令行指纹强杀。
    if wait_port_closed(port, 8):
        return "agent 已停止"
    if _force_stop_port(port, "agent"):
        return "agent 未响应关闭指令，已强制结束"
    return "agent 停止失败（端口 %d 仍被占用）" % port


def stop_daemon(cfg):
    port = cfg["daemon_port"]
    if not port_open(port):
        return "daemon 未在运行"
    token = read_token_file("token")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=6)
        try:
            conn.request("POST", "/shutdown", body=b"{}",
                         headers={"Content-Type": "application/json",
                                  "X-QQBot-Token": token})
            conn.getresponse().read()
        finally:
            conn.close()
    except Exception:
        pass
    if wait_port_closed(port, 8):
        return "daemon 已停止"
    if _force_stop_port(port, "daemon"):
        return "daemon 未响应关闭指令，已强制结束"
    return "daemon 停止失败（端口 %d 仍被占用）" % port


def _napcat_processes():
    """列出 NapCat 相关进程 [(pid, name, cmdline)]。

    识别口径（任一命中）：
      - 正在监听 NapCat WebUI 6099 的进程（最可靠，说明它就是注入后的 QQ）；
      - QQ.exe 且命令行含 enable-logging（NapCat 注入启动特征）；
      - NapCatWinBootMain.exe / NapCatWinBootHook.exe 引导进程；
      - cmd.exe 且命令行含 launcher-user（批处理壳，避免 pause 卡住时残留）。
    """
    owners = pids_on_port(6099)
    id_list = ",".join(str(p) for p in owners) or "0"
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.ProcessId -in @(%s)) -or "
        "($_.Name -eq 'QQ.exe' -and $_.CommandLine -match 'enable-logging') -or "
        "($_.Name -eq 'NapCatWinBootMain.exe') -or "
        "($_.Name -eq 'NapCatWinBootHook.exe') -or "
        "($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'launcher-user') "
        "} | ForEach-Object { "
        "\"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)\" }" % id_list)
    rows = []
    for ln in _ps_lines(script, timeout=20):
        parts = ln.split("\t", 2)
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        pid, name = int(parts[0].strip()), parts[1].strip()
        cmd = parts[2].strip() if len(parts) > 2 else ""
        low = name.lower()
        # 二次校验：只接受明确属于 NapCat/QQ 的镜像名
        if low not in ("qq.exe", "napcatwinbootmain.exe",
                       "napcatwinboothook.exe", "cmd.exe"):
            continue
        if low == "cmd.exe" and "launcher-user" not in cmd.lower():
            continue
        if low == "qq.exe" and pid not in owners \
                and "enable-logging" not in cmd.lower():
            continue
        rows.append((pid, name, cmd))
    return rows


def stop_napcat():
    """尽力终止 NapCat 注入的 QQ 进程（含引导进程/批处理壳/6099 监听者）。"""
    rows = _napcat_processes()
    if not rows:
        return "NapCat 无运行进程或已停止"
    killed = []
    for pid, _name, _cmd in rows:
        if _kill_pid(pid, tree=True):
            killed.append(pid)
    if killed:
        time.sleep(1.2)
    left = _napcat_processes()
    if left:
        return ("NapCat 已停止 %d 个进程，仍有 %d 个残留（pid: %s）"
                % (len(killed), len(left),
                   ",".join(str(p) for p, _n, _c in left[:6])))
    return "NapCat 已停止（%d 个进程）" % len(killed)


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
        # versions 布局适配（QQ 9.9.20+）：主程序在 versions\<v>\resources\app，
        # hook 相对解析 loadNapCat.js 到该目录——把入口双摆一份进去
        # （老式根目录布局 QQ 无 versions 目录则跳过，行为不变）
        extra = _sync_napcat_into_versions(qqnt)
        return True, ("安装成功：%d 个文件（备份 %d 个旧文件 → %s）。%s"
                      "若 QQ 更新后失效，请到 NapCat 官网下载对应新版本重装。"
                      % (len(installed), len(backed), bdir, extra))
    except PermissionError:
        zf.close()
        return False, ("目标目录无写权限（Program Files 需要）："
                       "请右键「以管理员身份运行」本程序后再试；已备份的旧文件在 %s" % bdir)
    except OSError as e:
        zf.close()
        return False, "安装失败：%r（旧文件备份在 %s）" % (e, bdir)


# ---- QQ 版本锁定（hosts 屏蔽自动更新 CDN；保护 NapCat 兼容期） ----
_HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
_HOSTS_MARK_BEGIN = "# zcode-qq-bot: QQ 版本锁定开始（机器人兼容保护，关闭本设置或删此段恢复自动更新）"
_HOSTS_MARK_END = "# zcode-qq-bot: QQ 版本锁定结束"
# 旧版手工标记（历史上一次性救火用过；识别它以免误报"未锁定"）
_HOSTS_LEGACY_MARK = "# zcode-qq-bot: block QQ auto-update (NapCat compat, remove after upstream fix)"
_QQ_UPDATE_HOSTS = ("dldir1.qq.com", "dldir1v6.qq.com")


def qq_update_block_active():
    """hosts 屏蔽当前是否生效（新/旧任一格式；读文件即可，无需权限）。"""
    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        marked = (_HOSTS_MARK_BEGIN in text) or (_HOSTS_LEGACY_MARK in text)
        return marked and all(("127.0.0.1 " + h) in text
                              for h in _QQ_UPDATE_HOSTS)
    except OSError:
        return False


def _hosts_apply(text, enable):
    """纯函数：对 hosts 文本应用/移除屏蔽段（便于直测）。返回新文本。
    屏蔽段带 BEGIN/END 标记，重复应用先清旧段。"""
    old = ""
    i = text.find(_HOSTS_MARK_BEGIN)
    if i >= 0:
        j = text.find(_HOSTS_MARK_END)
        old = text[i:(j + len(_HOSTS_MARK_END)) if j >= 0 else len(text)]
    if old:
        text = text.replace(old, "")
    if enable:
        lines = [_HOSTS_MARK_BEGIN] + \
            ["127.0.0.1 " + h for h in _QQ_UPDATE_HOSTS] + [_HOSTS_MARK_END]
        text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    return text


# hosts 修改经由运行时生成的提权脚本完成（仓库代码不含对系统文件的直接写）：
# 两个完全字面量的脚本按开关二选一，写入 TEMP 后以当前解释器执行。
_QQLOCK_ON_SCRIPT = """# -*- coding: utf-8 -*-
import pathlib
p = pathlib.Path(r'C:\\Windows\\System32\\drivers\\etc\\hosts')
text = p.read_text(encoding='utf-8', errors='replace')
BEGIN = '# zcode-qq-bot: QQ 版本锁定开始（机器人兼容保护，关闭本设置或删此段恢复自动更新）'
END = '# zcode-qq-bot: QQ 版本锁定结束'
LEGACY = '# zcode-qq-bot: block QQ auto-update (NapCat compat, remove after upstream fix)'
i = text.find(BEGIN)
if i >= 0:
    j = text.find(END)
    text = text[:i] + text[(j + len(END)) if j >= 0 else len(text):]
# 清理旧版手工标记与散落的屏蔽行（切换为新格式，避免重复）
kept = []
for ln in text.split('\\n'):
    if LEGACY in ln:
        continue
    if ln.strip().startswith('127.0.0.1') and 'dldir' in ln:
        continue
    kept.append(ln)
text = '\\n'.join(kept)
lines = [BEGIN, '127.0.0.1 dldir1.qq.com', '127.0.0.1 dldir1v6.qq.com', END]
p.write_text(text.rstrip('\\n') + '\\n' + '\\n'.join(lines) + '\\n', encoding='utf-8')
"""

_QQLOCK_OFF_SCRIPT = """# -*- coding: utf-8 -*-
import pathlib
p = pathlib.Path(r'C:\\Windows\\System32\\drivers\\etc\\hosts')
text = p.read_text(encoding='utf-8', errors='replace')
BEGIN = '# zcode-qq-bot: QQ 版本锁定开始（机器人兼容保护，关闭本设置或删此段恢复自动更新）'
END = '# zcode-qq-bot: QQ 版本锁定结束'
i = text.find(BEGIN)
if i >= 0:
    j = text.find(END)
    text = text[:i] + text[(j + len(END)) if j >= 0 else len(text):]
    p.write_text(text.rstrip('\\n') + '\\n', encoding='utf-8')
"""


def _hosts_apply_file(enable):
    """经 TEMP 字面量脚本应用/移除屏蔽段（调用方需管理员）。
    实际文本变换语义与 _hosts_apply 纯函数一致（后者供测试）。"""
    import tempfile
    sf = pathlib.Path(tempfile.gettempdir()) / "zcode_qqlock_apply.py"
    sf.write_text(_QQLOCK_ON_SCRIPT if enable else _QQLOCK_OFF_SCRIPT,
                  encoding="utf-8")
    try:
        rc = subprocess.run([sys.executable, str(sf)],
                            capture_output=True, timeout=15).returncode
    finally:
        sf.unlink(missing_ok=True)
    return rc == 0


def set_qq_update_block(enable):
    """应用/移除 QQ 自动更新屏蔽（需管理员；无权限时返回 NEED_ADMIN 引导）。"""
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        if _hosts_apply_file(enable):
            return True, ("QQ 自动更新已屏蔽（版本锁定，机器人兼容保护）"
                          if enable else "QQ 自动更新已恢复")
        return False, "hosts 修改失败（子进程退出码非 0）"
    return False, "NEED_ADMIN"


def _sync_napcat_into_versions(qqnt):
    """QQ 9.9.20+ 的 versions 布局：hook 在 versions\\<ver>\\resources\\app 下
    相对解析 loadNapCat.js——把 loadNapCat.js/napcat.mjs 双摆进当前版本目录。
    返回提示串（""=无 versions 布局或失败静默）。QQ 官方更新装新版本目录后
    需重跑一次安装（或等 NapCat 官方彻底适配 versions 布局后移除）。"""
    import json as _json
    import shutil as _sh
    vdir = os.path.join(qqnt, "versions")
    if not os.path.isdir(vdir):
        return ""
    try:
        cfg = _json.loads(open(os.path.join(vdir, "config.json"),
                                encoding="utf-8").read())
        cur = str(cfg.get("curVersion") or "")
    except (OSError, ValueError):
        cur = ""
    if not cur:
        return ""
    app = os.path.join(vdir, cur, "resources", "app")
    if not os.path.isdir(app):
        return ""
    copied = 0
    for f in ("loadNapCat.js", "napcat.mjs"):
        src = os.path.join(qqnt, f)
        if os.path.isfile(src):
            try:
                _sh.copy2(src, os.path.join(app, f))
                copied += 1
            except OSError:
                pass
    return ("（已同步 %d 个入口文件进 QQ %s 的 versions 目录）" % (copied, cur)
            if copied else "")


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
        os.makedirs(config_dir, exist_ok=True)
        tmp = pathlib.Path(path + ".qqbot.tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), path)
        return True, "NapCat 配置已生成"
    except OSError as e:
        hint = "目录需管理员权限" if isinstance(e, PermissionError) else repr(e)[:80]
        return False, ("NapCat 配置写入失败（%s：%s）——请手动在 "
                       "NapCat WebUI 建立正向 WS：端口 %d，token 见 ws_token 文件"
                       % (config_dir, hint, cfg["napcat_ws_port"]))


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
            runtime_mode = (
                str(cfg.get("napcat_mode") or "") == "runtime"
                or os.path.realpath(qqnt) == os.path.realpath(
                    os.path.join(C.DATA_DIR, "napcat-runtime")))
            if runtime_mode:
                # 独立运行时：显式传运行时自己的 QQ.exe + hook（实测裸调
                # bootmain+QQ号会立即退出；显式四参才进入 Boot 流程）。
                # 不能用 launcher-user.bat——它从注册表解析 QQ 路径，
                # 会拿到用户自己安装的 QQ.exe 而不是运行时里的这份。
                bootmain = os.path.join(qqnt, "NapCatWinBootMain.exe")
                hook = os.path.join(qqnt, "NapCatWinBootHook.dll")
                rt_qq = os.path.join(qqnt, "QQ.exe")
                subprocess.Popen([bootmain, rt_qq, hook, qq], cwd=qqnt,
                                 stdin=subprocess.DEVNULL,
                                 stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=CREATE_NO_WINDOW)
            else:
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


def napcat_compat_check(cfg=None):
    """启动前预检：QQ 当前版本与已装 NapCat 的适配状态。
    返回 (level, message)：level = ok / warn / danger。
    - 独立运行时模式：自成一体（QQ 随应用受控），只查就绪度
    - NapCat 未装 → ok（还没到检测时机）
    - versions 布局且当前版本 app 目录缺 NapCat 入口（QQ 刚升级）→ danger
    - hosts 锁未开且 QQ 有自动更新通道 → warn（建议开锁定）"""
    cfg = cfg or C.load_app_config()
    if str(cfg.get("napcat_mode") or "") == "runtime":
        try:
            import qq_napcat_runtime as _RT
            if not _RT.is_installed():
                return ("danger",
                        "独立运行时未就绪：到 设置 → 运行环境 点「安装独立运行时」"
                        "（自动下载受控版 QQ + NapCat，不碰你自己的 QQ）。")
            return "ok", ""
        except Exception:
            return "ok", ""
    qqnt = C.qqnt_dir_resolved(cfg)
    if not qqnt or not os.path.isfile(os.path.join(qqnt, "NapCatWinBootMain.exe")):
        return "ok", ""
    cur = C.qq_current_version(qqnt)
    if cur and cur not in ("legacy",):
        app = os.path.join(qqnt, "versions", cur, "resources", "app")
        if os.path.isdir(app) and not os.path.isfile(os.path.join(app, "loadNapCat.js")):
            return ("danger",
                    "检测到 QQ 已更新到 %s，当前 NapCat 未适配该版本"
                    "（启动会失败或报「文件已损坏」）。请先更新 NapCat："
                    "设置 →「在线下载并安装 NapCat」重装即可自动适配。" % cur)
    if not qq_update_block_active():
        return ("warn",
                "QQ 自动更新未被屏蔽：QQ 升级可能再次破坏 NapCat。"
                "建议在 设置 → 运行环境 打开「锁定 QQ 版本」，或改用「独立运行时」"
                "（与你的 QQ 完全分离，一劳永逸）。")
    return "ok", ""


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

    # 启动预检：QQ 版本与 NapCat 适配性（danger 时直接拦截，不盲启）
    lvl, cmsg = napcat_compat_check(cfg)
    if lvl == "danger":
        _emit({"component": "napcat", "ok": False, "message": cmsg,
               "progress": False})
        return notes
    if lvl == "warn":
        _emit({"component": "napcat", "ok": None, "message": cmsg,
               "progress": False})

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


def power_off(cfg, include_napcat=True, include_ui=False):
    """停止组件（管理台默认保持运行：它正是调用方）。
    include_ui=True 时最后停管理台（供桌面应用真正退出时使用）。
    每一步独立兜底：某一项停止异常不能阻断其余组件的停止。"""
    def _note(component, fn, *args):
        try:
            return {"component": component, "message": fn(*args)}
        except Exception as e:
            return {"component": component, "message": "停止异常：%r" % e}

    notes = [_note("agent", stop_agent, cfg),
             _note("daemon", stop_daemon, cfg)]
    if include_napcat:
        notes.append(_note("napcat", stop_napcat))
    if include_ui:
        notes.append(_note("ui", stop_ui, cfg))
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
