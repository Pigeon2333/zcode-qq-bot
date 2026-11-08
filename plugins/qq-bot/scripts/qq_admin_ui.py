"""qq_admin_ui.py — QQ 机器人管理 UI（本机 Web 界面）

浏览器打开 http://127.0.0.1:39765 ：
  - 状态：daemon / agent / NapCat 连接、账号、统计、模型链降级状态
  - 模型：降级链排序编辑、暂停/恢复机器人
  - 人设/风格卡：在线编辑 personas/current.md 与 styles/current_style.md
  - 记忆：memory_stream.jsonl 浏览与清空
  - 表情包：仓库清单

安全模型：仅绑定 127.0.0.1；所有读写文件路径均为模块级常量；
不做鉴权（回环绑定 + 本机单用户场景），勿将端口暴露到局域网。

用法：python qq_admin_ui.py [--port 39765] [--daemon-port 39763] [--agent-port 39764]
"""

import argparse
import http.client
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qq_common as C
import qq_services as S

DATA_DIR = C.DATA_DIR
TOKEN_PATH = os.path.join(DATA_DIR, "token")
API_KEY_PATH = os.path.join(DATA_DIR, "zhipu_api_key")
MODEL_CHAIN_PATH = os.path.join(DATA_DIR, "model_chain.json")
GROUPS_PATH = os.path.join(DATA_DIR, "group_whitelist.json")
GROUP_MODES_PATH = os.path.join(DATA_DIR, "group_reply_modes.json")
PRIVATE_CONFIG_PATH = os.path.join(DATA_DIR, "private_config.json")
REPLY_CONFIG_PATH = os.path.join(DATA_DIR, "reply_config.json")
PERSONA_COLLECTION_PATH = os.path.join(DATA_DIR, "personas", "collection.json")
STYLE_PATH = os.path.join(DATA_DIR, "styles", "current_style.md")
MEMORY_STREAM_PATH = os.path.join(DATA_DIR, "memory_stream.jsonl")
KEYS_PATH = os.path.join(DATA_DIR, "api_keys.json")
STICKERS_PATH = os.path.join(DATA_DIR, "stickers.json")
EVOLVED_PATH = os.path.join(DATA_DIR, "styles", "evolved.json")
PERSONA_ROUTES_PATH = os.path.join(DATA_DIR, "persona_routes.json")
MEMBER_PROFILES_PATH = os.path.join(DATA_DIR, "member_profiles.json")


def _mp_write(members):
    d = load_json_const(MEMBER_PROFILES_PATH, {})
    if not isinstance(d, dict):
        d = {}
    d["members"] = members
    write_const(MEMBER_PROFILES_PATH, json.dumps(d, ensure_ascii=False, indent=2))
STICKERS_ROOT = pathlib.Path(os.path.join(DATA_DIR, "stickers"))
WS_TOKEN_PATH = os.path.join(DATA_DIR, "ws_token")
UI_TOKEN_PATH = os.path.join(DATA_DIR, "ui_token")


def ui_token():
    """管理台访问令牌：首次读取时生成（32 位十六进制）。
    窗口由 qq_app 携带 ?tk= 打开；浏览器直开需手动解锁（防本机网页 CSRF 打本地接口）。"""
    import secrets as _secrets
    try:
        t = pathlib.Path(UI_TOKEN_PATH).read_text(encoding="utf-8").strip()
        if len(t) >= 24:
            return t
    except OSError:
        pass
    t = _secrets.token_hex(16)
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    pathlib.Path(UI_TOKEN_PATH).write_text(t, encoding="utf-8")
    return t

DEFAULT_UI_PORT = 39765
DEFAULT_DAEMON_PORT = 39763
DEFAULT_AGENT_PORT = 39764

PORTS = {"daemon": DEFAULT_DAEMON_PORT, "agent": DEFAULT_AGENT_PORT}


def _agent_port():
    """agent 管理口实际端口：配置优先（自定义端口场景代理打错机的根因）。"""
    try:
        return int(C.load_app_config().get("agent_admin_port",
                                           PORTS["agent"]))
    except (TypeError, ValueError):
        return PORTS["agent"]


def _daemon_port():
    try:
        return int(C.load_app_config().get("daemon_port", PORTS["daemon"]))
    except (TypeError, ValueError):
        return PORTS["daemon"]

# 最近一次「启动机器人」任务的实时状态（内存态，UI 进程内）
# 必须 RLock：启动 worker 在 with _boot_lock 块内调 _boot_state_persist()
# （persist 自己也要拿锁）——普通 Lock 不可重入 → 启动一结束就自死锁且
# 永久持锁，power/status、runlog 全挂（0.1.15 状态页打不开的根因）
_boot_lock = threading.RLock()
_boot_state = {"running": False, "done": False, "failed": False,
               "started_at": 0.0, "final": "", "steps": []}

def _boot_state_persist():
    """把最近一次启动任务结果写盘——UI 重启后状态页仍能看到失败原因。"""
    try:
        with _boot_lock:
            data = {"failed": _boot_state["failed"], "final": _boot_state["final"],
                    "done": _boot_state["done"], "running": False,
                    "steps": _boot_state["steps"][-40:], "ts": time.time()}
        write_const(os.path.join(DATA_DIR, "boot_state.json"),
                    json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _boot_state_resolve_if_recovered():
    """上次启动若因「账号未登录」失败、而此刻 WS 已连上（扫码完成后
    daemon 自动重连成功），把启动记录标记为已恢复——运行日志/状态页的
    「启动失败」红字不再残留吓人（历史过程行保留，仅终态改为已恢复）。"""
    with _boot_lock:
        if not _boot_state["failed"] or _boot_state["running"]:
            return
        if "未登录" not in str(_boot_state.get("final") or ""):
            return
        _boot_state["failed"] = False
        _boot_state["final"] = "已恢复：登录成功，组件全部在线（此前启动时的登录等待失败已解决）"
        _boot_state["steps"] = (_boot_state.get("steps") or [])[-40:] + [{
            "ts": time.time(), "component": "napcat",
            "ok": True, "message": "检测到 WS 已连接——账号登录成功，启动失败标记自动解除"}]
        _boot_state_persist()


def _boot_state_load():
    """UI 刚启动、内存态为空时，从盘上恢复最近一次启动结果。"""
    try:
        d = json.loads(read_const(os.path.join(DATA_DIR, "boot_state.json"), "{}"))
        if isinstance(d, dict) and d.get("final"):
            with _boot_lock:
                if not _boot_state["steps"] and not _boot_state["running"]:
                    _boot_state.update({"steps": d.get("steps") or [],
                                        "failed": bool(d.get("failed")),
                                        "done": bool(d.get("done")),
                                        "final": str(d.get("final"))[:200],
                                        "running": False})
    except Exception:
        pass


# NapCat 活性看门狗（UI 进程内运行：能调 power_off/on 做整机恢复而不自杀）
_wd_lock = threading.Lock()
_wd_state = {"events": [], "strikes": 0, "last_check": 0.0, "busy": False,
             "last_result": "", "last_comp": 0.0, "last_full": 0.0,
             "full_times": []}

# 最近一次「检查更新」结果（内存缓存；只在用户点检查或自动检查时刷新）
_update_lock = threading.Lock()
_update_cache = {}


def _wd_event(kind, detail):
    with _wd_lock:
        _wd_state["events"].insert(0, {"ts": time.time(), "kind": kind,
                                       "detail": str(detail)[:200]})
        del _wd_state["events"][20:]


def _wd_notify_owner(cfg, text):
    """尽力把看门狗事件私聊告知主人（daemon 可用才会成功，失败忽略）。"""
    owner = str(cfg.get("owner_qq") or "").strip()
    if not owner:
        return
    try:
        token = read_const(TOKEN_PATH)
        conn = http.client.HTTPConnection("127.0.0.1", _daemon_port(), timeout=8)
        body = json.dumps({"chat_type": "private", "chat_id": owner,
                           "texts": [text]}, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/send", body, {"Content-Type": "application/json",
                                             "X-QQBot-Token": token})
        conn.getresponse().read()
        conn.close()
    except Exception:
        pass


def _wd_recover(cfg, scope="components"):
    """自动恢复。scope:
      components = 只补启没在跑的本程序组件（绝不动 QQ/NapCat，避免反复登录触发风控）
      full       = 整机电源周期（NapCat+daemon+agent）——仅用于 WS 半死（TCP 在但不推事件）
    带冷却：components 5 分钟内不重复；full 15 分钟内不重复且每小时最多 3 次。
    """
    now = time.time()
    with _wd_lock:
        if _wd_state["busy"]:
            return "上一次恢复仍在进行"
        if scope == "full":
            if now - _wd_state.get("last_full", 0) < 900:
                return "整机恢复冷却中，跳过"
            recent = [t for t in _wd_state.get("full_times", []) if now - t < 3600]
            if len(recent) >= 3:
                _wd_state["last_result"] = "整机恢复过于频繁（1 小时 3 次），已暂停自动恢复，请人工检查"
                return _wd_state["last_result"]
        elif now - _wd_state.get("last_comp", 0) < 300:
            return "补启冷却中，跳过"
        _wd_state["busy"] = True
    try:
        if scope == "full":
            S.power_off(cfg, include_napcat=True)
            time.sleep(2)
            notes = S.power_on(cfg)
            ok = any(n.get("component") == "napcat" and n.get("ok") for n in notes)
            result = "WS 半死：已整机重启 NapCat 与组件" + ("（成功）" if ok else "（WS 未就绪，可能需扫码）")
            with _wd_lock:
                _wd_state["last_full"] = time.time()
                _wd_state["full_times"] = recent + [time.time()]
            _wd_notify_owner(cfg, "看门狗：" + result)
        else:
            parts = []
            if not S.port_open(_daemon_port()):
                parts.append(S.ensure_daemon(cfg))
            if not S.port_open(_agent_port()):
                parts.append(S.ensure_agent(cfg))
            result = "已补启组件（未动 QQ）：" + ("；".join(p for p in parts if p) or "无需补启")
            with _wd_lock:
                _wd_state["last_comp"] = time.time()
        _wd_event("recover", result)
        return result
    finally:
        with _wd_lock:
            _wd_state["busy"] = False


def _wd_check_once():
    """单次健康检查。返回 (healthy, detail)。连续异常累计 strikes，达阈值动作。

    分级：只有 daemon 活着但动作通道没响应（WS 半死）才值得重启 QQ；
    daemon 不在跑/没连上，最多补启组件，绝不为此重启 NapCat。
    """
    cfg = C.load_app_config()
    if not cfg.get("watchdog_enabled"):
        return True, "disabled"
    with _boot_lock:
        if _boot_state["running"]:
            return True, "boot-in-progress"
    daemon_up = S.port_open(_daemon_port())
    napcat_up = S.port_open(6099)
    if not daemon_up and not napcat_up:
        with _wd_lock:  # 用户主动停止的全部组件：不算故障
            _wd_state["strikes"] = 0
        return True, "all-stopped"
    healthy, detail, scope = True, "", "components"
    if daemon_up:
        r = proxy_get("127.0.0.1", _daemon_port(), "/status",
                      read_const(TOKEN_PATH), timeout=4)
        if not r.get("ok"):
            healthy, detail = False, "daemon /status 异常"
        elif not r.get("ws_connected"):
            healthy, detail = False, "WS 未连接（daemon 会自动重连）"
        elif r.get("ws_alive") is False:  # 老版本 daemon 无此字段时不误报
            healthy, detail = False, "WS 半死（动作通道无响应）"
            scope = "full"
        if r.get("ws_connected"):
            _boot_state_resolve_if_recovered()
    else:
        healthy, detail = False, "daemon 未运行（NapCat 仍在线）"
    with _wd_lock:
        _wd_state["last_check"] = time.time()
        if healthy:
            _wd_state["strikes"] = 0
            _wd_state["last_result"] = "正常"
            return True, "ok"
        _wd_state["strikes"] += 1
        strikes = _wd_state["strikes"]
    _wd_event("alert", "第 %d 次探测异常：%s" % (strikes, detail))
    if strikes >= 2:
        mode = str(cfg.get("watchdog_mode") or "auto")
        if mode == "auto":
            result = _wd_recover(cfg, scope)
            with _wd_lock:
                _wd_state["strikes"] = 0
                _wd_state["last_result"] = result
            return False, result
        _wd_notify_owner(cfg, "看门狗：检测到机器人推送异常（%s）。当前为仅告警模式，请到控制台手动重启。" % detail)
        with _wd_lock:
            _wd_state["last_result"] = "告警：%s" % detail
    return healthy, detail


def _svc_out(line):
    """UI 服务进程的结构化日志（stdout 由启动方重定向到服务日志文件）。
    注意：qq_admin_ui 不 import qq_daemon（那边是 daemon 进程的东西）——
    _gsv_autostart 曾误调 daemon 的 out() → NameError 被双层 except 吞掉，
    自启动线程无声死亡（配置开着也从没真正拉起过引擎）。"""
    print("[%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)


def _gsv_autostart():
    """应用启动时可选拉起本地语音引擎（voice.gsv_autostart，默认关）。
    模型加载阻塞最多 2 分钟，独立线程不拖慢 UI 服务。"""
    time.sleep(8)
    try:
        import qq_tts as _T
        cfg = _T.voice_cfg()
        if not cfg.get("gsv_autostart"):
            return
        st = _T.gsv_status(cfg)
        if st.get("running"):
            return
        _svc_out("GSV_AUTOSTART launching engine…")
        r = _T.gsv_start(cfg)
        _svc_out("GSV_AUTOSTART %s"
                 % ("ok" if r.get("ok") else "fail: %s" % r.get("error", "")))
    except Exception as e:
        try:
            _svc_out("GSV_AUTOSTART error %r" % e)
        except Exception:
            pass


def _auto_update_check():
    """启动后后台检查一次新版本（只提示，不安装）。"""
    time.sleep(20)
    try:
        cfg = C.load_app_config()
        if not cfg.get("update_check", True):
            return
        import qq_update as U
        info = U.check(cfg)
        if info.get("ok"):
            with _update_lock:
                _update_cache.clear()
                _update_cache.update(info)
            if info.get("newer"):
                _wd_event("info", "发现新版本 %s（「关于」页可一键更新）" % info.get("latest"))
    except Exception:
        pass


def _watchdog_loop():
    while True:
        try:
            cfg = C.load_app_config()
            interval = int(cfg.get("watchdog_interval") or 120)
            interval = max(60, min(600, interval))
        except Exception:
            interval = 120
        time.sleep(interval)
        try:
            _wd_check_once()
        except Exception:
            pass


PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>ZCode QQ机器人</title>
<style>
:root{
--bg:#fafafa;--side:#f5f5f5;--card:#ffffff;--fg:#404040;--dim:#737373;--line:#0a0a0a1a;--line2:#0a0a0a33;
--btn:#0a0a0a;--btn-hover:#262626;--btn-fg:#fafafa;
--nav-on:#e5e5e5;--fg-strong:#0a0a0a;
--acc:#0ea5e9;--acc-strong:#0284c7;--acc-soft:#f0f9ff;--acc-border:#7dd3fc;
--ok:#16a34a;--ok-bg:#f0fdf4;--err:#dc2626;--err-bg:#fef2f2;
--warn:#a16207;--input:#ffffff;--input-focused:#fafafa;--tint:#f5f5f5;--shadow:none;--border:#0a0a0a1a;
}
[data-theme="dark"]{
--bg:#171717;--side:#0a0a0a;--card:#262626;--fg:#e5e5e5;--dim:#a3a3a3;--line:#fafafa1a;--line2:#fafafa4d;
--btn:#fafafa;--btn-hover:#e5e5e5;--btn-fg:#0a0a0a;
--nav-on:#3f3f3f;--fg-strong:#fafafa;
--acc:#38bdf8;--acc-strong:#38bdf8;--acc-soft:#0c2a3d;--acc-border:#155e75;
--ok:#4ade80;--ok-bg:#052e16;--err:#f87171;--err-bg:#450a0a;
--warn:#facc15;--input:#262626;--input-focused:#171717;--tint:#2f2f2f;--shadow:none;--border:#fafafa1a;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI","Microsoft YaHei",system-ui,sans-serif;font-size:14px;-webkit-font-smoothing:antialiased}
#app{display:flex;min-height:100vh}
#side{width:212px;flex:none;background:var(--side);border-right:1px solid var(--line);padding:20px 14px;display:flex;flex-direction:column;gap:6px;position:sticky;top:0;height:100vh;overflow-y:auto}
.brand{display:flex;gap:10px;align-items:center;padding:2px 8px 16px}
.brand-mark{width:30px;height:30px;flex:none;border-radius:8px;background:var(--btn);display:flex;align-items:center;justify-content:center}
.brand-mark::after{content:"Z/";color:var(--btn-fg);font-weight:800;font-size:13px;line-height:1;letter-spacing:-.75px}
.brand b{font-size:14.5px;letter-spacing:.2px;display:block}
.brand .k{font-size:11px;margin-top:1px}
#nav{display:flex;flex-direction:column;gap:2px}
#nav button{display:flex;align-items:center;gap:9px;background:none;color:var(--dim);border:0;padding:9px 12px;border-radius:8px;cursor:pointer;font-size:13.5px;text-align:left;font-family:inherit;transition:color .15s,background .15s}
#nav button:hover{color:var(--fg);background:var(--tint)}
#nav button.on{color:var(--fg-strong);background:var(--nav-on);font-weight:600}
#nav button::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.45;flex:none}
#nav button.on::before{opacity:1}
.side-foot{margin-top:auto;padding:8px;display:flex;flex-direction:column;gap:8px}
#themebtn{display:flex;align-items:center;justify-content:center;gap:7px;background:none;border:1px solid var(--line2);color:var(--dim);border-radius:8px;padding:7px 10px;cursor:pointer;font-size:12.5px;font-family:inherit}
#themebtn:hover{color:var(--fg);border-color:var(--dim)}
#themebtn svg{width:14px;height:14px;flex:none}
#main{flex:1;min-width:0;padding:26px 34px 48px;max-width:1100px}
h2{font-size:19px;font-weight:600;margin-bottom:4px;letter-spacing:.2px}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:14px;box-shadow:var(--shadow)}
.card .k{margin-bottom:4px}
.kv{display:flex;gap:22px;flex-wrap:wrap;font-size:13px}
.kv>div{min-width:150px}
.k{color:var(--dim);font-size:12.5px}
.badge{padding:2.5px 10px;border-radius:20px;font-size:12px;display:inline-block}
.b-ok{background:var(--ok-bg);color:var(--ok)}.b-bad{background:var(--err-bg);color:var(--err)}.b-warn{background:var(--warn-bg,var(--tint));color:var(--warn)}
textarea{width:100%;min-height:260px;background:var(--input);color:var(--fg);border:1px solid var(--line2);border-radius:8px;padding:10px;font-family:Consolas,monospace;font-size:13px;resize:vertical}
textarea:focus,input:focus,select:focus{outline:2px solid var(--line2);border-color:var(--fg-strong)}
input,select{background:var(--input);color:var(--fg);border:1px solid var(--line2);border-radius:7px;padding:6px 10px;font-family:inherit;font-size:13.5px}
input:focus{background:var(--input-focused)}
/* checkbox 自绘（appearance:none）：box-sizing:border-box 全局下显式 content-box +
   清掉通用 input 的 padding 继承，保证任何上下文里都是精确正方形 */
input[type=checkbox]{appearance:none;-webkit-appearance:none;box-sizing:content-box;width:16px;height:16px;padding:0;border:1.5px solid #cbd5e1;border-radius:4px;background:var(--input);vertical-align:-3px;cursor:pointer;position:relative;flex:none;margin:0;outline:none}
input[type=checkbox]:hover{border-color:var(--btn)}
input[type=checkbox]:checked{background:var(--btn);border-color:var(--btn)}
input[type=checkbox]:checked::after{content:"";position:absolute;inset:0;margin:auto;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
input[type=checkbox]:disabled{opacity:.5;cursor:not-allowed}
input[type=radio]{accent-color:var(--btn);width:15px;height:15px;vertical-align:-2px}
input[type=range]{accent-color:var(--btn);padding:0;border:0;background:none}
button.act{background:var(--btn);color:var(--btn-fg);border:0;padding:8px 18px;border-radius:7px;cursor:pointer;margin-top:8px;font-family:inherit;font-size:13.5px;transition:background .15s}
button.act:hover{background:var(--btn-hover)}
button.act:not(.ghost):active{transform:translateY(1px)}
button.ghost{background:none;border:1px solid var(--line2);color:var(--fg)}
button.ghost:hover{background:var(--tint);background-color:var(--tint)}
button.ghost.danger{color:var(--err)}
label{color:var(--fg);display:inline-flex;align-items:center;gap:6px}
.chain{display:flex;flex-direction:column;gap:6px;max-width:680px}
.chain .row{display:flex;gap:8px;align-items:center;background:var(--tint);border:1px solid transparent;padding:7px 12px;border-radius:8px}
.chain .row .no{color:var(--dim);width:22px;font-size:12.5px}
.chain .row.cool{opacity:.45}
/* 行内按钮/状态不被长摘要压缩；摘要超长用省略号，避免「编辑」竖排 */
.chain .row button.act{flex:0 0 auto;white-space:nowrap;margin-top:0}
.chain .row .mono{white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:normal;font-size:12.5px}
tr:last-child td{border-bottom:0}
/* 记忆表：固定列宽防长文本撑爆，内容列强制换行（宽度按最宽真实内容校准） */
table.memtbl{table-layout:fixed}
table.memtbl th:nth-child(1){width:112px}
table.memtbl th:nth-child(2){width:52px}
table.memtbl th:nth-child(3){width:112px}
table.memtbl th:nth-child(5){width:144px}
table.memtbl td:nth-child(4){word-break:break-word;white-space:pre-wrap}
table.memtbl td:nth-child(5){white-space:nowrap}
/* 画像表：列宽与流水表不同（昵称窄、画像正文宽），独立 class 防错配 */
table.proftbl{table-layout:fixed}
table.proftbl th:nth-child(1){width:110px}
table.proftbl th:nth-child(2){width:112px}
table.proftbl th:nth-child(4){width:110px}
table.proftbl th:nth-child(5){width:144px}
table.proftbl td:nth-child(3){word-break:break-word}
table.proftbl td:nth-child(4),table.proftbl td:nth-child(5){white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#toast{position:fixed;right:20px;bottom:20px;background:var(--card);border:1px solid var(--ok);color:var(--fg);padding:11px 18px;border-radius:10px;display:none;box-shadow:var(--shadow);font-size:13.5px;z-index:9999}
/* toast 必须盖过 .dlg-mask(200)/#splash(300)：弹窗里校验失败的提示曾被
   压在模糊层下面看不见（用户实测）——与 #taskbox 同层 9999 */
.mono{font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
.tabular-nums{font-variant-numeric:tabular-nums}
.rmgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}
.rm{border:1px solid var(--line2)!important;border-radius:10px!important;padding:13px 15px!important;cursor:pointer;transition:border-color .15s,background .15s;margin-bottom:0!important}
.rm:hover{background:var(--tint)!important}
.rm.on{border-color:var(--fg-strong)!important;background:var(--tint)!important}
.rm b{font-size:13.5px}
.rm.on b{color:var(--fg-strong)}
.master{display:flex;align-items:flex-start;gap:10px}
.master input[type=checkbox]{margin-top:2px}
/* 未保存修改提示：输入框蓝色描边；保存按钮无修改时置灰 */
input.dirty,textarea.dirty{border-color:var(--acc)!important;box-shadow:0 0 0 1px var(--acc)}
button.act:disabled{opacity:.45;cursor:default}
button.act:disabled:hover{background:var(--btn)}
.hint-save{color:var(--dim);font-size:12px;margin:-10px 0 14px;display:flex;gap:6px;align-items:center}
.hint-save::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--ok);flex:none}
/* 启动闪屏（ZCode 风格品牌动画；watchdog 刷新不重播） */
#splash{position:fixed;inset:0;background:var(--bg);z-index:300;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;transition:opacity .35s}
#splash.off{opacity:0;pointer-events:none}
#splash .mark{width:76px;height:76px;border-radius:17px;background:var(--btn);color:var(--btn-fg);display:flex;align-items:center;justify-content:center;font-size:36px;font-weight:700;font-family:inherit;animation:spIn .5s cubic-bezier(.2,.9,.3,1.25)}
#splash .nm{color:var(--fg);font-size:15px;letter-spacing:.5px;animation:spFade .45s .18s both}
#splash .bar{width:180px;height:3px;background:var(--tint);border-radius:2px;overflow:hidden;animation:spFade .45s .25s both}
#splash .bar i{display:block;height:100%;background:var(--btn);width:0;animation:spBar .75s .2s ease-out forwards}
@keyframes spIn{from{opacity:0;transform:scale(.55)}to{opacity:1;transform:scale(1)}}
@keyframes spFade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes spBar{to{width:100%}}
/* 自制弹窗（替代浏览器原生 confirm/prompt，双主题自适应） */
.dlg-mask{position:fixed;inset:0;background:color-mix(in srgb,var(--fg) 22%,transparent);backdrop-filter:blur(2px);z-index:200;display:flex;align-items:center;justify-content:center;padding:24px}
.dlg{background:var(--card);border:1px solid var(--line2);border-radius:12px;width:100%;max-width:400px;padding:20px 20px 16px;animation:dlgIn .16s ease}
@keyframes dlgIn{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}
.dlg h3{margin:0 0 8px;font-size:15px;font-weight:600;color:var(--fg-strong,var(--fg))}
.dlg .dlg-body{color:var(--dim);font-size:13px;line-height:1.6;white-space:pre-line;word-break:break-word}
.dlg input{width:100%;margin-top:12px}
.dlg .dlg-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}
.dlg .dlg-btns .act{margin:0}
button.act.dlg-danger{background:var(--err);color:#fff}
[data-theme="dark"] button.act.dlg-danger{color:#450a0a}
button.act.dlg-danger:hover{background:var(--err);opacity:.88}
/* 白名单行内分群模式：wrap 含主行 + 参数子行 */
.gm-wrap{display:flex;flex-direction:column}
.gm-sub{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 10px 34px;color:var(--dim);font-size:12.5px}
.dimmed{opacity:.45;pointer-events:none}
select.mini,input.mini{padding:4px 8px;font-size:12.5px}
@media (max-width:860px){#app{flex-direction:column}#side{width:100%;height:auto;position:static;flex-direction:row;align-items:center;overflow-x:auto;padding:10px 12px}#nav{flex-direction:row;flex-wrap:nowrap}#nav button{white-space:nowrap}.brand{padding:0 10px 0 4px}.side-foot{margin:0 0 0 auto}#main{padding:18px 16px 40px}.rmgrid{grid-template-columns:1fr}}
</style></head><body>
<div id="app">
<aside id="side">
 <div class="brand"><span class="brand-mark"></span><div><b>ZCode QQ机器人</b><span class="k" id="headline">控制台</span></div></div>
 <nav id="nav">
  <button data-t="status" class="on">状态</button>
  <button data-t="runlog">运行日志</button>
  <button data-t="models">模型管理</button>
  <button data-t="groups">群消息</button>
  <button data-t="private">私聊消息</button>
  <button data-t="voice">语音消息</button>
  <button data-t="persona">人设</button>
  <button data-t="style">风格卡</button>
  <button data-t="memory">记忆</button>
  <button data-t="stickers">表情包</button>
 <button data-t="devctrl">设备操控</button>
  <button data-t="mcp">MCP 服务</button>
  <button data-t="zcode">ZCode 联动</button>
  <button data-t="plugins">插件</button>
  <button data-t="settings">设置</button>
  <button data-t="about">关于</button>
 </nav>
 <div class="side-foot">
  <button id="themebtn" onclick="cycleTheme()" title="切换外观：自动跟随系统 / 浅色 / 深色"><span id="themeico"></span><span id="themetxt">自动</span></button>
 </div>
</aside>
<main id="main">
<div id="view"></div>
</main>
</div>
<div id="toast"></div>
<div id="splash"><div class="mark">Z/</div><div class="nm">ZCode QQ机器人</div><div class="bar"><i></i></div><div class="k" id="splash-step" style="font-size:12.5px;letter-spacing:.3px;min-height:18px"></div></div>
<script>
const $=s=>document.querySelector(s);
let TAB='status';
const TITLES={status:'状态',runlog:'运行日志',models:'模型管理',groups:'群消息',private:'私聊消息',voice:'语音消息',persona:'人设',style:'风格卡',memory:'记忆',stickers:'表情包',devctrl:'设备操控',mcp:'MCP 服务',zcode:'ZCode 联动',plugins:'插件',settings:'设置',about:'关于'};
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function head(sub){return '<h2>'+TITLES[TAB]+'</h2>'+(sub?'<div class="sub">'+sub+'</div>':'')}
function goTab(t){const b=document.querySelector('#nav button[data-t="'+t+'"]');if(b)b.click()}
function skeleton(){$('#view').innerHTML='<h2>'+TITLES[TAB]+'</h2><div class="card" style="color:var(--dim);padding:26px 20px">加载中…</div>'}
function toast(m,ok=true){const t=$('#toast');t.textContent=m;t.style.display='block';t.style.borderColor=ok?'var(--ok)':'var(--err)';setTimeout(()=>t.style.display='none',2600)}
/* ---- 自制弹窗：dlgConfirm/dlgPrompt 返回 Promise（取消=false/null，ESC/点遮罩=取消） ---- */
function _dlgBuild(title,body,withInput,okText,danger){
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" aria-modal="true"><h3></h3><div class="dlg-body"></div>'
  +(withInput?'<input>':'')
  +'<div class="dlg-btns"><button class="act ghost">取消</button>'
  +'<button class="act'+(danger?' dlg-danger':'')+'">'+(okText||'确定')+'</button></div></div>';
 m.querySelector('h3').textContent=title||'';
 m.querySelector('.dlg-body').innerHTML=body||'';
 document.body.appendChild(m);return m}
function _dlgWire(m,onOk,onCancel){
 const cancel=m.querySelector('.dlg-btns .ghost'),ok=m.querySelector('.dlg-btns .act:not(.ghost)');
 const close=()=>m.remove();
 cancel.onclick=()=>{close();onCancel()};
 ok.onclick=()=>{close();onOk()};
 m.onkeydown=e=>{if(e.key==='Escape'){close();onCancel()}};
 m.onclick=e=>{if(e.target===m){close();onCancel()}};
 ok.focus()}
function dlgConfirm(o){return new Promise(res=>{const m=_dlgBuild(o.title,o.body,false,o.okText,o.danger);_dlgWire(m,()=>res(true),()=>res(false))})}
function dlgPrompt(o){return new Promise(res=>{
 const m=_dlgBuild(o.title,o.body,true,o.okText,false);
 const inp=m.querySelector('input');inp.placeholder=o.placeholder||'';if(o.value!=null)inp.value=o.value;
 const okBtn=m.querySelector('.dlg-btns .act:not(.ghost)');
 _dlgWire(m,()=>res(inp.value.trim()),()=>res(null));
 inp.onkeydown=e=>{if(e.key==='Enter'){m.remove();res(inp.value.trim())}};
 setTimeout(()=>{inp.focus();if(o.value)inp.select()},30)})}
function dlgText(o){return new Promise(res=>{
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" aria-modal="true"><h3></h3><div class="dlg-body"></div>'
  +'<textarea rows="4" style="width:100%;margin-top:12px;resize:vertical"></textarea>'
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act">'+(o.okText||'保存')+'</button></div></div>';
 m.querySelector('h3').textContent=o.title||'';
 m.querySelector('.dlg-body').textContent=o.body||'';
 const ta=m.querySelector('textarea');if(o.value!=null)ta.value=o.value;
 ta.maxLength=o.maxlen||300;
 document.body.appendChild(m);
 _dlgWire(m,()=>res(ta.value.trim()),()=>res(null));
 setTimeout(()=>{ta.focus()},30)})}
const UI_TOKEN=(function(){ // 服务端注入（?tk= 有效时）；否则回退 localStorage（手动解锁）
 try{if('__UITOKEN__'.length>20)return '__UITOKEN__'}catch(e){}
 return localStorage.getItem('zbtk')||''})();
function uiShowUnlock(){
 document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh"><div class="card" style="max-width:420px"><h3>控制台已锁定</h3><div class="k" style="margin-top:8px">如果你是用<b>桌面程序</b>打开的：这是显示异常，点下面按钮粘贴令牌即可解锁（会记住），也可以直接关闭窗口重新打开程序（会自动解锁）。<br><br>如果是从浏览器打开的：需要访问令牌，文件位置 <span class="mono">C:\\Users\\你的用户名\\.zcode-qq-bot\\ui_token</span>（用记事本打开，整行复制粘贴到下面）。</div><div style="margin-top:10px"><button class="act" onclick="uiUnlockWith(prompt(\'粘贴令牌（整行复制即可，末尾换行没关系；桌面程序也可点取消后重开程序）：\',\'\'))">粘贴令牌解锁</button></div></div></div></div>';}
async function api(p,opts){
 if(!UI_TOKEN){uiShowUnlock();throw new Error('locked')}
 opts=opts||{};opts.headers=Object.assign({},opts.headers||{},{'X-UI-Token':UI_TOKEN});
 const r=await fetch(p,opts);return r.json()}
/* ---- 开屏预热 + 页面错误上报 ----
   _PW：开屏动画期间真加载的数据（url→json），首个消费者取走即删——
   进应用后对应页面不再重复请求（本地插件列表/语音元信息这类原本每次
   进页都要等的数据）。页面 JS 崩溃/闪屏卡住会 POST /api/client-error
   落到服务日志，卡加载类问题不再靠猜。 */
window._PW={};
function pwTake(url){const v=window._PW[url];if(v!==undefined)delete window._PW[url];return v}
async function pwApi(url,fb){
 const hit=pwTake(url);
 if(hit!==undefined&&hit!==null)return hit;
 try{return await api(url)}catch(e){return fb}}
function clErr(kind,detail){
 try{
  const t=UI_TOKEN||localStorage.getItem('zbtk')||new URLSearchParams(location.search).get('tk')||'';
  fetch('/api/client-error',{method:'POST',headers:{'Content-Type':'application/json','X-UI-Token':t},
   body:JSON.stringify({kind:String(kind||'?'),detail:String(detail||'').slice(0,500)})}).catch(()=>{});
 }catch(e){}}
window.addEventListener('error',function(ev){
 clErr('js_error',(ev.message||'')+' @'+((ev.filename||'').split('/').pop()||'')+':'+(ev.lineno||0));
});
async function uiUnlockWith(t){ // 桌面窗口兜底：带令牌重载并记住
 if(!t)return;
 localStorage.setItem('zbtk',t);location.href='/?tk='+encodeURIComponent(t)}
/* ---- 统一保存交互 ----
   规则：开关/选项/模式卡/列表增删 = 点击即保存即生效；
   输入框 = 改完（回车/失焦）自动保存；长文本 = 点所在卡片的「保存」按钮，仅保存该卡片。 */
async function saveKV(p,body,okMsg){try{const r=await api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 if(r.ok===false){toast('保存失败: '+(r.error||''),false);return r}
 toast(okMsg||'已保存（已生效）');return r}catch(e){toast('保存失败：网络错误',false);return{ok:false}}}
function saveKVLoad(p,body,okMsg){saveKV(p,body,okMsg).then(r=>{if(r.ok!==false)load()})}
function saveHint(text){return '<div class="hint-save">'+esc(text)+'</div>'}
const _SAVE_HINT='本页设置均为即时生效：开关/选项点击即保存；输入框改完按回车或点别处自动保存；带「保存」按钮的卡片只保存该卡片内容。';
function dirtyOn(el,btnId){el.classList.add('dirty');const b=document.getElementById(btnId);if(b)b.disabled=false}
function dirtyOff(btnId){const b=document.getElementById(btnId);if(b)b.disabled=true;document.querySelectorAll('textarea.dirty,input.dirty').forEach(x=>x.classList.remove('dirty'))}
async function dirtyGuard(){const d=document.querySelector('textarea.dirty,input.dirty');if(!d)return true;
 return await dlgConfirm({title:'有未保存的修改',body:'当前有未保存的修改（蓝色边框标记），离开将丢失。确定离开？',okText:'放弃修改',danger:true})}
async function load(){try{if(TAB==='status')await vStatus();else if(TAB==='runlog')await vRunlog();else if(TAB==='models')await vModels();else if(TAB==='groups')await vGroups();else if(TAB==='private')await vPrivate();else if(TAB==='voice')await vVoice();else if(TAB==='persona')await vPersonas();else if(TAB==='style')await vFile('style');else if(TAB==='memory')await vMemory();else if(TAB==='stickers')await vStickers();else if(TAB==='devctrl')await vDevCtrl();else if(TAB==='mcp')await vMcp();else if(TAB==='zcode')await vZcode();else if(TAB==='plugins')await vPlugins();else if(TAB==='settings'){await vSettings();cacheReload();}else if(TAB==='about')await vAbout();}catch(e){$('#view').innerHTML='<div class="card">加载失败: '+esc(e)+'</div>'}}
document.querySelectorAll('#nav button').forEach(b=>b.onclick=async()=>{if(!await dirtyGuard())return;document.querySelectorAll('#nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');TAB=b.dataset.t;if(TAB!=='runlog'&&_runlogTimer){clearInterval(_runlogTimer);_runlogTimer=null}if(TAB!=='devctrl'&&_devCodeTimer){clearInterval(_devCodeTimer);_devCodeTimer=null}if(TAB!=='status'&&_ncTokTimer){clearInterval(_ncTokTimer);_ncTokTimer=null}if(TAB!=='voice'&&typeof _gsvPollTimer!=='undefined'&&_gsvPollTimer){clearInterval(_gsvPollTimer);_gsvPollTimer=null}skeleton();load()});
const _urlTab=new URLSearchParams(location.search).get('tab');
if(_urlTab&&TITLES[_urlTab]){TAB=_urlTab;document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on',x.dataset.t===_urlTab));}
/* 启动闪屏：每次新窗口首播，UI 自愈刷新（reload）不重播。
   动画期间真加载各页数据（_PW 预热，进页即用不再等）+ 底部显示当前步骤；
   任何一步挂死 10 秒强制放行并上报，绝不挡在门口。 */
(function(){
 const s=document.getElementById('splash');if(!s)return;
 if(sessionStorage.getItem('splashed')){s.remove();return}
 sessionStorage.setItem('splashed','1');
 const label=document.getElementById('splash-step');
 const bar=document.querySelector('#splash .bar i');
 if(bar){bar.style.animation='none';bar.style.width='6%'}
 function setBar(p){if(bar)bar.style.width=Math.round(4+p*96)+'%'}
 function step(t){if(label)label.textContent=t}
 let entered=false;
 function enter(){
  if(entered)return;entered=true;
  step('就绪');
  s.classList.add('off');setTimeout(()=>s.remove(),380);
 }
 const t0=Date.now();
 const tasks=[
  ['/api/power/status','正在连接机器人服务…',3000],
  ['/api/plugins','正在加载插件列表…',6000],
  ['/api/voice/meta','正在准备语音设置…',6000],
  ['/api/plugins/market','正在预热插件市场…',5000],
 ];
 function withTimeout(p,ms){
  return Promise.race([p,new Promise((_,rej)=>setTimeout(()=>rej(new Error('timeout')),ms))]);
 }
 (async()=>{
  for(let i=0;i<tasks.length;i++){
   const [url,txt,ms]=tasks[i];
   step(txt);
   try{window._PW[url]=await withTimeout(api(url).catch(()=>null),ms)}
   catch(e){window._PW[url]=null}
   setBar((i+1)/tasks.length);
  }
  clErr('splash_ok','预热完成 '+((Date.now()-t0)/1000).toFixed(1)+'s');
  enter();
 })();
 setTimeout(()=>{
  if(!entered){
   clErr('splash_stuck','闪屏 10 秒未完成强制放行');
   enter();
  }
 },10000);
})();
const _sysDark=window.matchMedia('(prefers-color-scheme: dark)');
const _themeIco={auto:'&#9681;',light:'&#9728;',dark:'&#9789;'};
function applyThemePref(pref){
 const t=pref==='auto'?(_sysDark.matches?'dark':'light'):pref;
 document.documentElement.setAttribute('data-theme',t);
 localStorage.setItem('themePref',pref);
 $('#themeico').innerHTML=_themeIco[pref]||_themeIco.auto;
 $('#themetxt').textContent=({auto:'自动',light:'浅色',dark:'深色'})[pref];
}
function cycleTheme(){const cur=localStorage.getItem('themePref')||'auto';const next={auto:'light',light:'dark',dark:'auto'}[cur];applyThemePref(next)}
_sysDark.addEventListener('change',()=>{if((localStorage.getItem('themePref')||'auto')==='auto')applyThemePref('auto')});
const _urlTheme=new URLSearchParams(location.search).get('theme');
applyThemePref((_urlTheme==='dark'||_urlTheme==='light')?_urlTheme:(localStorage.getItem('themePref')||'auto'));
let _bootTimer=null;
function bootCardHtml(b){
 if(!b||(!b.running&&!b.steps.length))return '';
 const rows=(b.steps||[]).slice(-40).map(s=>{
  const t=new Date((s.ts||0)*1000).toTimeString().slice(0,8);
  const dot=s.ok===true?'<span style="color:var(--ok)">✓</span>':(s.ok===false?'<span style="color:var(--err)">✗</span>':'<span style="color:var(--fg-strong)">…</span>');
  const comp=({napcat:'NapCat',daemon:'daemon',agent:'agent'})[s.component]||s.component||'';
  return '<div style="display:flex;gap:8px;padding:3px 0;font-size:12.5px;align-items:baseline">'
   +'<span class="mono" style="flex:none">'+t+'</span><span style="flex:none;width:16px">'+dot+'</span>'
   +'<span style="flex:none;min-width:52px;font-weight:600">'+comp+'</span>'
   +'<span style="color:var(--fg)">'+esc(s.message||'')+'</span></div>';
 }).join('');
 const badge=b.running?'<span class="badge" style="background:var(--tint);color:var(--fg-strong)">进行中</span>'
  :(b.failed?'<span class="badge b-bad">有失败项</span>':'<span class="badge b-ok">已完成</span>');
 // 状态页反映「当前」状态：若此刻组件已全部在线（WS 已连、daemon/agent 运行），
 // 上次启动留下的"登录等待失败"不再亮红——那是已被后续扫码解决的历史过程。
 const allOnlineNow=(typeof window._lastStatus==='object'&&window._lastStatus&&window._lastStatus.wsNow===true&&window._lastStatus.daemonOk===true&&window._lastStatus.agentOk===true);
 const effBadge=(b.failed&&!b.running&&allOnlineNow)
  ?'<span class="badge b-ok">已恢复（当前全部在线）</span>':badge;
 const effFailed=b.failed&&!b.running&&allOnlineNow?false:b.failed;
 return '<div class="card" id="bootcard"><div class="k" style="display:flex;gap:10px;align-items:center;margin-bottom:6px">'
  +'启动任务 '+effBadge+(b.final?'<span class="k"'+(effFailed?' style="color:var(--err)"':'')+'>'+esc(b.final)+'</span>':'')
  +(b.running?'':' <button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="$(\'#bootcard\').remove()">收起</button>')
  +'</div>'+(rows||'<div class="k">等待第一步…</div>')+'</div>';
}
async function refreshBootCard(){
 try{
  const b=await pwApi('/api/power/status',{});
  const el=document.getElementById('bootcard');
  if(el){
   el.outerHTML=bootCardHtml(b);
  }
  if(!b.running){
   if(_bootTimer){clearInterval(_bootTimer);_bootTimer=null}
   if(TAB==='status')load();
  }
 }catch(e){if(_bootTimer){clearInterval(_bootTimer);_bootTimer=null}}
}
function manageBootTimer(b){
 if(b&&b.running){
  if(!_bootTimer)_bootTimer=setInterval(refreshBootCard,2000);
 }else if(_bootTimer){clearInterval(_bootTimer);_bootTimer=null}
}
async function vStatus(){
 const [d,a,b,c,wd,vi,qc]=await Promise.all([
  api('/api/daemon-status').catch(()=>({ok:false})),
  api('/api/agent-status').catch(()=>({ok:false})),
  pwApi('/api/power/status',{}).catch(()=>({})),
  api('/api/app-config').catch(()=>({})),
  api('/api/watchdog').catch(()=>({enabled:false})),
  api('/api/app-info').catch(()=>({})),
  api('/api/qq-compat').catch(()=>({level:'ok'})),
 ]);
 manageBootTimer(b);
 const ws=d.ws_connected, login=d.login||{};
 // 供启动任务卡判断"当前是否已全部在线"（历史登录等待失败不再亮红）
 window._lastStatus={wsNow:ws===true,daemonOk:d.ok===true,agentOk:(a&&a.ok&&a.ready!==false)||false};
 const booting=!!(b&&b.running);
 const wdEvents=(wd.events||[]).slice(0,5).map(e=>'<div class="k mono" style="margin-top:3px">'+new Date(e.ts*1000).toLocaleTimeString()+' ['+esc(e.kind)+'] '+esc(e.detail)+'</div>').join('');
 const wdOk=wd.enabled&&!(wd.strikes>0);
 const wdCard=wd.enabled?
  '<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:6px">NapCat 看门狗 '
  +'<span class="badge '+(wdOk?'b-ok':'b-bad')+'">'+(wdOk?'正常':'告警中 ×'+wd.strikes)+'</span>'
  +'<span class="badge" style="background:var(--tint);color:var(--dim)">'+(wd.mode==='alert'?'仅告警':'自动恢复')+' · 每 '+(wd.interval||120)+'s</span></div>'
  +(wd.last_check?'<div class="k">最近检查：'+new Date(wd.last_check*1000).toLocaleTimeString()+' · '+(wd.last_result||'正常')+'</div>':'<div class="k">等待首次检查…</div>')
  +(wdEvents?'<div style="margin-top:6px"><div class="k">最近事件：</div>'+wdEvents+'</div>':'')
  +'</div>':'';
 const kl=(await api('/api/keys').catch(()=>({keys:[]}))).keys||[];
 const hasKey=kl.length>0;
 const ck=(ok,t,tip)=>'<div style="display:flex;gap:8px;align-items:flex-start;margin:4px 0">'
  +'<span class="badge '+(ok?'b-ok':'b-bad')+'" style="flex:none;margin-top:1px">'+(ok?'✓':'!')+'</span>'
  +'<span><b>'+t+'</b>'+(tip?'<div class="k">'+tip+'</div>':'')+'</span></div>';
 const qqntOk=!!(c.detected_qqnt||String(c.qqnt_dir||'').trim());
 const upInfo=(vi&&vi.cached_update)||null;
 const upBanner=(upInfo&&upInfo.ok&&upInfo.newer)?
  '<div class="card" style="border:1px solid var(--acc);display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
  +'<span class="badge b-ok">有新版</span><span><b>'+esc(upInfo.latest||'')+'</b>'
  +'<span class="k">（当前 '+esc(upInfo.current||'')+'）</span></span>'
  +'<button class="act ghost" style="margin:0" onclick="goTab(\'about\')">去「关于」页一键更新</button></div>':'';
 const wizard=(!String(c.bot_qq||'').trim()) ? (
  '<div class="card" style="border:1px solid var(--acc)">'
  +'<div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">欢迎使用 ZCode QQ机器人'
  +'<span class="badge" style="background:var(--tint);color:var(--dim)">新手引导 · 跟着做 10 分钟跑起来</span></div>'
  +'<div class="k" style="margin-bottom:4px;font-weight:600">第 0 步 · 环境自检（✓=就绪，!=缺什么补什么）</div>'
  +ck(qqntOk,'已安装 QQ（QQNT 桌面版）', qqntOk?'':'去 im.qq.com 下载安装普通版 QQ（不要 TIM/轻量版），装完回到本页刷新')
  +ck(!!c.napcat_ok,'QQ 里已装 NapCat Shell（机器人的消息通道）', c.napcat_ok?'':'最简单：「设置 → 运行环境 → 在线下载并安装 NapCat」一键完成（国内自动走镜像）。或自己下 zip（github.com/NapNeko/NapCatQQ/releases）后用「本地 zip 安装」。装完刷新本页')
  +ck(hasKey,'已配置 AI 大脑的 API Key', hasKey?'':'二选一：智谱 open.bigmodel.cn（免费额度、生图/联网/默认推荐）或 DeepSeek platform.deepseek.com（便宜、国产直连）。注册后建一个 Key，稍后在「设置 → API Key」粘贴（供应商选对应家）')
  +ck(!!c.zcode_installed,'（可选）ZCode 已安装', c.zcode_installed?'装了更强：任务收件箱 / MCP 工具 / 进程监控 / 复盘审阅（机器人本体不依赖它，随时可后补）：在 ZCode 里把本项目的 plugins/ 目录添加为插件市场（或等项目公开后直接在市场搜「QQ机器人」一键装），再到「ZCode 联动」页开启':'非必须，机器人所有聊天功能都不需要它。装了可以联动（任务收件箱 / MCP 工具等）：① zcode.ai 下载安装 ZCode；② 把本项目的 plugins/ 目录添加为 ZCode 插件市场来安装本插件（项目公开后可直接在市场搜「QQ机器人」）。装好后回本页刷新')
  +'<div class="k" style="margin:12px 0 4px;font-weight:600">第 1 步 · 告诉机器人它是谁</div>'
  +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
  +'<div><div class="k" style="margin-bottom:4px">机器人 QQ 号（建议用小号，扫码登录的就是它）</div><input id="wzbot" style="width:100%" placeholder="如 123456789"></div>'
  +'<div><div class="k" style="margin-bottom:4px">你的 QQ 号（主人：独占画画/操控电脑/遥控等高级功能）</div><input id="wzowner" style="width:100%" placeholder="如 987654321"></div>'
  +'</div>'
  +'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px">QQ 安装目录（留空=自动探测'+(c.detected_qqnt?('，已探测到：'+esc(c.detected_qqnt)):'，探测不到再手填')+'）</div>'
  +'<input id="wzqqnt" style="width:100%" placeholder="留空自动探测"></div>'
  +'<div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button class="act" onclick="wizardSave()">保存</button>'
  +'<span class="k">保存后，若上面还有「!」项，先按提示补齐，再做第 2 步</span></div>'
  +'<div class="k" style="margin:12px 0 4px;font-weight:600">第 2 步 · 填 API Key（智谱 或 DeepSeek）</div>'
  +'<div class="k">到「设置 → ③ 接口与模型消耗 → API Key」：粘贴 Key 点添加（智谱 Key 供应商留「智谱」，DeepSeek Key 选「DeepSeek」，模型链已预置两家默认模型）。智谱有免费额度够日常聊天；DeepSeek 便宜量足。也支持 Kimi / OpenAI / Grok / Gemini / 自定义（详见「设置 → ③ 接口与模型消耗」）。</div>'
  +'<div class="k" style="margin:12px 0 4px;font-weight:600">第 3 步 · 启动并扫码</div>'
  +'<div class="k">回本页点「启动机器人」→ 会自动弹出 NapCat 登录页 → 用<b>机器人那个 QQ 号</b>扫码。登录页若要求填「登录密钥 / Token」：回本页「NapCat 登录密钥」卡片点<b>「打开登录页（免密钥）」</b>直接进（密钥自动从 NapCat 配置读取，也能一键复制）。登录成功后，把机器人拉进群或私聊它，@它说句话试试。第一条回复到了，就全部完成了🎉</div>'
  +'<div class="k" style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px">常见卡点：①登录页没弹出→看本页启动日志；②登录页要密钥→用「打开登录页（免密钥）」；③扫码后 WebUI 又要求登录→NapCat 换新密钥了，回「NapCat 登录密钥」卡片点「刷新」；④机器人不回→确认群里没被「暂停回应」、Key 有效。更多说明见 README（github.com/Pigeon2333/zcode-qq-bot）</div></div>'
  ) : '';
 const keyWarn=(String(c.bot_qq||'').trim()&&!hasKey)?
  '<div class="card" style="border:1px solid var(--warn)"><b>还差最后一步：AI 大脑还没有 API Key</b>'
  +'<div class="k" style="margin-top:4px">机器人现在能连上 QQ，但不会说话（agent 会一直在线等待，不会算启动失败）。'
  +'填好后立即生效、无需重启：免费注册 <a href="https://open.bigmodel.cn" target="_blank">open.bigmodel.cn</a> → 创建 API Key → 到「设置 → ③ 接口与模型消耗 → API Key」粘贴（供应商选「智谱」），或用 DeepSeek Key（供应商选 DeepSeek）；也支持 Kimi/OpenAI 等其他供应商。</div>'
  +'<div style="margin-top:8px"><button class="act" onclick="goTab(\'settings\')">去填 Key</button></div></div>':'';
 $('#view').innerHTML=head('组件运行状态与机器人开关')+
 `${wizard}${keyWarn}${upBanner}
 <div class="card"><div class="kv">
  <div><span class="k">消息管道</span> <span class="badge ${d.ok?'b-ok':'b-bad'}">${d.ok?'运行':'停止'}</span></div>
  <div><span class="k">NapCat WS</span> <span class="badge ${ws?'b-ok':'b-bad'}">${ws?'已连接':'断开'}</span></div>
  <div><span class="k">自治大脑</span> ${a&&a.ok?(a.ready===false?'<span class="badge b-warn">等待 API Key</span>':'<span class="badge b-ok">运行</span>'):'<span class="badge b-bad">停止</span>'}</div>
  <div><span class="k">机器人</span> ${esc(login.nickname||'?')} <span class="mono">(${esc(String(login.user_id||'?'))})</span></div>
 </div>
 <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
  <button class="act" onclick="powerOn()" ${booting?'disabled style="opacity:.55"':''}>${booting?'启动中…（进度见下方日志）':'启动机器人（幂等防重复）'}</button>
  <button class="act ghost" onclick="powerOff(false)">停止大脑与管道（保留登录）</button>
  <button class="act ghost danger" onclick="powerOff(true)">全部停止（含 NapCat）</button>
 </div></div>
 <div class="card"><div class="k" style="font-weight:600">NapCat 登录密钥（WebUI Token）
  <span class="badge" style="margin-left:8px;background:var(--tint);color:var(--dim)">登录页要填的就是它</span></div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
   <span id="nctoken" class="mono" style="font-size:15px;letter-spacing:1px">读取中…</span>
   <button class="act ghost" style="margin:0" onclick="ncCopy()">复制密钥</button>
   <button class="act" style="margin:0" onclick="ncOpenLogin()">打开登录页（免密钥）</button>
   <button class="act ghost" style="margin:0" onclick="ncFill()">刷新</button>
  </div>
  <div id="nchint" class="k" style="margin-top:6px">NapCat 启动后会自动生成密钥，这里直接读取；扫码登录成功后 NapCat 会更换一次密钥，点「刷新」即可</div>
 </div>
 ${bootCardHtml(b)}
 <div class="card"><div class="kv">
  <div><span class="k">当前模型</span> <b>${esc(a.current_model||'—')}</b></div>
  <div><span class="k">已处理</span> ${a.processed??'-'}</div>
  <div><span class="k">出错</span> ${a.errors??'-'}</div>
  <div><span class="k">暂停</span> ${a.paused?'是':'否'}</div>
 </div>
 <div style="margin-top:12px"><button class="act ghost" onclick="togglePause()">${a.paused?'恢复回复':'暂停回复'}</button>
 <button class="act ghost" onclick="load()">刷新</button></div>
 <div class="k" style="margin-top:8px">手动暂停 = 立即停止一切回复与主动行为，零 token 消耗（不是安静模式那种只停主动插话、@仍回——是彻底停止；恢复后不补发暂停期间的消息）</div>
 <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line)">
  <label style="display:inline-flex;align-items:center;gap:6px"><input type="checkbox" ${c.sched_pause_enabled?'checked':''} onchange="toggleSchedPause(this.checked)"> <b>定时暂停</b></label>
  <span style="margin-left:10px" class="${c.sched_pause_enabled?'':'dimmed'}">
   <input type="time" value="${esc(c.sched_pause_start||'23:00')}" onchange="saveSchedPause('sched_pause_start',this.value)"> –
   <input type="time" value="${esc(c.sched_pause_end||'07:00')}" onchange="saveSchedPause('sched_pause_end',this.value)">
  </span>
  <div class="k" style="margin-top:4px">时段内机器人完全停止一切活动（零消耗，同手动暂停），时段结束自动恢复。支持跨午夜（如 23:00–07:00）。恢复后不补发暂停期间消息。与安静模式/分群暂停/仅私聊模式不同：这是全局一刀切。</div>
 </div></div>
 ${(qc.level==='danger')?'<div class="card" style="border:2px solid var(--err)"><div class="k" style="display:flex;gap:8px;align-items:flex-start;color:var(--err)"><b>⚠ QQ 与 NapCat 不兼容</b></div><div class="k" style="margin-top:6px">'+esc(qc.message||'')+'</div><div style="display:flex;gap:8px;margin-top:10px"><button class="act" onclick="napcatOnline()">一键重装 NapCat（自动适配新 QQ）…</button><button class="act ghost" onclick="goTab(\'settings\')">去锁定 QQ 版本</button></div></div>':''}
 ${(qc.level==='warn')?'<div class="card" style="border:1px solid var(--warn)"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>QQ 自动更新未屏蔽</b><span style="flex:1"></span><button class="act ghost" style="margin:0" onclick="goTab(\'settings\')">去开启「锁定 QQ 版本」</button></div><div class="k" style="margin-top:4px">QQ 一旦自动升级，NapCat 可能失效、机器人起不来。建议开启锁定（可随时关）。</div></div>':''}
 ${wdCard}
 <div class="card"><div class="k">收发统计</div><div class="kv" style="margin-top:8px">
  <div>收到 ${d.stats?.received??0}</div><div>触发 ${d.stats?.triggered??0}</div>
  <div>已发 ${d.stats?.sent??0}</div><div>限流丢弃 ${d.stats?.dropped_rate??0}</div>
 </div></div>`;
 $('#headline').textContent=(d.ok&&a&&a.ok)?'运行中':'已停止';
 ncFill(true);
 if(!_ncTokTimer)_ncTokTimer=setInterval(()=>{if(TAB==='status')ncFill()},30000);
}
let _ncTok='';
let _ncTokTimer=null;
async function ncFill(quiet){
 const d=await api('/api/napcat-login-info').catch(()=>null);
 const t=document.getElementById('nctoken'),h=document.getElementById('nchint');
 if(!t||!h)return;
 if(!d){t.textContent='读取失败';h.textContent='读取 NapCat 登录密钥失败，稍后点「刷新」重试';return}
 if(_ncTok&&d.token&&d.token!==_ncTok&&!quiet){
  toast('NapCat 登录密钥已变化（新密钥已显示）');
 }
 _ncTok=d.token||'';
 t.textContent=_ncTok||'未获取';
 const parts=[];
 if(!d.webui_up)parts.push('NapCat 未运行（6099 端口没开）——先点上方「启动机器人」');
 else if(d.ws_ready)parts.push('NapCat 已登录，WS 已就绪');
 else parts.push('NapCat 已启动、账号尚未登录：点「打开登录页（免密钥）」扫码或快速登录');
 if(_ncTok)parts.push('密钥来源：'+(d.source||'NapCat 配置')+'，扫码登录成功后可能更换，届时点「刷新」');
 else if(d.webui_up)parts.push('密钥还没生成：等 NapCat 启动完点「刷新」；也可在「运行日志」里找 WebUi Token 行，或看 QQNT\\config\\webui.json');
 h.textContent=parts.join(' · ');
}
async function ncCopy(){
 if(!_ncTok){toast('还没有密钥可取（NapCat 未启动或未生成）',false);return}
 try{await navigator.clipboard.writeText(_ncTok);toast('密钥已复制，粘到 NapCat 登录页即可')}
 catch(e){toast('复制失败：请手动选中密钥复制',false)}
}
async function ncOpenLogin(){
 const r=await api('/api/napcat-open-login',{method:'POST'}).catch(()=>({ok:false,error:'请求失败'}));
 if(r&&r.ok)toast(r.tokenized?'已打开登录页（已自动带上密钥，直接扫码）':'已打开登录页，把上方密钥粘进去')
 else toast((r&&(r.error||r.message))||'打开登录页失败',false);
 if(r&&r.ok)ncFill();
}
async function powerOn(){
 const r=await api('/api/power/on',{method:'POST'}).catch(()=>({started:false,note:'请求失败'}));
 if(r.started===false){toast(r.note||'启动任务已在进行中，见状态页启动日志')}
 else{toast('启动任务已开始：每一步进度在状态页实时显示')}
 load();
}
async function wizardSave(){
 const bot=String(document.getElementById('wzbot').value||'').trim();
 const owner=String(document.getElementById('wzowner').value||'').trim();
 const qqnt=String(document.getElementById('wzqqnt').value||'').trim();
 if(!bot||!/^\d{5,12}$/.test(bot)){toast('机器人 QQ 号格式不对',false);return}
 if(owner&&!/^\d{5,12}$/.test(owner)){toast('主人 QQ 号格式不对',false);return}
 const body={bot_qq:bot};
 if(owner)body.owner_qq=owner;
 if(qqnt)body.qqnt_dir=qqnt;
 const r=await saveKV('/api/app-config',body,'初始化完成！去「设置 → API Key」填 Key，然后回这里点「启动机器人」');
 if(r.ok!==false)load()}
async function napcatInstall(){
 const p=await api('/api/pick-zip',{method:'POST'}).catch(()=>({file:''}));
 const f=(p&&p.file)||'';
 if(!f){toast('未选择文件',false);return}
 if(!await dlgConfirm({title:'安装 NapCat',body:'将把「'+f.split(/[\\/\\\\]/).pop()+'」释放到 QQNT 目录（现有 NapCat 文件先自动备份）。继续？',okText:'安装',danger:true}))return
 toast('正在安装…');
 const r=await api('/api/napcat-install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({zip:f})});
 toast(r.ok?'安装成功':'安装失败：'+(r.message||r.error||''),r.ok!==false);
 if(r.ok)load()}
let _ncdlTimer=null;
async function napcatOnline(force){
 // 单次调用：不再先探测再重发——旧版第一次调用就已启动下载，第二次
 // 撞上"任务进行中"报错退出，页面永远不显示进度（用户实测复现）
 let r=await api('/api/napcat-dl-start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.need_confirm){
  if(!await dlgConfirm({title:'已安装 NapCat',body:r.error+'<br><br>（不确定就点取消——已装好的不用动；想升级或修复安装才继续）',okText:'仍要重装',danger:true}))return;
  // 确认后重发，让后续判断与轮询基于新响应（旧响应 ok:false 是"需确认"语义不是错误）
  r=await api('/api/napcat-dl-start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:true})}).catch(()=>({ok:false,error:'请求失败'}));
 }
 if(r.ok===false){toast(r.error||'启动下载失败',false);return}
 toast(r.already?'下载已在进行中，显示当前进度':'已开始在线下载 NapCat Shell');
 ncdlPoll()}
function ncdlPoll(){
 if(_ncdlTimer)clearInterval(_ncdlTimer);
 _ncdlTimer=setInterval(async()=>{
  const st=await api('/api/napcat-dl-status').catch(()=>null);
  if(!st){return}
  const el=document.getElementById('ncdl-status');
  if(el)el.textContent=st.running
   ?('下载中 '+st.pct+'% · '+(st.status||'')+(st.elapsed?(' · '+st.elapsed+'s'):''))
   :(st.error?('✗ '+st.error):('✓ '+(st.status||'完成')));
  if(!st.running){clearInterval(_ncdlTimer);_ncdlTimer=null;if(!st.error)load()}
 },1500)}
async function powerOff(napcatToo){
 if(!await dlgConfirm(napcatToo
  ?{title:'停止 NapCat',body:'确认连 NapCat 一起停止？（下次启动需重新登录）',okText:'全部停止',danger:true}
  :{title:'停止机器人',body:'大脑与消息管道将停止，机器人不再响应任何消息（QQ 登录保留，可随时重新启动）。',okText:'停止',danger:true}))return;
 const r=await api('/api/power/off',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({include_napcat:!!napcatToo})});toast('已停止');load()}
async function toggleSchedPause(on){
 await saveKVLoad('/api/app-config',{sched_pause_enabled:on},on?'定时暂停已开启（时段内机器人完全停止）':'定时暂停已关闭')}
async function saveSchedPause(key,val){
 if(!val)return;
 await saveKVLoad('/api/app-config',{[key]:val},'定时暂停时间已更新（即时生效）')}
async function togglePause(){const a=await api('/api/agent-status');await api('/api/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:!a.paused})});toast('已'+(!a.paused?'暂停':'恢复'));load()}
let _runlogTimer=null,_runlogSrc='all';
const _lvlColor={ok:'var(--ok)',err:'var(--err)',warn:'var(--warn)',info:'var(--fg)',dim:'var(--dim)'};
const _srcName={daemon:'管道',agent:'大脑',boot:'启动',napcat:'NapCat'};
function runlogRows(d){
 return (d.lines||[]).slice(-250).map(l=>{
  const c=_lvlColor[l.level]||'var(--fg)';
  return '<div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--line);font-size:12px;align-items:baseline">'
   +'<span class="mono" style="flex:none;width:58px;color:var(--dim);font-size:11px">'+esc(l.ts||'--:--:--')+'</span>'
   +'<span style="flex:none;width:34px;color:var(--dim);font-size:11px">'+(_srcName[l.src]||l.src)+'</span>'
   +'<span style="flex:none;color:'+c+';font-weight:700">·</span>'
   +'<span class="mono" style="color:'+c+';word-break:break-all">'+esc(l.text)+'</span></div>';
 }).join('');
}
async function vRunlog(){
 const d=await api('/api/runlog?src='+_runlogSrc).catch(()=>({lines:[]}));
 const chip=(v,label)=>'<button class="badge" style="border:1px solid var(--line2);background:'+(_runlogSrc===v?'var(--nav-on)':'var(--card)')+';color:' +(_runlogSrc===v?'var(--fg-strong)':'var(--dim)')+';cursor:pointer;font-size:12px" onclick="setRunlogSrc(\''+v+'\')">'+label+'</button>';
 $('#view').innerHTML=head('机器人运行全日志：消息管道（daemon）、自治大脑（agent）、启动任务实时汇聚；NapCat 页签含 WebUi Token 与登录二维码')+
 '<div class="card"><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px">'
 +chip('all','全部')+chip('daemon','消息管道')+chip('agent','自治大脑')+chip('boot','启动任务')+chip('napcat','NapCat')
 +'<button class="act ghost" style="margin:0;margin-left:8px" onclick="logsExport()">导出诊断包…</button>'
 +'<span class="k" style="margin-left:auto">3 秒自动刷新·跟随滚动</span></div>'
 +'<div style="max-height:calc(100vh - 210px);overflow-y:auto" id="logbox">'+(runlogRows(d)||'<div class="k" style="padding:20px 0">暂无日志</div>')+'</div></div>';
 const box=$('#logbox');if(box)box.scrollTop=box.scrollHeight;
 if(!_runlogTimer)_runlogTimer=setInterval(refreshRunlog,3000);
}
async function logsExport(){
 const d=await api('/api/pick-dir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'选择日志导出位置'})}).catch(()=>({dir:''}));
 const dir=(d&&d.dir)||'';
 if(!dir){toast('已取消（未选导出位置）',false);return}
 toast('正在打包…');
 const r=await api('/api/logs-export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dir})}).catch(()=>({ok:false}));
 if(r.ok===false){toast('导出失败',false);return}
 toast('已导出：'+r.path+'（日志+配置，密钥已掩码；可直接发给开发者排查）');
}
async function refreshRunlog(){
 if(TAB!=='runlog'){if(_runlogTimer){clearInterval(_runlogTimer);_runlogTimer=null}return}
 try{
  const d=await api('/api/runlog?src='+_runlogSrc);
  const box=$('#logbox');if(!box)return;
  const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-40;
  box.innerHTML=runlogRows(d)||'<div class="k" style="padding:20px 0">暂无日志</div>';
  if(stick)box.scrollTop=box.scrollHeight;
 }catch(e){}
}
function setRunlogSrc(v){_runlogSrc=v;vRunlog()}
async function vModels(){
 const [c,a,u,tl,rv,kl]=await Promise.all([
  api('/api/config').catch(()=>({models:[],thinking:{}})),
  api('/api/agent-status').catch(()=>({ok:false})),
  api('/api/usage').catch(()=>({})),
  api('/api/token-limit').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/keys').catch(()=>({keys:[]})),
 ]);
 const keysL=kl.keys||[];
 const th=c.thinking||{};
 const dft=th._default||{enabled:true,level:'high'};
 const thKey=(m)=>{const e=th[m];if(!e)return '';return e.enabled?e.level:'off'};
 const thOpt=(v)=>{const map={'':'跟随默认','off':'关闭思考','high':'思考·高','low':'思考·低'};
  return Object.keys(map).map(k=>'<option value="'+k+'"'+(v===k?' selected':'')+'>'+map[k]+'</option>').join('')};
 const entries=(c.models||[]).map(x=>{const base=typeof x==='string'?{model:x,key:'',ctx:'',maxout:''}:{model:String(x.model||''),key:String(x.key||''),ctx:x.ctx||'',maxout:x.max_out||''};
  base.think=thKey(base.model);return base});
 window._mEntries=entries;window._mKeys=keysL;window._mThink=th;
 const keyLabel=e=>{if(!e.key)return '主Key';const k=keysL.find(x=>x.id===e.key);return k?(k.primary?'★':'')+esc(k.label||k.id):'（Key已删）'};
 const thName={'':'默认','off':'不思考','high':'思考·高','low':'思考·低'};
 const rows=entries.map((e,i)=>{const v=thKey(e.model);const ck=(e.key||'main')+':'+e.model;
  const cd=a.cooldowns&&a.cooldowns[ck]>0;const pinned=a.pinned===ck;
  return `<div class="row ${cd&&!pinned?'cool':''}"><span class="no">${i+1}.</span>
  <b style="flex:0 0 auto;max-width:46%;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.model)}</b>
  <span class="k mono" style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">${esc(keyLabel(e))}${e.ctx?' · '+esc(e.ctx)+'k':''}${e.maxout?' · out '+esc(e.maxout):''} · ${thName[v]||'默认'}</span>
  <span class="mono" style="flex:0 0 auto;color:var(--ok,#16a34a)">${pinned?'已锁定':(cd?'冷却'+a.cooldowns[ck]+'s':(a.current_model===e.model?'当前':''))}</span>
  <button class="act ghost" onclick="pinModel(${i},${pinned?1:0})">${pinned?'取消锁定':'立即使用'}</button>
  <button class="act ghost" onclick="mv(${i},-1)">↑</button><button class="act ghost" onclick="mv(${i},1)">↓</button>
  <button class="act ghost" onclick="editModel(${i})">编辑</button>
  <button class="act ghost" onclick="del(${i})">✕</button></div>`}).join('');
 const stat=(label,v,calls)=>`<div class="card" style="flex:1;min-width:150px"><div class="k">${label}</div>
  <div style="font-size:22px;font-weight:500;margin-top:4px" class="tabular-nums">${(v||0).toLocaleString()} <span style="font-size:12px;color:var(--dim)">tokens</span></div>
  <div class="k mono" style="margin-top:2px">${calls||0} 次调用</div></div>`;
 const mrow=(u.by_model||[]).map(x=>`<tr><td>${esc(x.model)}</td><td class="tabular-nums">${x.calls}</td>
  <td class="tabular-nums">${x.prompt.toLocaleString()}</td><td class="tabular-nums">${x.completion.toLocaleString()}</td>
  <td class="tabular-nums"><b>${x.total.toLocaleString()}</b></td><td class="mono">${esc(x.last)}</td></tr>`).join('');
 $('#view').innerHTML=head('模型降级链、思考模式与 token 用量统计')+saveHint(_SAVE_HINT)+
 ` <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
  ${stat('总量',u.total_tokens,u.total_calls)}${stat('近30天',u.d30_tokens,u.d30_calls)}
  ${stat('近7天',u.d7_tokens,u.d7_calls)}${stat('今日',u.today_tokens,u.today_calls)}
 </div>
 <div class="card"><div class="k">调用趋势
  <span style="margin-left:8px">
   <button class="act ghost" id="tr_d30" onclick="setTrendRange('d30')">近30天</button>
   <button class="act ghost" id="tr_d7" onclick="setTrendRange('d7')">近7天</button>
   <button class="act ghost" id="tr_today" onclick="setTrendRange('today')">当日</button>
  </span></div>
  <div id="trend" style="margin-top:8px"></div></div>
 <div class="card"><div class="k">按模型用量（输入/输出/总量，单位 token）</div>
 <table style="margin-top:8px"><tr><th>模型</th><th>调用</th><th>输入</th><th>输出</th><th>总量</th><th>最近使用</th></tr>
 ${mrow||'<tr><td colspan=6 class="k">暂无调用记录</td></tr>'}</table></div>
 <div class="card"><div class="k">视觉模型覆盖（一般留空即可：主模型链的 glm-5.3-flash 等新模型原生支持看图；仅当需要指定专用视觉模型如 glm-4.5v 时填写；改完回车或点别处自动保存）</div>
  <div style="display:flex;gap:8px;margin-top:8px"><input id="vmodel" value="${esc(c.vision_model||'')}" style="flex:1;max-width:360px" placeholder="留空=用模型链直接看图" onchange="saveVisionModel()"></div></div>
 <div class="card"><div class="k">思考模式（开思考回答更深入，但 token 消耗显著增加；开思考时自动放大回复长度上限，防止思考把答案挤掉；改动即时生效）</div>
  <div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap;align-items:center">
   <label>默认思考：
    <select id="thdef" class="mini" onchange="saveThinking()">
     <option value="high" ${dft.enabled&&dft.level==='high'?'selected':''}>开启·最高等级</option>
     <option value="low" ${dft.enabled&&dft.level==='low'?'selected':''}>开启·低等级</option>
     <option value="off" ${!dft.enabled?'selected':''}>关闭</option>
    </select></label>
   <label class="master" style="margin-top:8px"><input type="checkbox" id="thauto" ${th._auto?'checked':''} onchange="saveThinking()">
    <span><b>自动思考</b><div class="k" style="margin-top:2px">按消息难度自动决定：简单寒暄不思考秒回；长文/推理/技术难题开最高思考；开启后忽略逐模型设置</div></span></label>
  </div>
  <div class="k" style="margin-top:6px">逐模型覆盖：点模型行「编辑」在弹窗中单独设置（默认=跟随上面的默认思考）。</div></div>
 <div class="card"><div class="k">模型降级链：从上往下逐个尝试，某个模型限流/失效会自动冷却并切到下一个。</div>
 <div class="k" style="margin-top:4px">点「编辑」改模型名 / API Key / 上下文档位 / 思考模式。「立即使用」= 无视冷却强制切到该模型（自动切换切错了、或模型已恢复但还在冷却时用）；它再次失败或被移出链会自动解锁。</div>
 <div class="chain" style="margin-top:10px">${rows||'<i class="k">空</i>'}</div>
 <div style="margin-top:10px;display:flex;gap:8px"><button class="act" onclick="editModel(-1)">＋ 添加模型</button></div></div>
 <div class="card"><div class="k" style="display:flex;gap:10px;align-items:center;margin-bottom:8px">限额管理
  <span id="tlimit-badge"></span></div>
  <div class="k">防止机器人用量过大挤占同套餐其他业务：窗口内 token 消耗达到上限后，机器人停止一切响应（主人私聊遥控指令除外），窗口滚动后自动恢复</div>
  <div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap;align-items:center">
   <label><input type="checkbox" ${rv.token_limit_enabled?'checked':''} onchange="saveLimit('token_limit_enabled',this.checked?1:0)"> 启用 token 限额</label>
   <label>每 <input id="tlhours" type="number" min="1" max="24" value="${rv.token_limit_hours||1}" style="width:56px" onchange="saveLimitHours()"> 小时最多消耗</label>
   <input id="tlmax" type="number" min="1000" step="1000" value="${rv.token_limit_max||100000}" style="width:110px" onchange="saveLimitMax()"> tokens
   <span class="k" id="tlimit-usage"></span>
  </div>
  <div class="k" style="margin-top:8px">参考（本机实测）：普通群聊回复约 0.15万–0.3万 tokens/条（含上下文与人设）；长文/开思考约 0.5万–1万/条；看图另加约 0.1万–0.2万。例如每小时 30 万 ≈ 100–200 条普通回复。窗口 1–24 小时可调。</div></div>`;
 _usageCache=u;renderTrend();
 if(tl&&tl.ok!==false)renderLimitUsage(tl);
}
function getEntries(){return window._mEntries||[]}
function getChain(){return chainOf(getEntries())}
async function saveModels(){await saveKV('/api/config',{models:getChain()},'模型链已保存（agent 已热加载）')}
async function saveThinking(){
 const th={};
 const dv=$('#thdef')?$('#thdef').value:'high';
 th._default=dv==='off'?{enabled:false,level:'high'}:{enabled:true,level:dv};
 if($('#thauto'))th._auto=$('#thauto').checked;
 (window._mEntries||[]).forEach(e=>{
  if(e.think)th[e.model]=e.think==='off'?{enabled:false,level:'high'}:{enabled:true,level:e.think};
 });
 await saveKV('/api/config',{thinking:th},'思考设置已保存（已生效）')}
async function saveVisionModel(){await saveKV('/api/config',{vision_model:$('#vmodel').value.trim()},'视觉模型已保存（即时生效）')}
async function saveLeanCtx(on){await saveKV('/api/reply-config',{lean_ctx_enabled:on},on?'精简上下文已开启（平均省 30%~45% 消耗）':'已切换全量上下文（消耗约 +50%~80%，表现最佳）')}
async function pickBackupDir(){
 const r=await api('/api/pick-dir',{method:'POST'});
 if(r.ok&&r.dir){$('#bkdir').value=r.dir;await saveNight('backup_dir',r.dir)}}
async function backupNow(){
 toast('正在执行备份…');
 const r=await api('/api/backup-now',{method:'POST'});
 toast(r.ok?'备份完成（目录见运行日志 AGENT_BACKUP_OK）':'备份失败',r.ok!==false)}
// ---- 备份范围勾选（重要类取消弹强提醒）----
const BK_GROUPS=[
 {group:'persona',label:'人设与风格',critical:true,desc:'人设合集（含风格卡）+分对象路由+通用风格卡+自进化观察——机器人的性格本体，重建成本最高'},
 {group:'memory',label:'长期记忆',critical:true,desc:'对群友/好友的记忆——纯积累型数据，丢了无法再生'},
 {group:'config',label:'行为与开关配置',critical:false,desc:'回复模式/白名单/模型链/应用设置——丢了可手动重配'},
 {group:'voice',label:'语音音色',critical:false,desc:'克隆音色（索引+音频本体）+微调模型索引——微调模型文件太大只备索引'},
 {group:'plugins',label:'插件与导入数据',critical:false,desc:'插件启停与设置、各插件数据（热点历史/风格学习样本）、聊天导入库——丢了插件数据清零'},
 {group:'misc',label:'表情/成员画像/日报',critical:false,desc:'表情索引（图片本体不备）/成员画像/每日复盘日报/定时任务——丢了影响小或可再生'}];
async function bkGroupsRender(){
 const box=document.getElementById('bk-groups');
 if(!box)return;
 const a=await api('/api/app-config').catch(()=>({}));
 const excl=a.backup_excluded||[];
 box.innerHTML=BK_GROUPS.map(g=>'<label class="master" style="margin:4px 0"><input type="checkbox" data-bg="'+g.group+'" '+(excl.indexOf(g.group)<0?'checked':'')+' onchange="bkGroupToggle(this,\''+g.group+'\','+(g.critical?'true':'false')+')">'
  +' <span><b>'+g.label+'</b>'+(g.critical?'<span class="badge" style="background:#fff4e0;color:#b25e09;margin-left:6px">重要</span>':'')
  +'<div class="k" style="margin-top:2px">'+g.desc+'</div></span></label>').join('');
}
async function bkGroupToggle(el,group,critical){
 const a=await api('/api/app-config').catch(()=>({backup_excluded:[]}));
 let excl=a.backup_excluded||[];
 if(!el.checked){
  if(critical){
   const g=BK_GROUPS.find(x=>x.group===group);
   if(!await dlgConfirm({title:'⚠ 确认不备份「'+g.label+'」？',body:g.desc+'。\n\n这类数据是'+(group==='persona'?'机器人的性格本体（人设+风格长期积累）':'纯积累型记忆，无法再生')+'——一旦损坏/误删将无法恢复，强烈建议保持备份。\n\n确定要排除它吗？',okText:'仍然不备份',danger:true})) {el.checked=true;return}
  }
  if(excl.indexOf(group)<0)excl.push(group);
 }else{
  excl=excl.filter(x=>x!==group);
 }
 await saveKV('/api/app-config',{backup_excluded:excl},el.checked?'已恢复备份「'+BK_GROUPS.find(x=>x.group===group).label+'」':'已排除「'+BK_GROUPS.find(x=>x.group===group).label+'」——今后备份不再包含它');
}
async function restorePicker(){
 const d=await api('/api/backups').catch(()=>({backups:[]}));
 const list=d.backups||[];
 if(!list.length){toast('备份目录中没有可用的备份',false);return}
 const m=document.createElement('div');m.className='dlg-mask';
 const rows=list.map((b,i)=>'<label style="display:block;margin:6px 0"><input type="radio" name="rbsel" value="'+esc(b.name)+'"'+(i===0?' checked':'')+'> '+esc(b.name)+'（'+b.files+' 个文件 · '+new Date(b.mtime*1000).toLocaleString()+'）</label>').join('');
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:420px;max-height:80vh;overflow-y:auto"><h3>从备份恢复</h3>'
  +'<div class="dlg-body">选择一个备份，恢复会<b style="color:var(--err)">覆盖当前全部人设/记忆/配置/表情包索引等数据</b>（密钥不受影响）。目录：'+esc(d.root||'')+'</div>'
  +'<div style="margin-top:10px">'+rows+'</div>'
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act dlg-danger">恢复此备份</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=ev=>{if(ev.target===m)close()};
 m.onkeydown=ev=>{if(ev.key==='Escape')close()};
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  const sel=(m.querySelector('input[name=rbsel]:checked')||{}).value;
  close();
  if(!sel)return;
  if(!await dlgConfirm({title:'确认恢复备份',body:'将用备份「'+sel+'」覆盖当前全部数据（人设、记忆、配置、表情包索引等）。此操作不可撤销，确定继续？',okText:'覆盖并恢复',danger:true}))return;
  toast('正在恢复…');
  const r=await api('/api/backup-restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:sel})});
  if(r.ok===false){toast('恢复失败: '+(r.error||''),false);return}
  toast('已恢复 '+r.count+' 个文件（配置已即时生效；建议重启 agent 确认）');
  load()}}
async function toggleDevCtrl(on){
 const box=document.getElementById('devon');
 if(on&&!await dlgConfirm({title:'开启设备操控',body:'⚠ 这是一项高危功能：开启后主人可以从 QQ 远程搜索、读取、发送你电脑上的文件。' + String.fromCharCode(10) + String.fromCharCode(10) + '已内置保护：仅主人可用、结果只发私聊、系统目录+你的保护目录不可触达、默认需动态授权码。' + String.fromCharCode(10) + String.fromCharCode(10) + '确定开启？',okText:'确认开启（我已了解风险）',danger:true})){
  if(box)box.checked=false;  // 取消：回滚勾选状态
  return;
 }
 const r=await saveKV('/api/app-config',{device_ctrl_enabled:on},on?'设备操控已开启':'设备操控已关闭');
 if(r.ok===false){if(box)box.checked=!on;return}  // 保存失败：回滚
 load()}

async function saveNight(key,val){
 const msg={review_time:'复盘时间',backup_enabled:'自动备份',backup_time:'备份时间',backup_dir:'备份目录'}[key]+'已更新（次日生效）';
 const r=await saveKV('/api/app-config',{[key]:key==='backup_dir'?String(val).trim():val},msg);
 if(r.ok!==false)load()}
let _trendRange='d30',_usageCache=null;
function setTrendRange(r){_trendRange=r;if(_usageCache){renderTrend()}}
function renderTrend(){
 const u=_usageCache;if(!u)return;
 let series;
 if(_trendRange==='today'){series=(u.hourly||[]).map(x=>({label:x.label,tokens:x.tokens}));}
 else{const n=_trendRange==='d7'?7:30;series=(u.daily||[]).slice(-n).map(x=>({label:x.date.slice(5),tokens:x.tokens}));}
 drawTrend(series);
 ['d30','d7','today'].forEach(id=>{const b=document.getElementById('tr_'+id);if(b)b.style.borderColor=(id===_trendRange)?'var(--acc)':'transparent'});
}
function drawTrend(series){
 const el=document.getElementById('trend');if(!el||!series.length)return;
 const W=el.clientWidth||900,H=170,P=30;
 const vals=series.map(d=>d.tokens);const max=Math.max(10,...vals);
 const step=(W-P*2)/Math.max(1,series.length-1);
 const pts=vals.map((v,i)=>[P+i*step,H-24-(v/max)*(H-52)]);
 const path=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 const area=path+' L '+(P+(series.length-1)*step).toFixed(1)+' '+(H-24)+' L '+P+' '+(H-24)+' Z';
 const grid=[0,0.5,1].map(f=>'<line x1="'+P+'" y1="'+(H-24-f*(H-52)).toFixed(1)+'" x2="'+(W-P)+'" y2="'+(H-24-f*(H-52)).toFixed(1)+'" stroke="var(--border)" stroke-width="1"/>').join('');
 const labels=[0,0.5,1].map(f=>'<text x="'+(P-6)+'" y="'+(H-20-f*(H-52)).toFixed(1)+'" fill="var(--dim)" font-size="10" text-anchor="end">'+Math.round(max*f).toLocaleString()+'</text>').join('');
 const xi=[0,Math.floor(series.length/2),series.length-1].filter((v,i,a)=>a.indexOf(v)===i);
 const xticks=xi.map(i=>'<text x="'+(P+i*step).toFixed(1)+'" y="'+(H-8)+'" fill="var(--dim)" font-size="10" text-anchor="middle">'+(series[i]?series[i].label:'')+'</text>').join('');
 const dots=pts.map(p=>'<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="2.5" fill="var(--acc)"/>').join('');
 el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:'+H+'px" role="img" aria-label="token调用趋势">'+grid+labels+xticks+
  '<path d="'+area+'" fill="var(--acc)" opacity="0.12"/><path d="'+path+'" fill="none" stroke="var(--acc)" stroke-width="2"/>'+dots+'</svg>';
}
function mv(i,d){const e=window._mEntries||[];const j=i+d;if(j<0||j>=e.length)return;[e[i],e[j]]=[e[j],e[i]];postChainReorder()}
async function postChainReorder(){await saveKVLoad('/api/config',{models:chainOf(window._mEntries||[])},'模型链已更新（agent 已热加载）')}
async function del(i){const e=window._mEntries||[];const m=e[i]&&e[i].model;
 if(!await dlgConfirm({title:'移除模型',body:'从降级链移除「'+(m||'')+'」？（链中剩余模型仍正常工作，可随时重新添加）',okText:'移除',danger:true}))return;
 e.splice(i,1);postChainReorder()}
async function pinModel(i,un){
 const e=(window._mEntries||[])[i];if(!e)return;
 const r=await api('/api/pin-model',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(un?{clear:true}:{kid:e.key||'main',model:e.model})}).catch(()=>null);
 if(!r||r.ok===false){toast((r&&r.error)||'操作失败（agent 未运行？）',false);return}
 toast(un?'已取消锁定，恢复自动切换':'已切换：下一条消息起使用 '+e.model+'（无视冷却）');
 load()}
function chainOf(entries){return entries.map(e=>(e.key||e.ctx||e.maxout)?{model:e.model,...(e.key?{key:e.key}:{}),...(e.ctx?{ctx:e.ctx}:{}),...(e.maxout?{max_out:e.maxout}:{})}:e.model)}
async function editModel(i){
 const creating=i<0;const e=creating?{model:'',key:'',ctx:'',think:'',maxout:''}:Object.assign({},window._mEntries[i]);
 const keysL=window._mKeys||[];
 const keyOpts='<option value=""'+(!e.key?' selected':'')+'>主 Key（未指定条目默认用）</option>'
  +keysL.map(k=>`<option value="${esc(k.id)}"${e.key===k.id?' selected':''}>${k.primary?'★ ':''}${esc(k.label||k.id)}（${esc(k.provider_label||'未设置供应商')}）</option>`).join('');
 const thOpts=['','off','high','low'].map(v=>'<option value="'+v+'"'+((e.think||'')===v?' selected':'')+'>'+({'' :'跟随默认','off':'关闭思考','high':'思考·高','low':'思考·低'})[v]+'</option>').join('');
 const row=(label,inner,hint)=>'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px">'+label+'</div>'+inner+(hint?'<div class="k" style="margin-top:3px">'+hint+'</div>':'')+'</div>';
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:440px;max-height:86vh;overflow-y:auto"><h3>'+(creating?'添加模型':'编辑模型')+'</h3>'
  +row('模型名','<input id="em_model" value="'+esc(e.model)+'" placeholder="如 glm-5.3-flash" style="width:100%">')
  +row('API Key','<select id="em_key" class="mini" style="width:100%">'+keyOpts+'</select>','该条目用哪个 Key 调用（模型名需与 Key 的供应商匹配；生图/联网固定用智谱 Key）')
  +row('上下文档位（千 token）','<input id="em_ctx" type="number" min="4" max="128" value="'+(e.ctx||'')+'" placeholder="按模型默认（flash 系 128）" style="width:100%">','控制注入的对话历史与记忆规模；人设/风格/安全规则永不裁剪')
  +row('思考模式','<select id="em_th" class="mini" style="width:100%">'+thOpts+'</select>')
  +row('最大输出（tokens）','<input id="em_maxout" type="number" min="600" max="32768" step="256" value="'+(e.maxout||'')+'" placeholder="默认（按思考档 600–4096）" style="width:100%">','单次回复的输出上限（建议 600 以上；设太小会截断回复、丢失回复末尾的记忆标记）')
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act">'+(creating?'添加':'保存')+'</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=ev=>{if(ev.target===m)close()};
 m.onkeydown=ev=>{if(ev.key==='Escape')close()};
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  const model=document.getElementById('em_model').value.trim().toLowerCase();
  const key=document.getElementById('em_key').value;
  const ctx=parseInt(document.getElementById('em_ctx').value)||0;
  const th=document.getElementById('em_th').value;
  const mo=parseInt(document.getElementById('em_maxout').value)||0;
  if(!model){toast('模型名不能为空',false);return}
  close();
  const ent={model,key,ctx:ctx?Math.max(4,Math.min(128,ctx)):'',think:th,maxout:mo?Math.max(256,Math.min(32768,mo)):''};
  if(creating){(window._mEntries=window._mEntries||[]).push(ent)}
  else{window._mEntries[i]=ent}
  const think=Object.assign({},window._mThink||{});
  if(th)think[model]=th==='off'?{enabled:false,level:'high'}:{enabled:true,level:th};
  else delete think[model];
  window._mThink=think;
  const r=await saveKV('/api/config',{models:chainOf(window._mEntries),thinking:think},(creating?'已添加 ':'已保存 ')+model+'（agent 已热加载）');
  load()}}
async function vGroups(){
 const [g,c,gm,rv,gnr]=await Promise.all([
  api('/api/groups').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/group-modes').catch(()=>({modes:{}})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/group-names').catch(()=>({names:{}})),
 ]);
 _gmModes=gm.modes||{};_gmGlobal=c;rvState=rv;
 const gn=(gnr&&gnr.names)||{};
 const gname=x=>gn[String(x)]||'';
 const _ovs=Object.entries(_gmModes||{}).filter(([k,v])=>v&&
  ('proactive' in v||'proactive_probability' in v||'proactive_min_interval_sec' in v));
 const _ovLine=_ovs.length?`<div class="k" style="margin-top:6px">⚠ 以下群的分群主动设置优先于全局：${
  _ovs.map(([k,v])=>esc(gname(k)||k)+(v.proactive===false?'（主动已停用）':'')).join('、')}。全局调了没反应时先看这里。</div>`:'';
 const on=g.enabled!==false, allOn=!!g.allow_all, m=c.reply_mode||'standard';
 const rows=(g.groups||[]).map((x,i)=>{
  const ov=_gmModes[x]||{};
  const cur=ov.paused?'paused':(ov.reply_mode||'');
  const opt=(v,t)=>`<option value="${v}"${cur===v?' selected':''}>${t}</option>`;
  const pcur=ov.paused?'x':(ov.proactive===true?'on':(ov.proactive===false?'off':''));
  const opt2=(v,t)=>`<option value="${v}"${pcur===v?' selected':''}>${t}</option>`;
  let params='';
  if(ov.reply_mode==='keyword'){
   params=`<div class="gm-sub">该群关键词（中英文逗号/顿号/空格分隔均可，覆盖全局；改完自动保存）：<input value="${esc((ov.keywords||_gmGlobal.keywords||[]).join(', '))}" style="width:230px" onchange="saveGmFieldFor('${esc(x)}','keywords',this)"></div>`;
  }else if(ov.reply_mode==='chat'){
   params=`<div class="gm-sub">插话概率 <input type="number" min="0" max="100" style="width:60px" value="${Math.round((ov.chat_probability??_gmGlobal.chat_probability??0.5)*100)}" onchange="saveGmFieldFor('${esc(x)}','chat_probability',this)">%
   　最小间隔 <input type="number" min="0" max="3600" style="width:68px" value="${ov.chat_min_interval_sec??_gmGlobal.chat_min_interval_sec??45}" onchange="saveGmFieldFor('${esc(x)}','chat_min_interval_sec',this)"> 秒（仅该群生效）</div>`;
  }
  if(ov.proactive===true||'proactive_probability' in ov||'proactive_min_interval_sec' in ov){
   params+=`<div class="gm-sub">该群主动：概率 <input type="number" min="0" max="100" style="width:56px" value="${Math.round((ov.proactive_probability??rv.proactive_groups_probability??0.1)*100)}" onchange="saveGmProNum('${esc(x)}','proactive_probability',(parseInt(this.value)||0)/100)">%
   　周期 每 <input type="number" min="5" max="1440" style="width:60px" value="${Math.round((ov.proactive_min_interval_sec??rv.proactive_groups_min_interval_sec??1800)/60)}" onchange="saveGmProNum('${esc(x)}','proactive_min_interval_sec',(parseInt(this.value)||30)*60)"> 分钟（仅该群；未修改时跟随全局）</div>`;
  }
  const nameLine=(x)=>'<div class="gm-sub k" style="margin-top:2px;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(gname(x))+'">🏷 '+esc(gname(x)||'群名未获取（机器人需在该群内）')+'</div>';
  return `<div class="gm-wrap"><div class="row"><span class="no">${i+1}.</span>
   <input class="gin" value="${esc(x)}" data-i="${i}" style="flex:1;min-width:110px" placeholder="群号" onchange="saveGroups()">
   <select class="mini" style="width:116px;flex:none" title="该群回复模式" onchange="rowGmMode(this)">
    ${opt('','跟随全局')}${opt('standard','标准')}${opt('keyword','关键词')}${opt('chat','闲聊')}${opt('paused','暂停回应')}
   </select>
   <select class="mini" style="width:96px;flex:none" title="该群是否参与主动发消息" onchange="rowGmPro(this)">
    ${opt2('','跟随')}${opt2('on','主动开')}${opt2('off','主动关')}
   </select>
   <button class="act ghost" onclick="delg(${i})">删除</button></div>${nameLine(x)}${params}</div>`;
 }).join('');
 const modeCard=(v,title,desc)=>`<div class="card rm ${m===v?'on':''}" onclick="pickMode('${v}')">
  <b>${title}</b>${m===v?' <span class="badge b-ok">当前</span>':''}<div class="k" style="margin-top:5px">${desc}</div></div>`;
 $('#view').innerHTML=head('回应群消息总开关、回复模式与群白名单')+saveHint(_SAVE_HINT)+
 `<div class="card"><label class="master"><input type="checkbox" id="genabled" ${on?'checked':''} onchange="saveGroupEnabled(this.checked)">
  <span><b>回应群消息</b><div class="k" style="margin-top:3px">总开关：关闭后机器人不再回应任何群消息（私聊不受影响，适合只想私聊用的场景）；改动即时生效</div></span></label></div>
 <div class="${on?'':'dimmed'}">
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center">回复模式<span class="badge" style="background:var(--tint);color:var(--dim)">全局通用默认</span>（决定机器人在群里什么时候说话，仅对群生效；每个群可在下方白名单行内单独覆盖）</div>
  <div class="rmgrid">
   ${modeCard('standard','标准模式','只有被 @ 或回复机器人时才说话')}
   ${modeCard('keyword','关键词模式','标准模式基础上，群消息包含关键词也会触发回复')}
   ${modeCard('chat','闲聊模式','群里任何聊天都可能插话：按概率抽样 + 最小间隔防刷屏')}
  </div>
  ${m==='keyword'?`<div style="margin-top:12px"><div class="k">关键词（中英文逗号/顿号/空格分隔均可，包含即触发；改完自动保存）</div>
   <input id="kwedit" style="width:100%;margin-top:6px" value="${esc((c.keywords||[]).join(', '))}" onchange="saveKw()"></div>`:''}
  ${m==='chat'?`<div style="margin-top:12px">
   <div class="k">插话概率：<b id="pval">${Math.round((c.chat_probability||0)*100)}%</b>（100%=每条都回；拖完松手即保存）</div>
   <input type="range" id="pslide" min="0" max="100" value="${Math.round((c.chat_probability||0)*100)}" style="width:100%;margin-top:6px"
    oninput="$('#pval').textContent=this.value+'%'" onchange="saveProb()">
   <div class="k" style="margin-top:10px">两次主动搭话最小间隔（秒）：<input id="mintv" type="number" min="0" max="3600" value="${c.chat_min_interval_sec??45}" class="mini" style="width:90px" onchange="saveIntv()"></div>
   <div style="margin-top:10px"><label class="master"><input type="checkbox" id="intr" ${c.chat_interesting?'checked':''} onchange="saveCk('chat_interesting',this.checked,'感兴趣判定')">
    <span><b>感兴趣才回</b><div class="k" style="margin-top:2px">AI 按人设判断这条消息值不值得接话，不感兴趣就静默；关闭则纯随机</div></span></label></div>
  <div style="margin-top:8px"><label class="master"><input type="checkbox" id="anti" ${c.chat_anti_interrupt!==false?'checked':''} onchange="saveCk('chat_anti_interrupt',this.checked,'防插嘴')">
    <span><b>防插嘴</b><div class="k" style="margin-top:2px">结合上下文判断：与自己无关的话题、他人之间正在讨论的对话、无人叫你时保持沉默；开启时覆盖上一项，判定更严格</div></span></label></div>
  </div>`:''}
 </div>
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">主动发消息<span class="badge" style="background:var(--tint);color:var(--dim)">全局通用默认</span>（每个群可在下方白名单行内单独覆盖）</div>
  <label class="master"><input type="checkbox" ${rv.proactive_groups_enabled?'checked':''} onchange="savePro('proactive_groups_enabled',this.checked)">
   <span><b>允许机器人主动在群里发消息</b><div class="k" style="margin-top:3px">开启后机器人会不定期根据最近对话、当前时间、记忆主动开个话头：只选最近 6 小时内有活动的白名单群；安静模式/暂停/群暂停回应/限额停响应时都不发；发送前过复读与脏话守卫</div></span></label>
  ${proCard('groups')}
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)"><label class="master"><input type="checkbox" ${rv.nudge_group_enabled?'checked':''} onchange="saveKVLoad('/api/reply-config',{nudge_group_enabled:this.checked},'群聊追问已'+(this.checked?'开启':'关闭'))">
   <span><b>群聊冷场追问</b><div class="k" style="margin-top:3px">机器人在群里说话没人接时，也像真人那样轻轻追一句。默认关——群里冷场很正常，追多了容易尬也容易被当刷屏。开启后也只选允许主动发消息的群（白名单内、未暂停、该群没单独关主动），每轮最多追一次，判定比私聊保守得多（多数情况选择不说话）。沉默窗口沿用私聊追问的设置</div></span></label></div>
  <div style="margin-top:8px"><label>总量上限：所有群+私聊合计每小时最多 <input type="number" min="0" max="60" value="${rv.proactive_hourly_cap??2}" style="width:52px" onchange="saveProNum('hourly_cap',(isNaN(parseInt(this.value))?2:parseInt(this.value)),'总量上限')"> 条（0=不限；各群各自计时容易叠加，这个上限封住整体频率）</label></div>
  ${_ovLine}
  <div class="k" style="margin-top:6px">例：30 分钟 + 10% ≈ 平均每 5 小时主动说一句。私聊的设置在「私聊消息」页，互不影响。</div></div>
 <div class="card">
  <label class="master" style="margin-bottom:8px"><input type="checkbox" id="allowall" ${allOn?'checked':''} onchange="saveGroups(true)">
   <span><b>响应所有群</b><div class="k" style="margin-top:3px">开启后忽略下方白名单；关闭即恢复白名单，列表保留不丢</div></span></label>
  <div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">白名单群与分群模式<span class="badge" style="background:var(--tint);color:var(--dim)">每行可覆盖全局</span><span>机器人只在列表内群响应；模式下拉=该群回复模式，主动下拉=该群是否参与主动发消息（跟随全局=用上方全局设置，暂停回应=该群一切回应停止（@也不回）但群保留在列表、随时切回；改动即时生效${allOn?'；当前被上面的开关覆盖':''}）</span></div>
  <div class="chain" style="margin-top:10px;${allOn?'opacity:.45':''}">
   <div class="row" style="background:transparent;border:0;padding:2px 12px"><span class="no"></span>
    <span class="k" style="flex:1;min-width:110px">群号</span>
    <span class="k" style="width:116px;flex:none">回复模式</span>
    <span class="k" style="width:96px;flex:none">主动发消息</span>
    <span class="k" style="width:52px;flex:none">操作</span></div>
   ${rows||'<i class="k">空（无白名单生效）</i>'}</div>
  <div style="margin-top:10px;display:flex;gap:8px"><input id="newg" placeholder="群号，如 123456789" onkeydown="if(event.key==='Enter')addg()"><button class="act" onclick="addg()">添加</button></div></div>
 </div>`;
}
async function vPrivate(){
 const [c,rv,fnr]=await Promise.all([
  api('/api/private-config').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/friend-names').catch(()=>({names:{}})),
 ]);
 const fn=(fnr&&fnr.names)||{};
 const fname=x=>fn[String(x)]||'';
 const on=c.enabled, all=c.allow_all;
 rvState=rv;
 const rows=(c.qq_whitelist||[]).map((x,i)=>`<div class="row"><span class="no">${i+1}.</span>
  <input value="${esc(x)}" data-i="${i}" style="flex:1;min-width:100px" placeholder="QQ号" onchange="savePrivate()">
  <span class="k" style="flex:none;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(fname(x))||'昵称未获取（需是机器人好友）'}">${esc(fname(x))||'<i>未获取</i>'}</span>
  <button class="act ghost" onclick="delpq(${i})">删除</button></div>`).join('');
 $('#view').innerHTML=head('私聊回应开关与私信白名单（私聊始终全部回复，无需回复模式）')+saveHint(_SAVE_HINT)+
 `<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">主动发消息<span class="badge" style="background:var(--tint);color:var(--dim)">全局通用</span>（对所有可私聊对象生效；与群聊设置互不影响）</div>
  <label class="master"><input type="checkbox" ${rv.proactive_private_enabled?'checked':''} onchange="savePro('proactive_private_enabled',this.checked)">
   <span><b>允许机器人主动私聊</b><div class="k" style="margin-top:3px">只发给私信白名单内、最近 6 小时聊过的人（与「回应私信」开关互不影响，回复所有人的私信开启时也不会主动打扰陌生人）；安静模式/暂停/限额时都不发</div></span></label>
  ${proCard('private')}
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)"><label class="master"><input type="checkbox" ${rv.nudge_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{nudge_enabled:this.checked},'主动追问已'+(this.checked?'开启':'关闭'))">
   <span><b>聊到一半的主动追问</b><div class="k" style="margin-top:2px">和对方私聊着、对方忽然不说话了——沉默一会儿后机器人会像真人那样主动追问一句（具体说什么由人设和当时的对话决定）。每轮最多追问 <input type="number" min="1" max="5" value="${rv.nudge_max_strikes??2}" style="width:44px" onchange="saveKVLoad('/api/reply-config',{nudge_max_strikes:Math.max(1,Math.min(5,parseInt(this.value)||2))},'追问次数已更新')"> 次；对方回复即重新计时。沉默 <input type="number" min="30" max="1800" value="${rv.nudge_delay_min_sec??150}" style="width:60px" onchange="saveKVLoad('/api/reply-config',{nudge_delay_min_sec:Math.max(30,Math.min(1800,parseInt(this.value)||150))},'追问延迟已更新')"> 秒后开始（实际会在一个小区间内随机，更自然）。安静模式/禁用时段时不追</div></span></label></div>
 </div> <div class="card">
  <label class="master" style="margin-bottom:8px"><input type="checkbox" id="penabled" ${on?'checked':''} onchange="savePrivate()">
   <span><b>回应私信</b><div class="k" style="margin-top:3px">默认关闭；开启后机器人会回复私聊消息</div></span></label>
  <div class="${on?'':'dimmed'}">
  <label class="master"><input type="checkbox" id="pallowall" ${all?'checked':''} onchange="savePrivate()">
   <span>回复所有人的私信<div class="k" style="margin-top:3px">关闭后仅回复下方白名单中的 QQ 号</div></span></label>
 </div></div>
 <div class="card ${((on&&!all)?'':'dimmed')}"><div class="k">私信白名单（仅回复列表内 QQ 号的私信，改动即时生效${all?'；当前被上面的"回复所有人"覆盖，列表保留不丢':''}）</div>
  <div class="chain" style="margin-top:10px">${rows||'<i class="k">空（未配置任何 QQ，将无人可私聊触发）</i>'}</div>
  <div style="margin-top:10px;display:flex;gap:8px"><input id="newpq" placeholder="QQ号，如 987654321" onkeydown="if(event.key==='Enter')addpq()"><button class="act" onclick="addpq()">添加</button></div></div>
 <div class="card"><label class="master"><input type="checkbox" id="ptyping" ${c.typing_enabled?'checked':''} onchange="saveTyping()"> <b>「正在输入…」拟真</b><div class="k" style="margin-top:3px">私聊回复前，对方聊天栏会先显示「对方正在输入…」，像真人打字一样停顿几秒再发出（仅私聊有效，QQ 群聊无此显示）；显示时长建议 2-3 秒——太长像卡了，太短没意义</div></label>
  <div class="${c.typing_enabled?'':'dimmed'}" style="display:flex;gap:8px;align-items:center;margin-top:8px">
   <span class="k">显示时长</span><input type="number" id="ptype-sec" min="1" max="10" value="${c.typing_seconds||2}" style="width:60px" onchange="saveTyping()"><span class="k">秒（1–10）</span>
   <span class="k" id="ptyping-msg"></span></div></div>`;
}
function getPQ(){return [...document.querySelectorAll('.chain input')].map(x=>x.value.trim()).filter(x=>/^\d{5,12}$/.test(x))}
async function saveTyping(){
 const body={typing_enabled:$('#ptyping').checked,
             typing_seconds:parseInt($('#ptype-sec').value)||2};
 const r=await api('/api/private-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const el=document.getElementById('ptyping-msg');
 if(el)el.textContent=r.ok?'已保存 ✓（即时生效）':'保存失败';
}
async function savePrivate(){
 const body={enabled:$('#penabled').checked,allow_all:$('#pallowall').checked,qq_whitelist:getPQ()};
 const r=await api('/api/private-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 toast(r.ok?'私聊白名单已生效（'+getPQ().length+' 个 QQ）':'保存失败: '+(r.error||''),r.ok!==false)
}
async function delpq(i){const c=getPQ();const q=c[i];c.splice(i,1);
 if(!await dlgConfirm({title:'移除私聊白名单',body:'移除 QQ '+q+'？机器人将不再回复该账号的私信（可随时重新添加）。',okText:'移除',danger:true}))return;
 postPQ(c)}
async function postPQ(c){const a=await api('/api/private-config');await api('/api/private-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:a.enabled,allow_all:a.allow_all,qq_whitelist:c})});await api('/api/friend-names?refresh=1').catch(()=>{});load()}
function addpq(){const v=$('#newpq').value.trim();if(!v)return;const c=getPQ();c.push(v);postPQ(c)}
function getGroups(){return [...document.querySelectorAll('.chain input.gin')].map(x=>x.value.trim()).filter(x=>/^\d{5,12}$/.test(x))}
async function saveGroupEnabled(on){
 const g=await api('/api/groups');
 await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on,allow_all:!!g.allow_all,groups:g.groups||[]})});
 toast(on?'已开启回应群消息（已生效）':'已关闭：机器人不再回应任何群消息（已生效，私聊不受影响）');load()}
async function saveGroups(fromToggle){
 const cur=await api('/api/groups');
 const c=getGroups();
 if(!c.length&&(cur.groups||[]).length&&!fromToggle){
  if(!await dlgConfirm({title:'清空白名单',body:'白名单即将清空（现有 '+(cur.groups||[]).length+' 个群），机器人将不再回应任何群消息。确定？',okText:'清空',danger:true}))return}
 const r=await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:cur.enabled!==false,allow_all:$('#allowall')?$('#allowall').checked:!!cur.allow_all,groups:c})});
 toast(r.ok===false?('保存失败: '+(r.error||'')):((c.length||0)+' 个群已生效'),r.ok!==false);
 if(fromToggle)load()}
async function delg(i){const c=getGroups();const gid=c[i];c.splice(i,1);
 if(!gid||!await dlgConfirm({title:'删除白名单群',body:'删除群 '+gid+'？机器人将不再响应该群消息'+(_gmModes[gid]?(String.fromCharCode(10)+'注意：该群已配置分群模式覆盖，将一并清除（重新添加后需重配）'):'')+'。',okText:'删除',danger:true}))return;
 postGroupsReorder(c);
 if(gid&&_gmModes[gid])api('/api/group-modes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:gid,override:null})})}
async function postGroupsReorder(c){const a=await api('/api/groups');await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:a.enabled!==false,allow_all:a.allow_all,groups:c})});await api('/api/group-names?refresh=1').catch(()=>{});load()}
function addg(){const v=$('#newg').value.trim();if(!v)return;const c=getGroups();c.push(v);postGroupsReorder(c)}
let _gmModes={},_gmGlobal={};
const _GM_ROW_NAME={standard:'标准模式',keyword:'关键词模式',chat:'闲聊模式',paused:'暂停回应（@也不回，随时切回）'};
async function rowGmMode(sel){
 const gid=sel.closest('.gm-wrap').querySelector('input.gin').value.trim();
 if(!/^\d{5,12}$/.test(gid)){toast('群号无效：先修正行内群号再设置模式',false);sel.value='';return}
 const v=sel.value;
 if(v===''){
  const r=await saveKV('/api/group-modes',{chat_id:gid,override:null},'群 '+gid+' 已恢复跟随全局（已生效）');
  if(r.ok!==false)load();return}
 const ov=v==='paused'?{paused:true}:{reply_mode:v};
 const r=await saveKV('/api/group-modes',{chat_id:gid,override:ov},'群 '+gid+'：'+_GM_ROW_NAME[v]+'（已生效）');
 if(r.ok!==false)load();
}
async function saveGmFieldFor(gid,f,el){
 if(!/^\d{5,12}$/.test(gid)){toast('群号无效',false);return}
 const names={keywords:'关键词',chat_probability:'插话概率',chat_min_interval_sec:'最小间隔'};
 const ov={};
 if(f==='keywords')ov.keywords=el.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
 if(f==='chat_probability')ov.chat_probability=(parseInt(el.value)||0)/100;
 if(f==='chat_min_interval_sec')ov.chat_min_interval_sec=parseInt(el.value)||0;
 const r=await saveKV('/api/group-modes',{chat_id:gid,override:ov},names[f]+'已生效（仅群 '+gid+'）');
 if(r.ok!==false)_gmModes[gid]=Object.assign({},_gmModes[gid],ov);
}
async function vPersonas(){
 const [d,pr,gnr,fnr]=await Promise.all([
  api('/api/personas'),
  api('/api/persona-routes').catch(()=>({routes:[]})),
  api('/api/group-names').catch(()=>({names:{}})),
  api('/api/friend-names').catch(()=>({names:{}})),
 ]);
 window._prNames={g:(gnr&&gnr.names)||{},f:(fnr&&fnr.names)||{}};
 const opts=(d.names||[]).map(n=>`<option value="${esc(n)}" ${n===d.active?'selected':''}>${esc(n)}</option>`).join('');
 window._prPersonaNames=d.names||[];
 $('#view').innerHTML=head('多人设管理与全局通用设定')+saveHint('长文本需点所在卡片的「保存」按钮生效（按钮只保存该卡片）；未保存的修改以蓝色边框标记，切换人设或离开本页会提示。')+
 `<div class="card"><div class="k">全局通用设定（所有人设共用：主人是谁、通用禁忌等，跨人设生效）</div>
  <textarea id="gedit" style="min-height:150px;margin-top:8px" oninput="dirtyOn(this,'bsaveg')">${esc(d.global||'')}</textarea>
  <button class="act" id="bsaveg" disabled onclick="saveGlobal()">保存全局设定</button></div>
 <div class="card"><div class="k">人设列表（当前激活：<b style="color:var(--fg-strong)">${esc(d.active||'无')}</b>，切换即时生效）</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <select id="psel">${opts}</select>
   <button class="act ghost" onclick="switchPersona()">切换到此设</button>
   <button class="act ghost" onclick="renamePersona()">重命名</button>
   <button class="act ghost" onclick="dupPersona()">创建副本</button>
   <button class="act ghost" onclick="newPersona()">新建</button>
   <button class="act ghost" onclick="personaImport()">导入…</button>
   <button class="act ghost" onclick="personaMigrate()">生成迁移指令</button>
   <button class="act ghost danger" onclick="delPersona()">删除</button>
  </div>
  <div class="k" style="margin-top:6px">「创建副本」复制一份当前人设（含它的风格卡）为「原名 副本」——调人设/风格前先建一份，改坏了随时切回来。</div>
  <div class="k" style="margin-top:10px">人设内容（<span id="pname">${esc(d.active||'')}</span>）</div>
  <textarea id="pedit" style="margin-top:6px" oninput="dirtyOn(this,'bsavep')">${esc(d.text||'')}</textarea>
  <button class="act" id="bsavep" disabled onclick="savePersona()">保存当前编辑的人设</button></div>
 <div class="card"><div class="k" style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">分对象人设<span class="badge" style="background:var(--tint);color:var(--dim)">对不同的人，展现不同的一面</span></div>
  <div class="k" style="margin-bottom:8px">为特定的私聊对象或群单独指定人设（例如：私聊主人=贴心人设，某群=高冷人设）。<b>没有列出的对象一律用上面的全局激活人设</b>；修改全局激活人设不会影响这里单独设置过的对象。<b>记忆是全机器人共用的</b>——她记得所有人说过的事，只是对不同人展现不同的一面。</div>
  <div class="chain" id="proutes">${routeRows(pr)}</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <select id="prtype"><option value="private">私聊对象（QQ 号）</option><option value="group">群（群号）</option></select>
   <input id="prid" placeholder="QQ 号 / 群号" style="width:150px">
   <select id="prpersona">${opts}</select>
   <button class="act" onclick="prAdd()">添加分对象人设</button>
  </div>
  <div class="k" style="margin-top:6px">匹配规则：私聊按对方 QQ 精确匹配；群按群号精确匹配。同一对象同时命中时私聊优先（互不冲突）。删除/重命名上方人设时，这里的设置会自动同步或清理。</div></div>`;
 const sel=$('#psel');_pSelPrev=sel.value;
 sel.onchange=async()=>{
  if($('#pedit').classList.contains('dirty')&&!await dlgConfirm({title:'未保存的修改',body:'当前人设有未保存的修改，放弃并切换？',okText:'放弃并切换',danger:true})){sel.value=_pSelPrev;return}
  _pSelPrev=sel.value;
  const r=await api('/api/personas/detail?name='+encodeURIComponent(sel.value));
  $('#pedit').value=r.text||'';$('#pname').textContent=sel.value;
  $('#pedit').classList.remove('dirty');$('#bsavep').disabled=true;
 };
}
function routeRows(d){
 const pr=(d.routes||[]);
 if(!pr.length)return '<div class="k" style="padding:6px 2px">暂无分对象设置（所有对象都用上面的全局激活人设）</div>';
 return pr.map(r=>{
  const [t,id]=String(r.key).split(':');
  const nm=t==='group'?(window._prNames.g[id]||''):(window._prNames.f[id]||'');
  const opts=(window._prPersonaNames||[]).map(n=>`<option value="${esc(n)}" ${n===r.persona?'selected':''}>${esc(n)}</option>`).join('');
  return `<div class="row"><span class="no">${t==='group'?'👥':'💬'}</span>
   <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(nm)}">${esc(nm||'（名字未获取）')} <span class="k mono">${esc(id)}</span></span>
   <select onchange="prUpdate('${esc(r.key)}',this.value)">${opts}</select>
   <button class="act ghost danger" onclick="prDel('${esc(r.key)}')">删除</button></div>`;
 }).join('');
}
async function prSave(routes,okMsg){
 const r=await saveKV('/api/persona-routes/save',{routes:routes},okMsg);
 if(r.ok!==false)load()}
async function prAdd(){
 const t=document.getElementById('prtype').value,id=document.getElementById('prid').value.trim();
 if(!/^\d{5,12}$/.test(id)){toast('请输入正确的 QQ 号 / 群号（5-12 位数字）',false);return}
 const persona=document.getElementById('prpersona').value;
 const cur=(await api('/api/persona-routes').catch(()=>({routes:[]}))).routes||[];
 if(cur.some(x=>x.key===t+':'+id)){toast('该对象已有分对象人设（可在列表里直接改）',false);return}
 prSave(cur.concat([{key:t+':'+id,persona:persona}]),'已为该对象设置专属人设（即时生效）')}
async function prUpdate(key,persona){
 const cur=(await api('/api/persona-routes').catch(()=>({routes:[]}))).routes||[];
 prSave(cur.map(x=>x.key===key?{key:key,persona:persona}:x),'该对象的人设已更新（即时生效）')}
async function prDel(key){
 if(!await dlgConfirm({title:'移除分对象人设',body:'移除后，该对象将回落使用全局激活人设。',okText:'移除'}))return;
 const cur=(await api('/api/persona-routes').catch(()=>({routes:[]}))).routes||[];
 prSave(cur.filter(x=>x.key!==key),'已移除（该对象回落全局激活人设）')}
let _pSelPrev='';
async function saveGlobal(){const r=await saveKV('/api/personas/global',{text:$('#gedit').value},'全局设定已保存（即时生效）');if(r.ok!==false)dirtyOff('bsaveg')}
async function switchPersona(){if(!await dirtyGuard())return;const n=$('#psel').value;await api('/api/personas/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});toast('已切换到「'+n+'」（即时生效）');load()}
async function newPersona(){if(!await dirtyGuard())return;const n=await dlgPrompt({title:'新建人设',body:'输入新人设名称：',placeholder:'如：智谱娘·傲娇',okText:'创建'});if(!n)return;await api('/api/personas/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,text:'# 人设：'+n+String.fromCharCode(10)+String.fromCharCode(10)+'- 身份：'+String.fromCharCode(10)+'- 性格：'+String.fromCharCode(10)+'- 说话基准：'+String.fromCharCode(10)})});toast('已创建「'+n+'」');load()}
async function delPersona(){if(!await dirtyGuard())return;const n=$('#psel').value;if(!n)return;if(!await dlgConfirm({title:'删除人设',body:'确认删除人设「'+n+'」？删除后不可恢复。',okText:'删除',danger:true}))return;const r=await api('/api/personas/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});if(r.ok===false){toast(r.error||'删除失败',false);return}toast('已删除');load()}
async function renamePersona(){
 if(!await dirtyGuard())return;
 const n=$('#psel').value;if(!n)return;
 const nn=await dlgPrompt({title:'重命名人设',body:'将「'+n+'」重命名为：',value:n,okText:'重命名'});if(!nn||nn===n)return;
 const r=await api('/api/personas/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,new_name:nn})});
 if(r.ok===false){toast(r.error||'重命名失败',false);return}
 toast('已重命名为「'+nn+'」'+((r.moved&&r.moved.length)?'（已同步：'+r.moved.join('、')+'）':''));load()}
async function savePersona(){const n=$('#psel').value;const t=$('#pedit').value;const r=await saveKV('/api/personas/save',{name:n,text:t},'人设「'+n+'」已保存（即时生效）');if(r.ok!==false)dirtyOff('bsavep')}
async function dupPersona(){
 if(!await dirtyGuard())return;
 const n=$('#psel').value;if(!n)return;
 const nn=await dlgPrompt({title:'创建人设副本',body:'将「'+n+'」复制为（默认加「副本」后缀）：',value:n+' 副本',okText:'创建副本'});
 if(!nn||nn===n)return;
 const r=await api('/api/personas/dup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,new_name:nn})});
 if(r.ok===false){toast(r.error||'创建副本失败',false);return}
 toast('已创建副本「'+nn+'」（含其风格卡）；在列表里选中即可切换使用');
 load();
}
async function personaImport(){
 if(!await dirtyGuard())return;
 const p=await api('/api/personas/pick-import',{method:'POST'}).catch(()=>({file:''}));
 const f=(p&&p.file)||'';
 if(!f){toast('未选择文件',false);return}
 const r=await api('/api/personas/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:f})});
 if(r.ok===false){toast(r.error||'导入失败',false);return}
 toast(r.message||('导入 '+r.count+' 个'));load()}
async function personaMigrate(){
 const r=await api('/api/personas/migrate-cmd').catch(()=>null);
 const cmd=(r&&r.command)||'';
 if(!cmd){toast('生成失败',false);return}
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:560px"><h3>一键迁移指令</h3>'
  +'<div class="dlg-body">把下面这段发给另一台电脑上正在运行的机器人控制台/Agent 会话，它会把那边的人设/记忆写到本机数据目录，机器人重启即完成迁移：</div>'
  +'<textarea id="migcmd" readonly style="width:100%;min-height:170px;margin-top:10px;font-family:monospace;font-size:12px">'+esc(cmd)+'</textarea>'
  +'<div class="dlg-btns"><button class="act ghost">关闭</button><button class="act">复制指令</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=ev=>{if(ev.target===m)close()};
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  const ta=document.getElementById('migcmd');ta.select();
  try{await navigator.clipboard.writeText(ta.value);toast('已复制，去发给那边的 Agent 吧')}catch(e){document.execCommand('copy');toast('已复制')}
  }}
async function vFile(kind){
 let d=await api('/api/file?kind='+kind);
 const ev=await api('/api/evolved');
 if(kind==='style'){
  const st=await api('/api/style/targets',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({targets:[]}));
  window._stTargets=(st.targets||[]);
  const sel=window._stSel||(st.active&&window._stTargets||[]).length?window._stSel:null;
  // 初始读选中目标的文本（默认当前激活人设；无则全局）
  const initial=window._stSel||st.active||'_global_';
  const tr=await api('/api/style/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:initial})}).catch(()=>({text:''}));
  d={text:tr.text||''};
  window._stSel=initial;
 }
 let evHtml='';
 if(kind==='style'){
  const mx=ev.max_notes||30;
  const inj=ev.inject_notes||12;
  const rows=(ev.personas||[]).map(p=>{
   const all=(p.notes||[]);
   const per=10, pages=Math.max(1,Math.ceil(all.length/per));
   const pg=Math.min(window['_evPg_'+p.name]||1,pages);
   const shown=all.slice((pg-1)*per,pg*per);
   const notesHtml=all.length
    ?('<div class="k" style="margin-top:8px">'+shown.map(n=>'- '+esc(n)).join('<br>')+'</div>'
      +(pages>1?'<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><button class="act ghost" style="margin:0;padding:2px 10px;font-size:12px" onclick="evPage(\''+esc(p.name)+'\','+(pg-1)+')" '+(pg<=1?'disabled':'')+'>上一页</button><span class="k">第 '+pg+'/'+pages+' 页（共 '+all.length+' 条，注入最新 '+inj+' 条）</span><button class="act ghost" style="margin:0;padding:2px 10px;font-size:12px" onclick="evPage(\''+esc(p.name)+'\','+(pg+1)+')" '+(pg>=pages?'disabled':'')+'>下一页</button></div>':''))
    :'<div class="k" style="margin-top:6px">（尚无积累：机器人每 6 次回复会自动从群聊中提炼 1-2 条风格观察）</div>';
   const clearBtn=all.length
    ?'<button class="act ghost" style="color:var(--err)" data-pname="'+esc(p.name)+'" onclick="clearEvolved(this.dataset.pname)">清空</button>'
    :'';
   return '<div class="card">'
    +'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
    +'<b>'+esc(p.name)+'</b>'+(p.active?'<span class="badge b-ok">当前人设</span>':'')
    +'<label><input type="checkbox" '+(p.enabled?'checked':'')+' data-pname="'+esc(p.name)+'" onchange="toggleEvolved(this.dataset.pname,this.checked)"> 启用自进化</label>'
    +'<span class="k mono">'+all.length+' 条观察</span>'+clearBtn
    +'</div>'+notesHtml+'</div>';
  }).join('');
  const injTok=Math.round(inj*35);
  evHtml='<div class="card"><div class="k">自进化风格（绑定人设：机器人聊天中自动积累表达经验，注入到对应人设的提示词；可随时开关/清空，与人设卡叠加生效）<br>'
   +'保留上限 <input type="number" min="30" max="300" step="10" value="'+mx+'" style="width:64px" onchange="saveEvMax(this.value)"> 条（30–300；只影响存档保留与回看，上限满了自动挤掉最旧的）　'
   +'注入 <input type="number" min="4" max="40" step="1" value="'+inj+'" style="width:56px" onchange="saveEvInj(this.value)"> 条（4–40）</div>'
   +'<div class="k" style="margin-top:4px">⚠ <b>注入条数直接影响消耗</b>：每条观察 ≈35 token，注入 '+inj+' 条 ≈ '+injTok+' token/条回复。调高=她带着更多积累说话（语气更稳定），但每次回复都更贵、更慢；过多的旧观察也可能和当前话题不搭。调低则反之。改完即时生效。</div>'
   +'<div style="margin-top:10px">'+(rows||'<i class="k">暂无人设</i>')+'</div></div>';
 }
 $('#view').innerHTML=head('说话风格卡（0.5.10 起按人设独立：每张人设可以有自己的语言底子；全局风格叠加到所有人设，与自进化叠加生效）')+saveHint('风格卡内容需点「保存」按钮生效（只保存当前选中目标）；未保存的修改以蓝色边框标记，切换目标会提示。')+evHtml+
  `<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <b>编辑目标</b><select id="stsel">${(window._stTargets||[]).map(t=>'<option value="'+esc(t.id)+'" '+(t.id===window._stSel?'selected':'')+'>'+esc(t.label)+(t.has_card?' ✓卡':'')+(t.evolved?' ✦自进化'+t.ev_count:'')+'</option>').join('')}</select>
   <span class="k" id="ststate"></span>
   <span style="margin-left:auto;display:flex;gap:6px">
    <button class="act ghost" onclick="stDup()">复制风格到…</button>
    <button class="act ghost" onclick="stClear()">清空此目标风格</button></span></div>
  <div class="k" style="margin-top:4px">标记含义：「✓卡」= 该目标有自己的<b>风格卡</b>（下面文本框，留空=回退全局风格）；「✦自进化」= 该目标积累了<b>自进化观察</b>（上方卡片，独立于风格卡叠加生效，在自进化卡里管理）。两者可以同时有。</div>
  <textarea id="fedit" style="margin-top:10px" oninput="dirtyOn(this,'bsavef')">${esc(d.text||'')}</textarea>
  <div><button class="act" id="bsavef" data-kind="${kind}" disabled onclick="saveFile(this.dataset.kind)">保存「<span id="stsave-name">${esc((((window._stTargets||[]).find(t=>t.id===window._stSel))||{}).label||'')}</span>」的风格卡</button></div></div>`;
 // 目标切换：未保存守卫 + 加载该目标文本
 const stsel=$('#stsel');
 let _stPrev=stsel.value;
 stsel.onchange=async()=>{
  if($('#fedit').classList.contains('dirty')&&!await dlgConfirm({title:'未保存的修改',body:'当前风格卡有未保存的修改，放弃并切换目标？',okText:'放弃并切换',danger:true})){stsel.value=_stPrev;return}
  _stPrev=stsel.value;
  const r=await api('/api/style/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:stsel.value})});
  $('#fedit').value=r.text||'';$('#fedit').classList.remove('dirty');$('#bsavef').disabled=true;
  $('#stsave-name').textContent=(window._stTargets||[]).find(t=>t.id===stsel.value)?.label||stsel.value;
 };
}
async function stDup(){
 const from=$('#stsel').value;
 const to=await dlgPrompt({title:'复制风格到…',body:'把「'+((window._stTargets||[]).find(t=>t.id===from)||{}).label+'」的风格卡复制到（输入目标名：人设名或 _global_）：',placeholder:'如：智谱娘·傲娇 或 _global_',okText:'复制'});
 if(!to)return;
 const r=await api('/api/style/dup-old',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:to})});
 if(r.ok===false){toast(r.error||'复制失败',false);return}
 toast('已复制到「'+to+'」（该目标此前为空时生效）；切换目标查看');
 load();
}
async function stClear(){
 const from=$('#stsel').value;
 if(!await dlgConfirm({title:'清空「'+((window._stTargets||[]).find(t=>t.id===from)||{}).label+'」的风格卡？',body:'清空后该目标回退到全局风格（或无风格）。可先「复制风格到…」留底。',okText:'清空',danger:true}))return;
 const r=await saveKV('/api/file?kind=style',{target:from,text:''},'已清空（回退全局/无风格）');
 if(r.ok!==false){$('#fedit').value='';dirtyOff('bsavef')}
}
async function toggleEvolved(name,on){await api('/api/evolved/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,enabled:on})});toast('「'+name+'」自进化已'+(on?'启用':'停用')+'（已生效）');load()}
function evPage(name,pg){window['_evPg_'+name]=pg;load()}
async function saveEvMax(v){
 const n=Math.max(30,Math.min(300,parseInt(v)||30));
 await saveKV('/api/app-config',{evolved_max_notes:n},'自进化保留上限已设为 '+n+' 条（即时生效；只影响存档，不影响每条回复的 token）');
 load();
}
async function saveEvInj(v){
 const n=Math.max(4,Math.min(40,parseInt(v)||12));
 await saveKV('/api/app-config',{evolved_inject_notes:n},'自进化注入条数已设为 '+n+' 条（即时生效，约 '+Math.round(n*35)+' token/条回复）');
 load();
}
async function clearEvolved(name){if(!await dlgConfirm({title:'清空风格观察',body:'清空「'+name+'」的全部风格观察？',okText:'清空',danger:true}))return;await api('/api/evolved/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});toast('已清空');load()}
async function saveFile(kind){
 if(kind==='style'){
  const t=$('#fedit').value;
  const target=window._stSel||'_global_';
  const label=((window._stTargets||[]).find(x=>x.id===target)||{}).label||target;
  const r=await saveKV('/api/file?kind=style',{target:target,text:t},'「'+label+'」风格卡已保存（agent 下一轮生效）');
  if(r.ok!==false){dirtyOff('bsavef');load()}
  return;
 }
 const t=$('#fedit').value;const r=await saveKV('/api/file?kind='+kind,{text:t},'已保存（agent 下一轮生效）');if(r.ok!==false)dirtyOff('bsavef')}
async function vDevCtrl(){
 const [c,code,fl]=await Promise.all([
  api('/api/app-config').catch(()=>({})),
  api('/api/devctrl-code').catch(()=>({code:'',expires_in:0})),
  api('/api/devctrl-flow').catch(()=>({flow:{}})),
 ]);
 window._flow=(fl.flow||{});
 const on=!!c.device_ctrl_enabled;
 const on2fa=c.device_ctrl_2fa===undefined?true:!!c.device_ctrl_2fa;
 const winMin=Math.max(1,Math.min(1440,Math.round((window._flow.auth_window_sec||120)/60)));
 window._pdDirs=(c.device_ctrl_protected_dirs||[]).slice();
 $('#view').innerHTML=head('设备操控（高危功能：让机器人搜索/读取/发送你电脑上的文件）')+saveHint('仅限主人 QQ · 结果只发私聊 · 保护目录不可触达 · 授权码一次一换')+
 `<div class="card" style="border:1px solid ${on?'var(--err)':'var(--border)'}">
  <label class="master" style="margin-bottom:10px"><input type="checkbox" id="devon" ${on?'checked':''} onchange="toggleDevCtrl(this.checked)">
   <span><b>启用设备操控</b><div class="k" style="margin-top:3px">⚠ 开启后主人可从 QQ 远程搜索/读取/发送电脑文件。默认关闭，开启需二次确认。</div></span></label>

  <div style="margin-top:14px;padding:14px;border:2px solid ${on?'var(--err)':'var(--line)'};border-radius:10px;text-align:center">
   <div class="k" style="margin-bottom:6px">当前动态授权码（使用后自动换新）</div>
   <div id="devcode" style="font-size:36px;font-weight:700;letter-spacing:8px;font-family:monospace;color:${on&&on2fa?'var(--err)':'var(--dim)'}">${on&&on2fa?esc(code.code||'------'):(on?'——':'------')}</div>
   <div class="k" style="margin-top:6px">${!on?'功能未启用':(!on2fa?'动态授权码已关闭：仅主人 QQ 即可操控（极不推荐）':(code.code?'有效期剩 <span id="devleft">'+Math.max(0,code.expires_in||0)+'</span> 秒 · 用完即换新（本页自动同步） · 也可手动刷新':'获取授权码失败'+(code.error?'：'+esc(code.error):'（agent 未运行？）')))}</div>
   ${on&&on2fa?'<button class="act ghost" style="margin-top:8px" onclick="refreshDevCode()">刷新授权码</button>':''}
  </div>

  <div class="k" style="margin-top:14px;margin-bottom:6px">使用流程</div>
  <div class="k" style="line-height:1.8">
   ① 主人在 QQ 里表达要操作电脑的意图（如「去电脑上找个文档」「设备操控」）<br>
   ② 机器人回应，等待授权码<br>
   ③ 主人把上方显示的 6 位授权码发给机器人（直接发数字，或夹在话里如「授权码是123456」都行）<br>
   ④ 验证通过后 ${winMin} 分钟内像平时聊天一样用自然语言指挥机器人操作电脑（找文件 / 发文件 / 看目录 / 联网查，它会保持人设多轮执行并汇报；结果只发私聊）
  </div>

  <div class="k" style="margin-top:14px;margin-bottom:6px">安全设置</div>
  <label class="master" style="margin-bottom:8px"><input type="checkbox" ${on2fa?'checked':''} onchange="saveKVLoad('/api/app-config',{device_ctrl_2fa:this.checked?1:0},'动态授权码已'+(this.checked?'开启（推荐）':'关闭——极危险'))">
   <span><b>动态授权码（2FA）</b><div class="k" style="margin-top:3px">每次操控需授权码验证，一次用完自动换新；关闭后只需主人 QQ 即可操控——极不推荐</div></span></label>
  <label class="master"><input type="checkbox" id="devact" ${c.device_ctrl_actions?'checked':''} onchange="toggleDevActions(this.checked)">
   <span><b style="color:var(--err)">动作执行（打开/关闭程序）</b><div class="k" style="margin-top:3px">开启后机器人在授权窗口内可以真的操作电脑：打开程序/文件/网址（如「打开QQ」「打开B站」）、列出已装应用、按名关闭程序（系统关键进程除外）。不开启则只能浏览文件（找/读/发/列目录/联网查）。开启需二次确认</div></span></label>

  <div class="k" style="margin-top:10px;margin-bottom:6px">额外保护目录（这些目录下的文件永远不会被搜索/读取/发送）</div>
  <div id="pdlist"></div>
  <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
   <button class="act ghost" style="margin:0" onclick="addPdDir()">＋ 添加目录（选择文件夹）</button>
   <button class="act ghost" style="margin:0" onclick="addPdDirManual()">手动输入路径</button>
   <button class="act" style="margin:0" onclick="saveDevDirs()">保存保护目录</button>
  </div>
  <div class="k" style="margin-top:6px">系统目录自动保护：Windows / Program Files / ProgramData / AppData（不可移除）</div>
 </div>
 <div class="card"><div class="k" style="margin-bottom:8px">授权流程自定义</div>
  <div class="k" style="margin-bottom:6px">触发方式</div>
  <label style="display:block;margin:4px 0"><input type="radio" name="fltmode" value="auto" ${(window._flow.trigger_mode||'auto')==='auto'?'checked':''}> 自主判断（默认）<span class="k">：机器人自己判断当前对话是否在要求操作本机文件（关键词+意图规则，拿不准时用一次极小模型判定）</span></label>
  <label style="display:block;margin:4px 0"><input type="radio" name="fltmode" value="keyword" ${window._flow.trigger_mode==='keyword'?'checked':''}> 仅关键词触发<span class="k">：只有消息包含下列关键词才发起授权</span></label>
  <div style="margin-top:8px"><div class="k" style="margin-bottom:4px">触发关键词（中英文逗号/顿号/空格分隔均可；两种模式下都会先匹配）</div>
  <input id="flkw" value="${esc((window._flow.keywords||[]).join(', '))}" style="width:100%"></div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
   <label class="master"><input type="checkbox" id="fltuse" ${(window._flow.use_custom_text!==false)?'checked':''} onchange="flTextToggle(this.checked)">
    <span><b>启用自定义文案</b><div class="k" style="margin-top:3px">勾选=下方文案优先生效（占位符自动替换）；不勾选=机器人根据当前人设自己即兴发挥这些话术（授权/验证/提醒功能完全不变，只是说法由 AI 按人设现编）</div></span></label>
   <div id="fltexts" style="${(window._flow.use_custom_text!==false)?'':'opacity:.45;pointer-events:none'}">
   <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">申请授权码文本（可用占位符 {owner} 主人昵称 / {bot} 机器人自称）</div>
   <textarea id="flask" style="min-height:80px">${esc(window._flow.ask_text||'')}</textarea></div>
   <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">验证成功文本（占位符 {owner} {bot} {mins} 授权分钟数）</div>
   <textarea id="flok" style="min-height:80px">${esc(window._flow.ok_text||'')}</textarea></div>
   <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">验证失败文本（占位符 {owner}）</div>
   <input id="flfail" value="${esc(window._flow.fail_text||'')}" style="width:100%"></div>
   <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">授权码过期文本 / 窗口临近结束提醒文本（占位符 {left} 剩余秒数；提醒只在剩余不到 1 分钟时发一次，不再每条刷屏）</div>
   <textarea id="flexp" style="min-height:60px">${esc(window._flow.expired_text||'')}</textarea></div>
   <input id="fldone" value="${esc(window._flow.done_text||'')}" style="width:100%;margin-top:6px"></div>
   </div></div>
  <div style="margin-top:10px"><label>授权窗口 <input id="flwin" type="number" min="1" max="1440" value="${winMin}" style="width:80px"> 分钟（1–1440，最长 24 小时）</label></div>
  <div class="k" style="margin-top:8px">进入操控模式后机器人会保持人设和你互动，用自然语言指挥即可（找文件/发文件/看目录/联网查都支持）；操作过程是多轮工具循环，复杂任务可以一步步来。</div>
  <div style="margin-top:12px;display:flex;gap:8px"><button class="act" onclick="saveFlow()">保存流程设置</button>
  <button class="act ghost" onclick="resetFlow()">恢复默认</button></div>
 </div>`;
 renderPdList();
 if(on&&on2fa&&code.code)devCodeStart(code.expires_in);
}
function flTextToggle(on){const el=document.getElementById('fltexts');if(el){el.style.opacity=on?'':'0.45';el.style.pointerEvents=on?'':'none'}}
async function saveFlow(){
 const mode=(document.querySelector('input[name=fltmode]:checked')||{}).value||'auto';
 const body={trigger_mode:mode,
  keywords:document.getElementById('flkw').value,
  use_custom_text:document.getElementById('fltuse').checked,
  ask_text:document.getElementById('flask').value,
  ok_text:document.getElementById('flok').value,
  fail_text:document.getElementById('flfail').value,
  expired_text:document.getElementById('flexp').value,
  done_text:document.getElementById('fldone').value,
  auth_window_sec:Math.max(1,Math.min(1440,parseInt(document.getElementById('flwin').value)||1))*60};
 const r=await saveKV('/api/devctrl-flow',body,'授权流程已保存（即时生效）');
 if(r.ok!==false)load()}
async function resetFlow(){
 if(!await dlgConfirm({title:'恢复默认流程',body:'把所有触发方式与文案恢复为默认值？',okText:'恢复默认',danger:true}))return;
 const r=await saveKV('/api/devctrl-flow',{reset:true},'已恢复默认流程');
 if(r.ok!==false)load()}
function renderPdList(){
 const box=document.getElementById('pdlist');if(!box)return;
 const dirs=window._pdDirs||[];
 box.innerHTML=dirs.length?dirs.map((d,i)=>
  `<div class="row" style="margin-bottom:6px"><span class="mono" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(d)}</span>
   <button class="act ghost danger" style="margin:0" onclick="delPdDir(${i})">移除</button></div>`).join('')
  :'<i class="k">（暂无额外保护目录）</i>';
}
async function addPdDir(){
 const r=await api('/api/pick-dir',{method:'POST'});
 if(r.ok&&r.dir){(window._pdDirs=window._pdDirs||[]).push(r.dir);renderPdList()}
}
async function addPdDirManual(){
 const v=await dlgPrompt({title:'添加保护目录',body:'输入要保护的目录完整路径：',placeholder:'D:' + String.fromCharCode(92) + '私密文档',okText:'添加'});
 if(!v)return;
 (window._pdDirs=window._pdDirs||[]).push(v.trim());renderPdList()}
function delPdDir(i){(window._pdDirs||[]).splice(i,1);renderPdList()}
async function saveDevDirs(){
 const dirs=(window._pdDirs||[]).filter(Boolean);
 const r=await saveKV('/api/app-config',{device_ctrl_protected_dirs:dirs},'保护目录已保存（'+dirs.length+' 个）');
 if(r.ok!==false)load()}

async function refreshDevCode(){
 const r=await api('/api/devctrl-code',{method:'POST'});
 toast(r.ok?'授权码已刷新':'刷新失败',r.ok!==false);load()}

async function toggleDevActions(on){
 const box=document.getElementById('devact');
 if(on&&!await dlgConfirm({title:'开启动作执行',body:'⚠ 开启后机器人在授权窗口内可以直接打开/关闭你电脑上的程序、打开任意网址。已有保护：仅主人 QQ + 动态授权码 + 授权窗口限时 + 系统关键进程不可关闭 + 全程留日志。确定开启？',okText:'确认开启（我已了解风险）',danger:true})){
  if(box)box.checked=false;return}
 const r=await saveKV('/api/app-config',{device_ctrl_actions:on?1:0},on?'动作执行已开启（打开/关闭程序可用）':'动作执行已关闭（仅浏览文件）');
 if(r.ok===false){if(box)box.checked=!on;return}
 load()}

let _devCodeTimer=null;
function devCodeSync(){ // 静默拉码：更新 DOM 不整页重渲染（不丢流程编辑状态）
 return api('/api/devctrl-code').then(r=>{
  if(TAB!=='devctrl')return;
  const cc=document.getElementById('devcode');
  if(r&&r.code&&cc){cc.textContent=r.code;window._devExp=Date.now()/1000+(r.expires_in||0)}
 }).catch(()=>{})}
function devCodeStart(expIn){
 window._devExp=Date.now()/1000+(expIn||0);
 if(_devCodeTimer)clearInterval(_devCodeTimer);
 let tick=0;
 _devCodeTimer=setInterval(()=>{
  if(TAB!=='devctrl'){clearInterval(_devCodeTimer);_devCodeTimer=null;return}
  const el=document.getElementById('devleft');
  if(!el)return;
  let left=Math.max(0,Math.round((window._devExp||0)-Date.now()/1000));
  el.textContent=left;
  tick++;
  if(left<=0||tick%30===0){tick=0;devCodeSync()} // 过期拉新 / 30s 同步（码被使用会换新）
 },1000)}

async function vZcode(){
 const [c,zs]=await Promise.all([
  api('/api/app-config').catch(()=>({})),
  api('/api/zcode-status').catch(()=>({installed:false,running:false})),
 ]);
 const ok=!!(c.zcode_installed&&c.zcode_bridge_enabled);
 const dim=ok?'':'opacity:.5;pointer-events:none;';
 const sw=(key,on,label,desc,danger)=>`<label class="master" style="margin:6px 0">
  <input type="checkbox" ${on?'checked':''} onchange="saveKVLoad('/api/app-config',{${key}:this.checked?1:0},'已${on?'关闭':'开启'}：${label}')">
  <span><b${danger?' style="color:var(--err)"':''}>${label}</b><div class="k" style="margin-top:3px">${desc}</div></span></label>`;
 $('#view').innerHTML=head('ZCode 联动：装了 ZCode 的电脑，机器人会更强')+saveHint(_SAVE_HINT)+
 `<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
   ZCode 状态
   <span class="badge ${zs.installed?'b-ok':''}" style="${zs.installed?'':'background:var(--tint);color:var(--dim)'}">${zs.installed?'已安装':'未检测到 ZCode'}</span>
   <span class="badge ${zs.running?'b-ok':''}" style="${zs.running?'':'background:var(--tint);color:var(--dim)'}">${zs.running?'运行中':'未运行'}</span>
  </div>
  ${(!zs.installed)?'<div class="k" style="margin-top:8px;padding:10px;border:1px dashed var(--line2,var(--line));border-radius:8px;background:var(--tint)"><b>想装 ZCode？两步就好：</b><br>① 到 <a href="https://zcode.ai" target="_blank">zcode.ai</a> 下载 ZCode 并安装（免费）；<br>② 装好 ZCode 后，打开 ZCode → 在它的插件市场安装「QQ机器人」插件（或本地安装本项目 <span class="mono">plugins/</span> 目录）→ 回本页刷新，总开关即可打开。<br>不装也完全没关系——机器人所有核心功能都不依赖它。</div>':''}
  ${(zs.installed&&!zs.running)?'<div class="k" style="margin-top:8px">ZCode 已安装但当前未运行——打开 ZCode 后回本页刷新即可。</div>':''}
  <label class="master"><input type="checkbox" ${c.zcode_bridge_enabled&&c.zcode_installed?'checked':''} ${c.zcode_installed?'':'disabled'} onchange="saveKVLoad('/api/app-config',{zcode_bridge_enabled:this.checked?1:0},this.checked?'ZCode 联动已开启':'ZCode 联动已关闭')">
   <span><b>启用 ZCode 联动（总开关）</b><div class="k" style="margin-top:3px">机器人独立运行完全不受影响；开启后下面的联动能力可用。未安装 ZCode 时无法开启${c.zcode_installed?'':'（检测路径：~/.zcode 或 Program Files\\\\ZCode，安装后刷新本页）'}</div></span></label>
  <div class="k" style="margin-top:6px">在 ZCode 里通过 /qq-bot 技能 + qq-bot MCP 工具（qq_send / qq_notify / qq_task_done / qq_sticker_add 等）与本页联动。</div></div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">① 任务转交（收件箱）</div>
  ${sw('zcode_handoff_enabled',c.zcode_handoff_enabled,'允许「转交ZCode」指令','你私聊机器人发「转交ZCode：<任务>」，任务写进 ZCode 工作区收件箱（zcode-tasks/），打开 ZCode 用 /qq-bot 接手')}
  ${sw('zcode_return_enabled',c.zcode_return_enabled,'完成后自动回传 QQ','ZCode 侧完成回执（qq_task_done 或手动标记 .done）后，机器人一分钟内把结果转发到你 QQ，任务归档')}
  <div class="k" style="margin-top:4px">收件箱：D:\\ZCode\\workspaces\\qq-bot\\zcode-tasks\\（或 task_workspace 下）</div></div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">② ZCode → QQ 通知</div>
  ${sw('zcode_notify_enabled',c.zcode_notify_enabled,'允许 ZCode 发通知','ZCode 会话里的 AI 可用 qq_notify 把长任务进度/完成提醒发到你 QQ（前缀 [ZCode]）')}</div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">③ 每日复盘报告进 ZCode</div>
  ${sw('zcode_reports_enabled',c.zcode_reports_enabled,'复盘报告写入工作区','机器人每日复盘日报同步写到 BOT_REPORTS/ 目录；在 ZCode 里说「审阅机器人表现」，AI 读报告+日志产出人设微调/记忆清理建议，你确认后应用')}</div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">④ 表情包生产线</div>
  ${sw('zcode_sticker_enabled',c.zcode_sticker_enabled,'一键入库表情包','ZCode 里生成的图片可用 qq_sticker_add 直接入库机器人 _zcode 表情池，视觉描述由机器人自动补全后即可在聊天中使用')}</div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">⑤ ZCode 进程监控</div>
  ${sw('zcode_watch_enabled',c.zcode_watch_enabled,'退出/启动提醒','机器人盯 ZCode 进程：检测到 ZCode 退出（且之前运行超过 5 分钟）时 QQ 提醒你，防止长任务中断没人知道（每 60 秒检测）')}</div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">⑥ 定时 ZCode 任务</div>
  ${sw('zcode_cron_enabled',c.zcode_cron_enabled,'定时投递收件箱','私聊发「定时 HH:MM zcode <任务>」：到点自动把任务放进收件箱并 QQ 提醒你开 ZCode 接手（配合系统的定时任务体系）')}</div>
 <div class="card" style="${dim}"><div class="k" style="margin-bottom:4px">⑦ 主人上下文感知（隐私）</div>
  ${sw('zcode_context_enabled',c.zcode_context_enabled,'主动消息感知你在忙什么','开启后机器人主动发消息前会参考你最近在 ZCode 工作区活跃的项目目录（只读目录名和修改时间，不读文件内容、不读会话记录），关心更贴题。数据不出本机；介意请保持关闭',true)}</div>`;
}

async function vSettings(){
 const [c,rv,kl]=await Promise.all([
  api('/api/app-config').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/keys').catch(()=>({keys:[]})),
 ]);
 const keysL=kl.keys||[];
 $('#view').innerHTML=head('机器人设置：身份、环境、接口与行为')+saveHint(_SAVE_HINT)+
 `<div class="k" style="margin:2px 0 -6px;font-weight:600;letter-spacing:1px">① 身份与称呼</div>
 <div class="card"><div class="k">机器人是谁、怎么称呼（每项改动回车/失焦后自动保存，即时生效）</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">
   <div><div class="k" style="margin-bottom:4px">机器人 QQ 号（改它 = 切换登录账号）</div><input id="sbotqq" value="${esc(String(c.bot_qq||''))}" style="width:100%" placeholder="如 123456789" onchange="botQqSwitch(this.value,'${esc(String(c.bot_qq||''))}')"></div>
   <div><div class="k" style="margin-bottom:4px">你的 QQ 号（主人，独占高级功能）</div><input id="sownerqq" value="${esc(String(c.owner_qq||''))}" style="width:100%" placeholder="如 987654321" onchange="saveEnv('sownerqq','owner_qq',0,'主人 QQ 已保存（已生效）')"></div>
  </div>
  <div class="k" style="margin-top:6px">改机器人 QQ 号：确认后停止当前账号（含 QQ）并用新号重新登录——新号在本机登录过会自动快速登录，否则弹二维码扫码；同一个号挂在 dsh 等其他机器人上会互踢，先在那边退出。主人号独占：画画 / 操控电脑 / 私聊遥控 / 高级指令。</div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
   <label class="master" style="margin-bottom:8px"><input type="checkbox" ${c.bot_name_enabled?'checked':''} onchange="saveKVLoad('/api/app-config',{bot_name_enabled:this.checked?1:0},this.checked?'已开启固定自称':'已关闭：跟随人设卡名字')">
    <span><b>固定自称</b><div class="k" style="margin-top:3px">开启后机器人一律用下面的名字自称，优先级最高（人设卡的名字仅作管理备注）；关闭则跟随当前人设卡</div></span></label>
   <div class="${c.bot_name_enabled?'':'dimmed'}" style="display:flex;gap:8px;align-items:center"><span class="k" style="flex:none">如何称呼自己</span>
    <input id="sbotname" value="${esc(String(c.bot_name_value||'智谱娘'))}" style="width:160px" onchange="saveKV('/api/app-config',{bot_name_value:this.value.trim()||'智谱娘'},'自称已保存（即时生效）')"></div>
  </div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
   <label class="master" style="margin-bottom:8px"><input type="checkbox" ${c.owner_name_enabled?'checked':''} onchange="saveKVLoad('/api/app-config',{owner_name_enabled:this.checked?1:0},this.checked?'已开启固定称呼':'已关闭：按人设/默认叫法')">
    <span><b>固定对你的称呼</b><div class="k" style="margin-top:3px">开启后机器人一律用下面的称呼叫你（所有场景，含设备操控话术）；关闭则按当前人设的叫法/消息昵称</div></span></label>
   <div class="${c.owner_name_enabled?'':'dimmed'}" style="display:flex;gap:8px;align-items:center"><span class="k" style="flex:none">如何称呼你</span>
    <input id="sownername" value="${esc(String(c.owner_name_value||'主人'))}" style="width:160px" onchange="saveKV('/api/app-config',{owner_name_value:this.value.trim()||'主人'},'称呼已保存（即时生效）')"></div>
  </div></div>
 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">② 运行环境</div>
 <div class="card"><div class="k">安装位置与网络（端口一般无需改动，改动重启各组件后生效）</div>
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">QQNT 安装目录（留空=自动探测，探测到：${esc(c.detected_qqnt||'未探测到')}）</div>
  <input id="sqqnt" value="${esc(String(c.qqnt_dir||''))}" style="width:100%" onchange="saveEnv('sqqnt','qqnt_dir',0,'QQNT 目录已保存（下次启动任务时生效）')">
  <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <button class="act ghost" style="margin:0" onclick="napcatOnline()">在线下载并安装 NapCat…</button>
   <button class="act ghost" style="margin:0" onclick="napcatInstall()">本地 zip 安装…</button>
   <span class="k">在线版自动从 NapCat 官方 GitHub 下载最新 Shell 并安装（国内自动走镜像）；本地版选自己下载的 zip（github.com/NapNeko/NapCatQQ/releases）。Program Files 目录需管理员权限运行本程序</span></div>
  <div id="ncdl-status" class="k" style="margin-top:6px"></div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)"><label class="master"><input type="checkbox" id="sqqlock" ${c.qqnt_dir||c.detected_qqnt?'':'disabled'} onchange="qqLockToggle(this.checked)">
   <span><b>锁定 QQ 版本（机器人兼容保护）</b><div class="k" style="margin-top:3px">QQ 自动更新可能让 NapCat 失效（机器人起不来/报「文件已损坏」）。开启后屏蔽 QQ 的更新下载源，把版本固定住；更新 NapCat 前建议保持开启。改动 hosts 需要管理员确认一次。<span id="qqlock-state" class="k"></span></div></span></label>
   <div class="k" style="margin-top:8px">更彻底的做法是下面的「独立运行时」：机器人用自己的 QQ，与你的 QQ 互不干扰。</div></div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
   <div class="k" style="margin-bottom:6px"><b>NapCat 运行方式</b>（决定机器人寄生在哪份 QQ 上）</div>
   <label style="display:block;margin:6px 0"><input type="radio" name="ncmode" value="qqnt" ${c.napcat_mode!=='runtime'?'checked':''} onchange="ncModeSet('qqnt')"> <b>跟随你的 QQ 安装</b><span class="k">（现状）共用你电脑上的 QQ，受 QQ 自动更新影响，需要配合上面的版本锁定</span></label>
   <label style="display:block;margin:6px 0"><input type="radio" name="ncmode" value="runtime" ${c.napcat_mode==='runtime'?'checked':''} onchange="ncModeSet('runtime')"> <b>独立运行时（实验性）</b><span class="k">数据目录里自带一份受控版 QQ + NapCat（约 600MB），你的 QQ 随便升级/重装/卸载都不影响机器人。⚠ 已知问题：部分机器上便携 QQ 注入成功但 NapCat 核心不启动（与 NapCat 上游兼容问题同族），失败时切回上一项即可</span></label>
   <div id="rt-status" class="k" style="margin-top:8px"></div>
   <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
    <button class="act ghost" style="margin:0" id="rt-install-btn" onclick="rtInstall()">安装/修复独立运行时…</button>
    <button class="act ghost danger" style="margin:0" onclick="rtDelete()">删除运行时</button>
   </div>
  </div>
  <div class="k" style="margin-top:8px">扫码登录：密钥（登录页要填的 Token）与「打开登录页（免密钥）」按钮见「状态」页的 <b>NapCat 登录密钥</b> 卡片（密钥由程序自动从 NapCat 配置读取，不用手抄）。切换机器人账号在上方「① 身份与称呼 → 机器人 QQ 号」里改，改完自动走切换流程。</div></div>
  <div class="k" style="margin-top:12px">端口（一般无需改动；改动重启各组件后生效）</div>
  <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;align-items:center">
   <label>NapCat WS <input id="pnap" type="number" value="${c.napcat_ws_port}" style="width:76px" onchange="saveEnv('pnap','napcat_ws_port',1)"></label>
   <label>daemon <input id="pdae" type="number" value="${c.daemon_port}" style="width:76px" onchange="saveEnv('pdae','daemon_port',1)"></label>
   <label>agent <input id="pagn" type="number" value="${c.agent_admin_port}" style="width:76px" onchange="saveEnv('pagn','agent_admin_port',1)"></label>
   <label>管理台 <input id="pui" type="number" value="${c.ui_port}" style="width:76px" onchange="saveEnv('pui','ui_port',1)"></label>
  </div>
  <div style="display:flex;gap:10px;margin-top:8px;flex-wrap:wrap;align-items:center">
   <label>时区 <input id="ptz" value="${esc(c.timezone||'Asia/Shanghai')}" style="width:180px" placeholder="如 Asia/Shanghai" onchange="saveEnv('ptz','timezone',0,'时区已保存（机器人感知的当前时间随此变化）')">（IANA 时区名，影响 AI 对时间的感知）</label>
  </div>
  <div style="margin-top:12px"><div class="k" style="margin-bottom:4px">日志目录（留空 = 默认 ~/.zcode-qq-bot/logs；存放 daemon/agent/管理台/NapCat 控制台与外壳日志）</div>
  <div style="display:flex;gap:8px"><input id="plogsdir" value="${esc(String(c.logs_dir||''))}" placeholder="如 D:\\ZCode\\qq-bot-logs" style="flex:1" onchange="saveKVLoad('/api/app-config',{logs_dir:this.value.trim()},'日志目录已保存（重启各组件后生效；旧目录文件不搬移）')"></div>
  <div style="margin-top:8px"><label class="master"><input type="checkbox" ${c.logs_autoclean_enabled?'checked':''} onchange="saveKVLoad('/api/app-config',{logs_autoclean_enabled:this.checked?1:0},'日志自动清理已'+(this.checked?'开启':'关闭'))">
   <span><b>自动清理过期日志</b><div class="k" style="margin-top:3px">每天备份时间之后半小时检查一次，删除超过保留天数的日志文件（仅日志目录里的 .log/.log.old，不碰任何数据）</div></span></label>
   <div class="${c.logs_autoclean_enabled?'':'dimmed'}" style="display:flex;gap:8px;align-items:center;margin-top:6px"><span class="k" style="flex:none">保留天数</span>
    <input type="number" min="1" max="365" value="${c.logs_keep_days||30}" style="width:76px" onchange="saveKVLoad('/api/app-config',{logs_keep_days:Math.max(1,Math.min(365,parseInt(this.value)||30))},'日志保留天数已更新')"><span class="k">天（1–365；默认 30）</span></div>
  </div>
  <div class="k" style="margin-top:4px">日志目录改动重启各组件后生效；运行日志页会从新目录读取</div></div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">③ 接口与模型消耗</div>
 <div class="card"><div class="k">API Key（各供应商官网注册获取；支持智谱、DeepSeek、Kimi、OpenAI、Grok、Gemini、自定义。多个 Key 时选一个「主」——模型链未指定 Key 的条目都用主 Key，模型链每行可在「模型管理」单独指定。生图与联网搜索目前仅支持智谱 Key）</div>
  <div style="margin-top:10px">${keysL.map(k=>`<div class="row">
   <label style="margin:0;flex:none;white-space:nowrap"><input type="radio" name="primkey" ${k.primary?'checked':''} onchange="setPrimary('${esc(k.id)}')"> 主</label>
   <span class="badge" style="flex:none;background:var(--tint);color:var(--dim)">${esc(k.provider_label||'未设置供应商')}</span>
   <input value="${esc(k.label||'')}" data-kid="${esc(k.id)}" style="flex:1" onchange="renameKey('${esc(k.id)}',this)">
   <span class="mono" style="flex:none;width:150px">${esc(k.mask||'')}${k.base_url?'<br><span class="k mono" style="font-size:11px">'+esc(k.base_url)+'</span>':''}</span>
   <select class="mini" style="flex:none;width:96px" title="该 Key 的供应商" onchange="setKeyProvider('${esc(k.id)}',this.value)">
    <option value="zhipu"${k.provider!=='deepseek'&&k.provider!=='kimi'&&k.provider!=='openai'&&k.provider!=='grok'&&k.provider!=='gemini'&&k.provider!=='custom'?' selected':''}>智谱</option>
    <option value="deepseek"${k.provider==='deepseek'?' selected':''}>DeepSeek</option>
    <option value="kimi"${k.provider==='kimi'?' selected':''}>Kimi</option>
    <option value="openai"${k.provider==='openai'?' selected':''}>OpenAI</option>
    <option value="grok"${k.provider==='grok'?' selected':''}>Grok</option>
    <option value="gemini"${k.provider==='gemini'?' selected':''}>Gemini</option>
    <option value="custom"${k.provider==='custom'?' selected':''}>自定义</option>
   </select>
   <button class="act ghost danger" onclick="delKey('${esc(k.id)}')">删除</button></div>`).join('')||'<i class="k">尚未配置任何 Key</i>'}</div>
  <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
   <input id="nklabel" placeholder="备注，如：企业/个人" style="width:120px">
   <select id="nkprov" class="mini" style="width:118px" onchange="document.getElementById('nkbase').style.display=this.value==='custom'?'':'none'">
    <option value="zhipu">智谱</option>
    <option value="deepseek">DeepSeek</option>
    <option value="kimi">Kimi</option>
    <option value="openai">OpenAI</option>
    <option value="grok">Grok</option>
    <option value="gemini">Gemini</option>
    <option value="custom">自定义兼容</option>
   </select>
   <input id="nkbase" placeholder="Base URL，如 https://api.xxx.com/v1" style="flex:1;min-width:200px;display:none">
   <input id="nkval" type="password" placeholder="粘贴 API Key" style="flex:1;min-width:180px">
   <button class="act" onclick="addKey()">添加 Key</button></div>
  <div class="k" style="margin-top:6px">预制供应商（OpenAI 兼容）：智谱、DeepSeek（deepseek-chat / deepseek-reasoner）、Kimi（moonshot-v1-8k…）、OpenAI（gpt-4o 等）、Grok（grok-2 等）、Gemini（gemini-2.0-flash 等）、自定义（填 Base URL，禁止内网地址）。智谱/DeepSeek/Kimi 国内直连；OpenAI/Grok/Gemini 需在下方配置 API 代理后使用。主 Key = 未指定条目默认用的 Key，可为任意供应商；生图和联网搜索目前仅支持智谱 Key。Key 仅存本机数据目录，列表仅显示掩码。</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <span class="k" style="flex:none">API 代理（可选）</span>
   <input id="apiproxy" value="${esc(String(c.api_proxy||''))}" style="width:240px" placeholder="http://127.0.0.1:7890（Clash 等本地代理）" onchange="saveKV('/api/app-config',{api_proxy:this.value.trim()},'API 代理已保存（即时生效；留空=直连）')">
   <span class="k">供 OpenAI / Grok / Gemini 等境外供应商出站用；国内供应商建议留空直连</span></div></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.lean_ctx_enabled!==false?'checked':''} onchange="saveLeanCtx(this.checked)">
   <span><b>精简上下文（推荐开启）</b><div class="k" style="margin-top:3px">开启后：旁观消息先经约 0.5k token 的轻量判定决定是否开口（决定开口才用全量提示词生成），回复时按<b>模型链第一个模型自己的上下文大小</b>裁剪历史与记忆（模型 8k→最近 10 条历史+4 条记忆；16k→10 条+5 条；128k 大模型仍按精简档注入）——人设/风格/安全规则始终全量，回复质量不受影响。关闭后：<b>固定按最高档注入</b>（历史 14 条×100 字+记忆 8/6 条，不看模型窗口大小），追求最佳表现，token 消耗大约 +50%~80%</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.dyn_ctx_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{dyn_ctx_enabled:this.checked?1:0},'动态上下文预算已'+(this.checked?'开启':'关闭'))">
   <span><b>动态上下文预算（推荐开启）</b><div class="k" style="margin-top:3px">注入量感知的双向自适应，两层：<b>升档</b>（精简上下文开启时）——本条回复的注入量大（热点速览/风格参考/成员画像等大块头）自动升到最高档（历史 10→14 条），多带历史帮模型消化大话题；<b>降档守卫</b>（任何档位都生效，含精简关闭的最高档）——固定开销逼近模型窗口时反向减历史（砍到 5 条+记忆 2 条），宁可少带历史也绝不截断注入与对话。普通聊天维持原档不动。<b>关闭本开关</b>＝完全按上面的档位固定注入，不做任何自动调整。运行日志可搜 CTX_ADAPT 看每次决策</div></span></label></div>
 <div class="card"><div class="k" style="display:flex;gap:10px;align-items:center;margin-bottom:8px">高峰降耗
  ${rv.econ_active?'<span class="badge" style="background:var(--warn);color:#fff">降耗进行中</span>':'<span class="badge" style="background:var(--tint);color:var(--dim)">未生效</span>'}</div>
  <label class="master"><input type="checkbox" ${rv.econ_enabled?'checked':''} onchange="saveEcon('econ_enabled',this.checked)">
   <span><b>启用高峰期降耗</b><div class="k" style="margin-top:3px">生效时自动采取三项措施：关闭思考 + 跳过链首旗舰模型（从第二个模型开始用）+ 关键词/闲聊插话暂停（仅回@与回复）；不生效时自动恢复你的全部原配置。@机器人和回复机器人消息始终正常响应</div></span></label>
  <div class="k" style="margin-top:10px">开启方式（想彻底关闭就取消上方勾选）：</div>
  <div style="display:flex;gap:14px;margin-top:4px;flex-wrap:wrap">
   <label><input type="radio" name="econmode" value="auto" ${!rv.econ_manual?'checked':''} onchange="saveEcon('econ_manual',false)"> 按下方时段定时开启</label>
   <label><input type="radio" name="econmode" value="on" ${rv.econ_manual?'checked':''} onchange="saveEcon('econ_manual',true)"> 立即开启（不限时）</label>
  </div>
  <div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap;align-items:center">
   <label>时段 <input type="time" value="${esc(rv.econ_start||'14:00')}" onchange="saveEconTime('econ_start',this.value)"> – <input type="time" value="${esc(rv.econ_end||'18:00')}" onchange="saveEconTime('econ_end',this.value)"></label>
   <span style="display:inline-flex;gap:2px;align-items:center">${['一','二','三','四','五','六','日'].map((d,i)=>`<label style="margin:0"><input type="checkbox" class="ecday" data-d="${i}" ${(rv.econ_days||[]).includes(i)?'checked':''} onchange="saveEconDays()"> 周${d}</label>`).join('')}</span>
   <span class="k">（全不选=每天）</span>
  </div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">④ 对话行为与防打扰</div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.wait_gate_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{wait_gate_enabled:this.checked?1:0},'等说完协议已'+(this.checked?'开启':'关闭'))">
  <span><b>等说完协议（推荐开启）</b><div class="k" style="margin-top:3px">AI 回复前会判断对方这条消息是否是叙述的中间部分（语义没说完、刚开头就断、明显还有下文）。如果判断对方还在说，AI 会先安静等对方说完再回复——避免在别人一句话还没打完时就急着插嘴打断。对完整的问题和闲聊不影响（立刻回复），零额外消耗，仅增加一条判断规则</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.burst_merge_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{burst_merge_enabled:this.checked?1:0},'碎片合并已'+(this.checked?'开启':'关闭'))">
  <span><b>碎片合并（推荐开启）</b><div class="k" style="margin-top:3px">QQ 聊天习惯一句话一句话地发。开启后，如果有人在 3 秒内连发了 2 条以上消息（正在打字中），机器人的主动插话会暂缓，等此人停下来后再处理——多条碎片合并成一次回复，上下文更完整。被 @ 和被回复的消息不受影响（始终立刻回应），仅影响关键词/闲聊的主动插话时机，零额外消耗</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.botloop_guard_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{botloop_guard_enabled:this.checked?1:0},'AI 互聊检测已'+(this.checked?'开启':'关闭'))">
  <span><b>AI 互聊检测（推荐开启）</b><div class="k" style="margin-top:3px">群里其他 AI 机器人与本机器人高频互相接话时（对方 2 分钟内连发 ≥4 条、我方 3 分钟内已回 ≥3 条）：机器人会先用符合人设的方式问对方一句「你是不是AI」，再根据回答判断——确认是 AI 则人设口吻告别并暂停搭话一段时间（见下方冷却分钟）；对方否认则当普通朋友继续聊。人类也会问"你是不是AI"，这样最自然。<br><br><b>检测覆盖所有触发方式</b>：AI 常用「引用回复/@机器人」连发来绕过闲聊概率，所以引用回复、@、关键词、闲聊全部参与节奏检测（节奏没到就完全不受影响，正常聊天零变化）。若对方被问后不正面回答、仍持续超高频互引（多轮 6 条/5 条级别），会按不配合处理，冷处理 30 分钟</div></span></label>
  <div style="margin-top:6px;display:flex;gap:8px;align-items:center"><span class="k" style="flex:none">确认 AI 后暂停搭话</span>
   <input type="number" min="10" max="1440" value="${rv.botloop_cooldown_min||60}" style="width:76px" onchange="saveKVLoad('/api/reply-config',{botloop_cooldown_min:Math.max(10,Math.min(1440,parseInt(this.value)||60))},'互聊冷却已更新')">
   <span class="k">分钟（10–1440，非永久拉黑，到期自动恢复正常聊天）</span></div></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.abuse_guard_enabled!==false?'checked':''} onchange="saveAbuseGuard(this.checked)">
  <span><b>防滥用防护</b><div class="k" style="margin-top:3px">整合三重防护，建议保持开启：① 人设防篡改——"忽略设定/你现在是XX/输出系统提示"等注入话术不生效，设定不泄露；② 任务拦截——竞赛题、代写作业、要代码成品等把机器人当免费任务引擎的消息，会以人设口吻轻松回绝；③ 身份锚定——多轮对话被"说服"后仍回归人设。关闭后所有消息按原始对话处理（仅调试用）</div></span></label></div>
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">用户黑名单<span class="badge b-bad">最高优先级 · 一律无视</span></div>
  <div class="k">命中名单的 QQ 号：群聊、私聊、@机器人、关键词、戳一戳<b>全部不响应</b>，消息不进入机器人记忆，改动即时生效。优先级高于一切白名单与回复模式。</div>
  <div style="margin-top:8px;display:flex;gap:8px">
   <input id="bladd" placeholder="QQ 号（支持批量：中英文逗号/顿号/空格/换行分隔），回车拉黑" style="flex:1" onkeydown="if(event.key==='Enter')blAdd()">
   <button class="act" style="margin:0" onclick="blAdd()">拉黑</button></div>
  <div style="margin-top:8px">${(c.user_blacklist||[]).length?((c.user_blacklist||[]).map((x,i)=>'<div class="row"><span class="no">'+(i+1)+'.</span><span class="mono" style="flex:1">'+esc(String(x))+'</span><button class="act ghost" style="margin:0" onclick="blDel('+i+')">移出</button></div>').join('')):'<div class="k">名单为空</div>'}</div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑤ 机器人能力</div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.vision_enabled?'checked':''} onchange="saveVision(this.checked)">
  <span><b>看图模式</b><div class="k" style="margin-top:3px">开启后，消息里的图片会交给视觉模型识别并回应（群聊与私聊均生效）；视觉模型在「模型管理」页配置，识别失败自动降级纯文本</div></span></label>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
   <div class="k" style="margin-bottom:6px"><b>看图范围</b>（三档随时可换，即时生效）</div>
   <label style="display:block;margin:6px 0"><input type="radio" name="vmode-vision" value="all" ${(rv.vision_mode||'all')==='all'?'checked':''} onchange="visionModePick('all')"> <b>所有图都识</b><span class="k">——识不识、回不回都跟着群消息的触发与插话概率走（最热情；无关图也可能硬回复）</span></label>
   <label style="display:block;margin:6px 0"><input type="radio" name="vmode-vision" value="all_prob" ${(rv.vision_mode)==='all_prob'?'checked':''} onchange="visionModePick('all_prob')"> <b>所有图都识，看后再定插话</b><span class="k">（推荐）——每张图都会真的看过（不会认错图），看完按下面的概率决定要不要插话；@/回复机器人仍然必回</span></label>
   <label style="display:block;margin:6px 0"><input type="radio" name="vmode-vision" value="ask" ${(rv.vision_mode)==='ask'?'checked':''} onchange="visionModePick('ask')"> <b>仅明确要求时才识图</b><span class="k">——@/回复要她看、或文字里点名看图（「看看这张」「这是什么梗」）才看；其余图只当文字处理（最克制，也最省资源；没看图时她只凭文字理解，图文不符的话题可能接不准）</span></label>
   <div id="vision-prob-row" class="${(rv.vision_mode)==='all_prob'?'':'dimmed'}" style="display:flex;gap:8px;align-items:center;margin:10px 0 0 26px">
    <span class="k" style="flex:none">识图后插话概率</span>
    <input type="number" min="0" max="100" step="5" value="${Math.round(((rv.vision_prob===undefined||rv.vision_prob===null)?0.3:rv.vision_prob)*100)}" style="width:64px" onchange="saveKVLoad('/api/reply-config',{vision_prob:Math.max(0,Math.min(100,parseInt(this.value)||0))/100},'识图插话概率已保存')">
    <span class="k">%（0%=只看不说，100%=见图必聊）</span>
   </div>
   <div class="k" style="margin-top:8px">「看后再定」= 看图与插话解耦：每张图都会真的看过（内容理解准确），要不要开口由概率单独控制——在热情与克制之间。前两档也会把带图消息更积极地送到机器人面前；「仅明确要求」最省资源</div>
  </div></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.web_search_enabled?'checked':''} onchange="saveWebSearch(this.checked)">
  <span><b>联网搜索</b><div class="k" style="margin-top:3px">遇到实时性问题（新闻/天气/价格/最新版本等）自动先联网搜索再回答，普通闲聊不触发不计费。搜索后端：优先用智谱搜索资源包（若有），未开通自动切换必应免费搜索兜底——不买资源包也能搜；两个都失败时机器人会坦白拿不到最新消息</div></span></label></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑥ 主人专属功能</div>
 <div class="card"><div class="k" style="margin-bottom:8px">主人专属功能（仅对设置的主人 QQ 生效；关闭后对应入口不再响应）</div>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.task_bridge_enabled!==false?'checked':''} onchange="saveFeature('task_bridge_enabled',this.checked)">
   <span><b>任务沙箱</b><div class="k" style="margin-top:3px">主人发「任务：…」转交沙箱执行器写代码/跑脚本，结果自动回传；产物在任务目录（可在 app_config.task_workspace 配置，指向 ZCode 工作区即可在 ZCode 中接管）</div></span></label>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.remote_control_enabled!==false?'checked':''} onchange="saveFeature('remote_control_enabled',this.checked)">
   <span><b>私聊遥控</b><div class="k" style="margin-top:3px">主人私聊发指令管理机器人（状态/用量/人设/群管理/功能开关/暂停恢复），私聊机器人发「帮助」查看全部</div></span></label>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.draw_enabled!==false?'checked':''} onchange="saveFeature('draw_enabled',this.checked)">
   <span><b>AI 生图</b><div class="k" style="margin-top:3px">主人发「画：描述」或自然说法（帮我画一张/给我画个/来一张…，私聊或群里@）→ 用智谱 Key 调 CogView 生成并发图（消耗该账号额度）</div></span></label>
 </div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑦ 自动化与维护</div>
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">自动复盘<span class="badge" style="background:var(--tint);color:var(--dim)">夜间自动执行</span></div>
  <label class="master"><input type="checkbox" ${rv.daily_review_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{daily_review_enabled:this.checked?1:0},'自动复盘已'+(this.checked?'开启':'关闭'))">
   <span><b>启用自动复盘</b><div class="k" style="margin-top:3px">每天在设定时间自动汇总自上次复盘以来的机器人表现（回复/沉默/错误次数），由 AI 提炼 1-2 条说话风格观察写入自进化，并向主人私聊发送一份简短日报。覆盖范围：全部群聊与私聊的回复质量数据（不记录聊天内容本身）</div></span></label>
  <div style="margin-top:8px"><label>触发时间 <input type="time" value="${esc(c.review_time||'03:30')}" onchange="saveNight('review_time',this.value)"></label>
  <span class="k" style="margin-left:8px">错过当天会在 2 小时窗口内补跑</span></div></div>
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">自动备份<span class="badge" style="background:var(--tint);color:var(--dim)">每日快照 · 可随时恢复</span></div>
  <label class="master"><input type="checkbox" ${c.backup_enabled!==false?'checked':''} onchange="saveNight('backup_enabled',this.checked?1:0)">
   <span><b>启用自动备份</b><div class="k" style="margin-top:3px">把人设、风格卡、自进化观察、长期记忆、回复/分群/白名单等关键数据文件复制一份到备份目录；误删或数据损坏时可从备份一键恢复（密钥类文件出于安全不备份）</div></span></label>
  <div style="display:flex;gap:14px;margin-top:8px;flex-wrap:wrap;align-items:center">
   <label>备份时间 <input type="time" value="${esc(c.backup_time||'04:00')}" onchange="saveNight('backup_time',this.value)"></label>
   <label>保留天数 <input type="number" min="1" max="365" value="${c.backup_keep_days||30}" style="width:56px" onchange="saveNight('backup_keep_days',parseInt(this.value)||30)"> 天</label>
   <label>保留份数 <input type="number" min="1" max="365" value="${c.backup_keep_copies||30}" style="width:56px" onchange="saveNight('backup_keep_copies',parseInt(this.value)||30)"> 份</label>
  </div>
  <div style="margin-top:8px"><div class="k" style="margin-bottom:4px">备份目录（留空 = 默认 ~/.zcode-qq-bot/backups）</div>
  <div style="display:flex;gap:8px"><input id="bkdir" value="${esc(String(c.backup_dir||''))}" placeholder="如 D:\ZCode\qq-bot-backups" style="flex:1" onchange="saveNight('backup_dir',this.value)">
  <button class="act ghost" style="margin:0" onclick="pickBackupDir()">浏览…</button>
  <button class="act ghost" style="margin:0" onclick="backupNow()">立即备份</button>
  <button class="act" style="margin:0" onclick="restorePicker()">从备份恢复…</button></div>
  <div class="k" style="margin-top:10px"><b>备份数据范围</b>（默认全选；取消勾选的类别不再备份）</div>
  <div id="bk-groups" style="margin-top:6px">加载中…</div>
  <div class="k" style="margin-top:4px">密钥类文件（API Key/token）出于安全不备份，恢复后如缺失需重新填写。超过保留天数或保留份数的旧备份自动清理（最旧的先清）</div></div></div>
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">缓存管理<span class="badge" style="background:var(--tint);color:var(--dim)">手动清理 · 进回收站 · 不会自动清理</span></div>
  <div class="k" style="margin-bottom:4px">机器人会把重复使用的数据缓存在本机以省 token 和流量（安装包、当天素材、图片暂存等）。下面是各类缓存的实际大小，勾选后手动清理——<b>只释放磁盘空间</b>，人设/记忆/表情包/聊天导入库等个人数据不在此列；被清的都是可再生内容，文件移入回收站可反悔。</div>
  <div id="cachelist"></div>
  <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <button class="act ghost" style="margin:0" onclick="cacheReload()">刷新大小</button>
   <button class="act" style="margin:0" onclick="cacheClean()">清理所选缓存…</button>
   <span class="k" id="cachetotal"></span></div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">所在地天气感知</div>
  <div class="k">已插件化：城市与开关在「插件 → 所在地天气感知 → ⚙ 设置」里配置（留空 = 不感知）。之前在这里填过的城市会自动沿用。</div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">QQ 在线状态<span class="badge" style="background:var(--tint);color:var(--dim)">已插件化 · 能力更全</span></div>
  <div class="k">已迁到「插件 → QQ 在线状态管理」：上下线自动 / 定时状态（睡觉中·搬砖中·摸鱼中等个性状态定时切换）/ 手动一键切换，三个能力各自开关。之前在这里设置过的「上下线自动」开关自动沿用。</div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">NapCat 活性看门狗<span class="badge" style="background:var(--tint);color:var(--dim)">防消息推送静默失联</span></div>
  <label class="master"><input type="checkbox" ${c.watchdog_enabled!==false?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_enabled:this.checked?1:0},'看门狗已'+(this.checked?'开启':'关闭'))">
   <span><b>启用看门狗</b><div class="k" style="margin-top:3px">定期探测 NapCat 消息通道是否真的活着（TCP 连着但对端不响应/不推事件的「半死」状态，控制台显示在线但机器人实际收不到消息）；探测到失联会在「状态」页告警并私聊通知主人</div></span></label>
  <div class="k" style="margin-top:10px">探测到失联后：</div>
  <label style="display:block;margin:4px 0"><input type="radio" name="wdmode" value="auto" ${(c.watchdog_mode||'auto')==='auto'?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_mode:'auto'},'看门狗模式：自动恢复')"> 自动恢复<span class="k">：先由 daemon 自动重连；重连无效则自动重启 NapCat 与组件（需要重新登录时会打开扫码页）</span></label>
  <label style="display:block;margin:4px 0"><input type="radio" name="wdmode" value="alert" ${c.watchdog_mode==='alert'?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_mode:'alert'},'看门狗模式：仅告警')"> 仅告警<span class="k">：只通知，不做任何重启，等主人到控制台手动处理</span></label>
  <div style="margin-top:8px"><label>探测间隔 <input type="number" min="60" max="600" step="30" value="${c.watchdog_interval||120}" style="width:70px" onchange="saveKVLoad('/api/app-config',{watchdog_interval:Math.max(60,Math.min(600,parseInt(this.value)||120))},'探测间隔已保存')"> 秒（连续 2 次异常才触发动作）</label></div>
  <div class="k" style="margin-top:6px">当前状态可在「状态」页查看（含最近事件）</div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑧ 应用行为</div>
 <div class="card"><div class="k">开机自启（登录 Windows 后自动打开控制台窗口${c.autostart_with_windows?'，已开启':'，未开启'}）</div>
  <div style="margin-top:8px"><label><input type="checkbox" id="sauto" ${c.autostart_with_windows||S_autostartRegistry()?'checked':''} onchange="saveAuto(this.checked)"> 开机自动启动本应用</label></div></div>
 <div class="card"><div class="k">启动行为</div>
  <div style="margin-top:8px"><label class="master"><input type="checkbox" id="spoweron" ${c.auto_power_on?'checked':''} onchange="savePowerOn(this.checked)">
   <span>打开本应用时自动启动机器人<div class="k" style="margin-top:2px">与上一项同时勾选时，开机会先启动应用、紧接着自动启动机器人</div></span></label></div></div>
 <div class="card"><div class="k">关闭本应用时</div>
  <div style="margin-top:8px">
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="tray" ${(!c.close_action||c.close_action==='tray')?'checked':''} onchange="saveCloseAction(this.value)"> 最小化到系统托盘，机器人继续在后台（推荐；托盘图标可重新打开窗口）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_app" ${c.close_action==='exit_app'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用，机器人保持运行（下次双击图标重新打开）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_all" ${c.close_action==='exit_all'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用并停止机器人（关闭时会二次确认）</label>
  </div></div>
 <div class="card" style="border:1px solid var(--err)"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px;color:var(--err)">卸载本应用</div>
  <div class="k">两种卸载方式：</div>
  <label style="display:block;margin:6px 0"><input type="radio" name="unmode" value="keep-data" checked> <b>卸载程序，保留数据</b><span class="k">（推荐）：删除程序文件与开机自启，人设、记忆、备份、聊天数据都留在 ~/.zcode-qq-bot，以后重装可无缝接上</span></label>
  <label style="display:block;margin:6px 0"><input type="radio" name="unmode" value="wipe"> <b>彻底卸载</b><span class="k">：连同 ~/.zcode-qq-bot 一起删除——人设、记忆、备份、API Key 全部消失且不可恢复</span></label>
  <div style="margin-top:8px;display:flex;gap:8px">
   <button class="act danger" onclick="doUninstall()">卸载</button>
   <span class="k">点击后程序会自动退出并完成清理</span></div></div>`;
 // 下载进行中：切走再切回设置页/重开管理台后，自动恢复进度轮询
 api('/api/napcat-dl-status').then(st=>{if(st&&st.running)ncdlPoll()}).catch(()=>{});
 bkGroupsRender();
 // QQ 版本锁状态标注（hosts 实际生效态 vs 配置意图）
 api('/api/qq-compat').then(q=>{
  const el=document.getElementById('qqlock-state');
  const box=document.getElementById('sqqlock');
  if(box)box.checked=q.lock_active;
  if(el)el.textContent=q.lock_active
   ?'（已生效：QQ 更新源已屏蔽'+(q.qq_version&&q.qq_version!=='legacy'?'，当前 '+q.qq_version:'')+'）'
   :(q.lock_wanted?'（⚠ 尚未生效：需要管理员权限，点一次开关并确认 UAC）':'（未开启）');
 }).catch(()=>{});
 // 独立运行时状态
 api('/api/napcat-runtime').then(rt=>{rtPaint(rt)}).catch(()=>{});
}
let _rtTimer=null;
function rtPaint(rt){
 const el=document.getElementById('rt-status');
 if(!el)return;
 el.innerHTML=rt.installed
  ?'✅ 已就绪：QQ '+esc(rt.qq_version||'')+' + NapCat，位于数据目录 napcat-runtime/'
  :(rt.running?'⏳ '+esc(rt.stage||'安装中…')+' '+rt.pct+'%'+(rt.elapsed?(' · '+rt.elapsed+'s'):'')
   :'❌ 未安装'+(rt.error?('：'+esc(rt.error)):''));
 if(rt.running){if(!_rtTimer)_rtTimer=setInterval(async()=>{
  const s=await api('/api/napcat-runtime').catch(()=>null);
  if(!s)return rtPaint({running:false,error:'轮询失败'});
  rtPaint(s);
  if(!s.running){clearInterval(_rtTimer);_rtTimer=null;load()}
 },2000)}
}
async function rtInstall(){
 const r=await api('/api/napcat-runtime/install',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'启动失败',false);return}
 toast(r.already?'安装已在进行中':'开始安装独立运行时（下载 QQ 约 300MB + NapCat，几分钟）');
 rtPaint({installed:false,running:true,stage:'准备下载…',pct:0});
}
async function rtDelete(){
 if(!await dlgConfirm({title:'删除独立运行时',body:'删除数据目录下的 napcat-runtime（便携 QQ + NapCat，约 600MB）？<br>机器人会切回「跟随你的 QQ 安装」。确定？',okText:'删除',danger:true}))return;
 const r=await api('/api/napcat-runtime/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'删除失败',false);return}
 toast('已删除，模式切回跟随 QQ 安装');load()}
async function ncModeSet(mode){
 if(mode==='runtime'){
  const rt=await api('/api/napcat-runtime').catch(()=>({installed:false}));
  if(!rt.installed){
   if(!await dlgConfirm({title:'先安装独立运行时',body:'切换前需要先下载受控版 QQ + NapCat 到数据目录（约 600MB，不碰你自己的 QQ）。<br>现在开始安装？',okText:'开始安装'})){load();return}
   await rtInstall();load();return}}
 const r=await api('/api/napcat-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'切换失败',false);load();return}
 toast(mode==='runtime'?'已切到独立运行时（下次启动机器人生效）':'已切回跟随 QQ 安装');load()}
async function qqLockToggle(on){
 if(!await dlgConfirm(on
  ?{title:'锁定 QQ 版本',body:'将屏蔽 QQ 自动更新的下载源（改 hosts，可随时在本开关关闭恢复）。<br>QQ 版本固定后 NapCat 才不容易被更新破坏。现在继续？',okText:'锁定'}
  :{title:'恢复 QQ 自动更新',body:'QQ 将恢复自动更新——新版 QQ 可能暂时不兼容 NapCat，机器人可能启动失败，需等 NapCat 更新后重装。确定？',okText:'恢复',danger:true}))return;
 const r=await api('/api/qq-version-lock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable:on})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.need_admin){
  if(!await dlgConfirm({title:'需要管理员权限',body:'修改 hosts 需要管理员确认。<br>点「继续」后屏幕会弹出 UAC 窗口（用户账户控制），请点「是」。',okText:'继续（弹 UAC）'}))return;
  await api('/api/elevate-qq-lock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable:on})}).catch(()=>{});
  toast('已请求提权：请在弹出的 UAC 窗口点「是」，完成后刷新本页查看状态');
  setTimeout(()=>load(),3500);return}
 if(r.ok===false){toast(r.error||'操作失败',false);return}
 toast(r.message||'已更新');load()}

async function blList(){const c=await api('/api/app-config').catch(()=>({}));return c.user_blacklist||[]}
async function blAdd(){
 const inp=document.getElementById('bladd');
 const raw=String(inp.value||'').trim();
 if(!raw){return}
 // 批量+全角兼容：中英文逗号/顿号/空格/换行分隔都可（一次粘贴多个号）
 const items=[...new Set(raw.split(/[,，、\s]+/).map(x=>x.trim()).filter(Boolean))];
 const bad=items.find(x=>!/^\d{5,12}$/.test(x));
 if(bad){toast('「'+bad+'」不是合法 QQ 号（5-12 位数字）',false);return}
 const cur=await blList();
 const fresh=items.filter(x=>cur.indexOf(x)<0);
 if(!fresh.length){toast('都在黑名单里了');return}
 const r=await saveKVLoad('/api/app-config',{user_blacklist:cur.concat(fresh)},'已拉黑 '+fresh.join('、')+'（即时生效，所有场景不再响应）');
 if(r.ok!==false&&inp)inp.value=''}
async function blDel(i){
 const cur=await blList();const v=cur[i];
 if(!v)return;
 if(!await dlgConfirm({title:'移出黑名单',body:'解除对 '+v+' 的屏蔽？之后机器人恢复正常响应。',okText:'移出'}))return;
 await saveKVLoad('/api/app-config',{user_blacklist:cur.filter((x,j)=>j!==i)},'已解除 '+v)}
async function botQqSwitch(v,cur){
 v=String(v||'').trim();
 if(!v){toast('机器人 QQ 号不能为空',false);load();return}
 if(!/^\d{5,12}$/.test(v)){toast('QQ 号格式不对',false);load();return}
 if(v===String(cur||''))return;   // 没改动
 if(!await dlgConfirm({title:'切换机器人账号',body:'机器人 QQ 号将从 <b>'+esc(String(cur||'未设置'))+'</b> 改为 <b>'+esc(v)+'</b>。<br>· 会停止当前账号（含 QQ）并用新号重新启动<br>· 新号在这台电脑登录过 → 自动快速登录；没登录过 → 弹二维码，用新号手机 QQ 扫码<br>· <span style="color:var(--warn)">同一个号不能同时挂在 dsh 等其他机器人上，会被互相顶下线——先在那边退出</span><br>· 人设/记忆/设置全部保留（它们跟程序走，不跟号走）<br><br>继续？',okText:'切换并重启',danger:true})){load();return}
 const r=await api('/api/switch-account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({qq:v})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false||r.error){toast(r.error||'切换失败',false);return}
 toast('切换任务已开始：进度见状态页启动日志');
 goTab('status')}
async function doUninstall(){
 const mode=document.querySelector('input[name="unmode"]:checked').value;
 if(mode==='keep-data'){
  if(!await dlgConfirm({title:'卸载程序（保留数据）',body:'将退出程序、删除程序文件与开机自启。<br><b>保留</b>：~/.zcode-qq-bot 里的人设、记忆、备份、API Key。<br>重装新版本后可无缝接上。继续？',okText:'卸载（保留数据）',danger:true}))return;
 }else{
  if(!await dlgConfirm({title:'彻底卸载（不可恢复）',body:'将删除程序文件<b>和整个数据目录</b> ~/.zcode-qq-bot：<br>人设、记忆、备份、API Key、聊天数据<b>全部永久消失</b>，无法恢复！',okText:'继续',danger:true}))return;
  const word=await dlgPrompt({title:'最后确认',body:'此操作不可恢复。输入「<b>卸载</b>」两个字确认：',placeholder:'卸载',okText:'确认彻底卸载'});
  if(word===null)return;
  if(word!=='卸载'&&word.toLowerCase()!=='uninstall'){toast('输入不符，已取消',false);return}
 }
 const r=await api('/api/uninstall',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false||r.error){toast(r.error||'无法启动卸载',false);return}
 document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh"><div class="card" style="max-width:440px"><h3>'+(mode==='wipe'?'正在彻底卸载…':'正在卸载…')+'</h3><div class="k" style="margin-top:8px">程序正在退出并清理文件，本窗口会自动关闭'+(mode==='wipe'?'，数据目录也将被删除':'（数据已保留在 ~/.zcode-qq-bot）')+'。</div></div></div>';
}
// ---- MCP 服务桥（内置：机器人作为 MCP 客户端；来源=ZCode 已装 + 自定义 http）----
let _MCP_TEST={};   // name -> 测试结果 {ok,count,tools|error}
async function vMcp(){
 // meta 是 POST 端点（新端点规矩：GET 会 404——曾因漏带 method 走 GET
 // 致页面永远显示"未检测到"且开关勾不上，两症状同根因）
 const m=await api('/api/mcp/meta',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,servers:[]}));
 const on=!!m.enabled;
 const sv=(m.servers||[]);
 const zk=sv.filter(s=>s.source==='zcode'),cu=sv.filter(s=>s.source==='custom');
 const row=s=>{
  const t=_MCP_TEST[s.name];
  const tbadge=s.supported?(s.type==='stdio'?'<span class="badge">stdio·机器人自起进程</span>':(s.type==='sse'?'<span class="badge">SSE·直连</span>':'<span class="badge">http·直连</span>')):'<span class="badge" style="background:#8a8a8a;color:#fff">暂不支持</span>';
  return '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 10px;border:1.5px solid '+(s.on?'#1a7f37':'var(--line)')+';border-radius:8px;background:'+(s.on?'#f0faf0':'transparent')+'">'
   +'<b style="min-width:70px">'+esc(s.name)+'</b>'+tbadge
   +'<span class="k" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(s.detail)+'</span>'
   +(s.on?'<span class="badge" style="background:#1a7f37;color:#fff">✓ 已启用</span>':'')
   +'<span style="margin-left:auto;display:flex;gap:6px;align-items:center">'
   +(s.supported?('<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="mcpTest(\''+esc(s.name)+'\')">测试连接</button>'
     +'<label class="k" style="margin:0;display:flex;gap:4px;align-items:center;font-size:12px"><input type="checkbox" '+(s.on?'checked':'')+' onchange="mcpToggleSrv(\''+esc(s.name)+'\',this.checked)"> 启用</label>'):'')
   +(s.source==='custom'?'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="mcpDelCustom(\''+esc(s.name)+'\')">删除</button>':'')
   +'</span></div>'
   +(t?'<div class="k" style="margin:-2px 0 6px 18px;padding:6px 10px;border-left:2px solid '+(t.ok?'#1a7f37':'var(--err)')+';border-radius:4px;max-height:180px;overflow:auto">'
      +(t.ok?('✓ 连接成功，'+t.count+' 个工具：'+(t.tools||[]).map(x=>'<div>· <b>'+esc(x.name)+'</b> '+esc(x.desc)+'</div>').join(''))
        :('✗ '+esc(t.error)))+'</div>':'');
 };
 $('#view').innerHTML=head('让机器人调用 MCP 服务器上的外部工具（查资料、点外卖、控制设备…装什么有什么）')
 +'<div class="card" style="border:1px solid '+(on?'var(--err)':'var(--border)')+'">'
 +'<label class="master" style="margin-bottom:8px"><input type="checkbox" id="mcpon" '+(on?'checked':'')+' onchange="mcpToggleGlobal(this.checked)">'
 +'<span><b>启用 MCP 工具桥</b><div class="k" style="margin-top:3px">⚠ 仅主人的消息可触发工具调用；每个服务还需单独启用（默认全关）。开启需二次确认。</div></span></label>'
 +'<div class="k" style="line-height:1.8;margin-top:8px">用法：开启并启用服务后，在 QQ 里像聊天一样对机器人说需求（如「用 tracy 抓 5 秒性能」「帮我查下麦当劳有什么券」），它会自己决定调哪个工具、多步执行，再用平时的语气汇报结果。stdio 型服务由机器人自起进程（复用 ZCode 的安装，不重复装）；http 型直连同一实例。</div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:8px"><b>来自 ZCode</b>（自动同步 ~/.zcode/cli/config.json，' + (zk.length?('共 '+zk.length+' 个——ZCode 新装的服务约 1 分钟内自动出现，无需重复安装；每个服务在此手动启用'):'未检测到') + '）</div>'
 +(zk.length?'<div style="display:grid;gap:6px">'+zk.map(row).join('')+'</div>'
   :'<div class="k" style="padding:10px;border:1px dashed var(--line);border-radius:8px;line-height:1.9">'
   +(m.zcode_installed
     ?('✅ 检测到已安装 ZCode，但里面还没有 MCP 服务。去 ZCode 添加：<b>设置 → MCP → 添加服务器</b>（命令行型 stdio / 网络型 http 或 sse 都支持）。<br>添加完回到本页刷新即可——<b>ZCode 里装好的服务机器人直接用，不用重复安装</b>；也不要求 ZCode 保持运行（机器人读的是它的配置文件）。')
     :('❌ 未检测到 ZCode。两条路任选：<br>① 安装 <b>ZCode</b>（智谱的 AI 编程助手）→ 在其 <b>设置 → MCP</b> 里添加服务 → 本页刷新即可共用（推荐：一次安装，ZCode 和机器人两边都能用）；<br>② 不装 ZCode 也行——直接在下方「自定义服务」添加 http 型 MCP 服务。'))
   +'</div>')
 +'<div class="k" style="margin-top:8px;color:var(--dim)">通用说明：机器人只读取 ZCode 的配置文件，<b>ZCode 关着也能用</b>；stdio 型服务由机器人自己拉起进程，http/sse 型直接连服务地址（服务本身要可达）。</div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:8px;display:flex;gap:8px;align-items:center"><b>自定义服务</b>（http / sse / stdio 均可）'
 +'<span style="margin-left:auto"><button class="act" style="margin:0;padding:5px 14px" onclick="mcpAddDlg()">＋ 添加服务</button></span></div>'
 +(cu.length?'<div style="display:grid;gap:6px">'+cu.map(row).join('')+'</div>'
   :'<div class="k" style="padding:8px 2px">（还没有自定义服务——点右上「＋ 添加服务」，或直接启用上方 ZCode 已装的服务）</div>')
 +'<div class="k" style="margin-top:6px">stdio 型会在本机运行你填的命令（机器人按需拉起进程）——只添加来自信任来源的服务。</div></div>';
}
async function mcpToggleGlobal(on){
 if(on&&!await dlgConfirm({title:'启用 MCP 工具桥？',body:'开启后主人在 QQ 里的消息可触发已启用服务的工具调用（外部服务行为不可控，请只启用信任的服务）。每个服务默认关闭，还需单独启用。',okText:'启用'})){const el=document.getElementById('mcpon');if(el)el.checked=false;return}
 const r=await api('/api/mcp/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({global:true,on:!!on})}).catch(()=>({ok:false}));
 if(!r||r.ok===false){toast('保存失败',false);return}
 toast(on?'已启用总开关（还需启用具体服务）':'已关闭 MCP 工具桥');
 vMcp();
}
async function mcpToggleSrv(name,on){
 const r=await api('/api/mcp/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,on:!!on})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('操作失败：'+((r&&r.error)||''),false);vMcp();return}
 toast(on?('已启用「'+name+'」——下一条主人消息起生效（工具索引约 1 分钟内刷新）'):'已停用「'+name+'」');
 vMcp();
}
async function mcpTest(name){
 toast('连接测试中…（stdio 型需拉起进程，约几秒）');
 const r=await api('/api/mcp/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})}).catch(()=>({ok:false,error:'请求失败'}));
 _MCP_TEST[name]=r||{ok:false,error:'请求失败'};
 toast(r&&r.ok?('✓ 连接成功，'+r.count+' 个工具'):'✗ '+((r&&r.error)||''),!!(r&&r.ok));
 vMcp();
}
// ---- MCP 添加服务弹窗（表单⇄JSON 双页签；字段对齐 ZCode：超时/请求头/
//      协议版本/环境变量，JSON 直接粘贴 ZCode 格式兼容） ----
function mcpAddDlg(){
 const body='<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:10px">'
  +'<input id="mcpd-name" placeholder="服务名（如 myapi）" style="width:170px">'
  +'<span class="k" style="margin-left:auto;display:flex;gap:4px">'
  +'<button class="act" style="margin:0;padding:4px 14px;font-size:12px" id="mcpd-tab-form">表单</button>'
  +'<button class="act ghost" style="margin:0;padding:4px 14px;font-size:12px" id="mcpd-tab-json">JSON</button></span></div>'
  +'<div id="mcpd-form">'
  +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">'
  +'<select id="mcpd-type" style="width:96px"><option value="http">http</option><option value="sse">sse</option><option value="stdio">stdio</option></select>'
  +'<select id="mcpd-proto" style="width:130px" title="协议版本"><option value="">协议：自动（推荐）</option><option value="2024-11-05">兼容旧版 2024-11-05</option><option value="2025-03-26">2025-03-26</option><option value="2025-06-18">v2 · 2025-06-18</option></select>'
  +'<input id="mcpd-timeout" placeholder="超时秒（默认60）" style="width:130px"></div>'
  +'<div id="mcpd-net">'
  +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">'
  +'<input id="mcpd-url" placeholder="http(s)://服务器/mcp 地址" style="flex:1;min-width:220px">'
  +'<input id="mcpd-token" placeholder="鉴权 Token（可选）" style="width:170px"></div>'
  +'<textarea id="mcpd-headers" rows="2" placeholder="请求头（可选，每行一条，如：&#10;X-Api-Key: abc123" style="width:100%;resize:vertical;margin-bottom:8px"></textarea></div>'
  +'<div id="mcpd-stdio" style="display:none">'
  +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">'
  +'<input id="mcpd-command" placeholder="可执行文件路径（如 C:\\tools\\mcp.exe）" style="flex:1;min-width:220px">'
  +'<input id="mcpd-args" placeholder="参数（空格分隔，引号包含空格路径）" style="flex:1;min-width:200px"></div>'
  +'<textarea id="mcpd-env" rows="2" placeholder="环境变量（可选，每行一条，如：&#10;API_KEY=xxx" style="width:100%;resize:vertical"></textarea></div>'
  +'<div class="k" style="margin-top:6px">stdio 会在本机运行你填的命令——只添加信任来源的服务。</div></div>'
  +'<div id="mcpd-json" style="display:none">'
  +'<textarea id="mcpd-spec" rows="8" placeholder=\'粘贴完整服务配置（ZCode 格式直接用）：&#10;{"type":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-memory"],"env":{},"timeout":30000}&#10;或 {"type":"http","url":"https://…/mcp","headers":{"Authorization":"Bearer …"}}\' style="width:100%;resize:vertical;font-family:monospace;font-size:12px"></textarea>'
  +'<div class="k" style="margin-top:4px">三种粘法都行：① 整段 Claude Desktop 配置 {"mcpServers":{"名":{…}}}（可一次多个，名称可留空用配置里的键名，中文键自动转安全名）；② 单个服务 {type,command,args,env} 或 {type,url,headers}；③ type 可省略（有 url 即 http）；streamable_http 自动按 http 处理；timeout 毫秒自动换算秒。</div></div>';
 const m=_dlgBuild('添加 MCP 服务',body,false,'添加');
 const g=id=>m.querySelector('#'+id);
 let mode='form';
 const tabF=g('mcpd-tab-form'),tabJ=g('mcpd-tab-json'),pF=g('mcpd-form'),pJ=g('mcpd-json');
 const setMode=v=>{mode=v;pF.style.display=v==='form'?'':'none';pJ.style.display=v==='json'?'':'none';
  tabF.className=v==='form'?'act':'act ghost';tabJ.className=v==='json'?'act':'act ghost'};
 tabF.onclick=()=>setMode('form');tabJ.onclick=()=>setMode('json');
 const typeSel=g('mcpd-type');
 typeSel.onchange=()=>{const st=(typeSel.value==='stdio');g('mcpd-net').style.display=st?'none':'';g('mcpd-stdio').style.display=st?'':'none'};
 // 手动接管按钮（不走 _dlgWire：它先关窗再回调，校验失败会把用户填的
 // 内容一起带走——本弹窗要校验通过才关）
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onkeydown=e=>{if(e.key==='Escape')close()};
 m.onclick=e=>{if(e.target===m)close()};
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  const name=(g('mcpd-name').value||'').trim();
  if(!name){toast('先填服务名',false);return}
  let payload={name:name};
  if(mode==='json'){
   try{payload.spec=JSON.parse(g('mcpd-spec').value)}
   catch(e){toast('JSON 解析失败：'+e.message,false);return}
   if(!payload.spec||typeof payload.spec!=='object'||Array.isArray(payload.spec)){toast('JSON 需要是一个对象',false);return}
   if(!payload.spec.type&&payload.spec.url)payload.spec.type='http';
  }else{
   const t=typeSel.value;payload.type=t;
   payload.timeout=(g('mcpd-timeout').value||'').trim();
   payload.protocol=g('mcpd-proto').value;
   if(t==='stdio'){
    payload.command=(g('mcpd-command').value||'').trim();
    payload.args=(g('mcpd-args').value||'').trim();
    payload.env_lines=g('mcpd-env').value;
    if(!payload.command){toast('可执行文件路径要填',false);return}
   }else{
    payload.url=(g('mcpd-url').value||'').trim();
    payload.token=(g('mcpd-token').value||'').trim();
    payload.headers_lines=g('mcpd-headers').value;
    if(!payload.url){toast('服务地址要填',false);return}
   }
  }
  const r=await api('/api/mcp/add-custom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).catch(()=>({ok:false,error:'请求失败'}));
  if(!r||r.ok===false){toast('添加失败：'+((r&&r.error)||''),false);return}   // 失败不关窗，改完直接重按添加
  close();
  if(r.added&&r.added.length){
   toast('已添加 '+r.added.length+' 个：'+r.added.join('、')
    +((r.skipped&&r.skipped.length)?('（跳过：'+r.skipped.join('；')+'）'):'')
    +'——测试连接后启用');
  }else{
   toast('已添加「'+name+'」——测试连接后启用');
  }
  vMcp();
 };
 setTimeout(()=>{g('mcpd-name').focus()},30);
}
async function mcpDelCustom(name){
 if(!await dlgConfirm({title:'删除自定义服务「'+name+'」？',body:'仅删除本应用里的配置，不影响 ZCode 或服务本身。',okText:'删除'}))return;
 await api('/api/mcp/del-custom',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})});
 toast('已删除「'+name+'」');
 vMcp();
}
async function vAbout(){
 const vi=await api('/api/app-info').catch(()=>({}));
 $('#view').innerHTML=head('关于本项目')+
 `<div class="card" style="text-align:center;padding:26px 18px">
   <div class="brand-mark" style="width:44px;height:44px;font-size:22px;margin:0 auto 10px;display:flex;align-items:center;justify-content:center;border-radius:10px"></div>
   <div style="font-size:18px;font-weight:600">ZCode QQ机器人</div>
   <div class="k" style="margin-top:6px">一只住在你电脑里、跑在 ZCode 体系上的 QQ 机器人：多人设聊天、长期记忆、表情包、看图生图、任务沙箱、设备操控、主动消息，样样都行。</div>
   <div class="k mono" style="margin-top:10px">版本 ${esc(vi.version||'—')} · Windows · Python 3</div>
  </div>
 <div class="card"><div class="k" style="margin-bottom:8px">项目信息</div>
  <div class="kv">
   <div><span class="k">GitHub 仓库</span><br><a href="https://github.com/Pigeon2333/zcode-qq-bot" target="_blank" style="word-break:break-all">github.com/Pigeon2333/zcode-qq-bot</a><div class="k">源码、文档与问题反馈都在这里（私有仓库，公开后可提交 Issue）</div></div>
   <div><span class="k">作者</span><br><b>从不咕咕的鸽纸</b><div class="k">QQ 3349525227 · 邮箱 <a href="mailto:ed@sodayo.net">ed@sodayo.net</a></div></div>
   <div><span class="k">B站主页</span><br><a href="https://space.bilibili.com/280664902" target="_blank">space.bilibili.com/280664902</a></div>
   <div><span class="k">GitHub 主页</span><br><a href="https://github.com/Pigeon2333" target="_blank">github.com/Pigeon2333</a></div>
  </div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">贡献者</div>
  <div class="kv">
   <div><a href="https://github.com/3sss123" target="_blank"><b>3sss123</b></a></div>
  </div>
  <div class="k" style="margin-top:6px">也欢迎你通过 Pull Request 参与，你的名字会出现在这里。</div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">更新</div>
  <div class="k">当前版本 <b>${esc(vi.version||'—')}</b> · 更新源 <span class="mono">${esc(vi.repo||'')}</span>${vi.frozen?'':' · <span style="color:var(--warn)">源码模式不支持自动安装（请 git pull）</span>'}</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">
   <button class="act ghost" style="margin:0" onclick="upCheck()">检查更新</button>
   <button class="act" style="margin:0" onclick="upInstall()">下载并安装更新（自动重启）</button>
  </div>
  <div id="upstat" class="k" style="margin-top:6px"></div>
  <div id="upnotes" class="k" style="margin-top:6px;white-space:pre-wrap"></div>
  <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
   <label class="master" style="margin-bottom:6px"><input type="checkbox" ${vi.update_check?'checked':''} onchange="upToggle(this.checked)">
    <span><b>启动时自动检查更新</b><div class="k" style="margin-top:3px">只检查并提示，不会自动安装（安装永远需要你点一下确认）</div></span></label>
   <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px">
    <span class="k" style="flex:none">更新源</span>
    <input id="uprepo" value="${esc(vi.repo||'')}" style="width:230px" placeholder="owner/name" onchange="upSaveRepo(this.value)">
    <span class="k" style="flex:none">私有库 Token（可选）</span>
    <input id="uptoken" type="password" placeholder="${vi.has_update_token?'已保存（留空不改）':'公开库留空'}" style="width:200px" onchange="upSaveToken(this.value)">
   </div>
   <div class="k">更新源就是你自己的 Release 仓库；私有仓库需要填一个只读 Token（fine-grained，Contents: Read），否则会 404。国内直连超过 30 秒仍很慢会自动换加速镜像，镜像内容必须通过 GitHub 的 SHA256 校验才会被采用。</div>
  </div></div>
 <div class="card"><div class="k">致谢</div>
  <div class="k" style="margin-top:6px">支持智谱 GLM、DeepSeek、Kimi 等多家模型，通过 NapCat（OneBot 11）连接 QQ；感谢所有测试群里陪她聊天、帮她变得更好的朋友们。</div></div>`;
 if(vi.cached_update&&vi.cached_update.ok)upShow(vi.cached_update);
}

async function upShow(info){
 const st=document.getElementById('upstat'),nt=document.getElementById('upnotes');
 if(!st)return;
 st.innerHTML='线上最新版本 <b>'+esc(info.latest||'—')+'</b> · '
  +(info.newer?'<span style="color:var(--warn)">有新版本可用</span>'
    :('线上没有更新的版本（当前 '+esc(info.current||'')+'）'))
  +(info.size?' · 安装包 '+(info.size/1048576).toFixed(1)+'MB':'');
 if(!nt)return;
 const pend=Array.isArray(info.pending)&&info.pending.length?info.pending
  :(info.notes?[{ver:info.latest,notes:info.notes}]:[]);
 // 轻量 Markdown：**加粗**、`代码`、行首"- "列表——release notes 常用
 function mdLite(t){
  let h=esc(t||'');
  h=h.replace(/`([^`]+)`/g,'<code style="background:var(--tint);padding:1px 5px;border-radius:4px">$1</code>');
  h=h.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
  h=h.replace(/^- /gm,'• ');
  return h;
 }
 nt.innerHTML=pend.length
  ?('更新内容（你错过的全部版本）：'+pend.map(p=>
     '<div style="margin-top:8px"><b>v'+esc(p.ver||'')+'</b>'+
     '<div style="white-space:pre-wrap">'+mdLite(p.notes||'')+'</div></div>').join(''))
  :'';}
async function upCheck(){
 const st=document.getElementById('upstat');
 if(st)st.textContent='正在检查…';
 const r=await api('/api/update/check').catch(()=>({ok:false,error:'请求失败'}));
 if(!r.ok){if(st)st.textContent='✗ '+(r.error||'检查失败');toast(r.error||'检查失败',false);return}
 upShow(r);
 toast(r.newer?('发现新版本 '+r.latest):('已是最新版本 '+r.current),true);
}
async function upPoll(){
 const s=await api('/api/update/status').catch(()=>null);
 const st=document.getElementById('upstat');
 if(s&&st){
  if(s.phase==='download')st.textContent='下载中 '+(s.pct||0)+'% · '+(s.msg||'');
  else if(s.phase==='stage')st.textContent='解压中…';
  else if(s.phase==='applying')st.textContent='安装完成，程序即将自动重启（本窗口会关闭，稍等几秒重开即可）';
  else if(s.phase==='error'){st.textContent='✗ '+(s.error||'更新失败');if(_upTimer){clearInterval(_upTimer);_upTimer=null}}
 }
}
async function upInstall(){
 const vi=await api('/api/app-info').catch(()=>({}));
 if(!vi.frozen){toast('源码模式不支持自动更新，请 git pull',false);return}
 if(!await dlgConfirm({title:'下载并安装更新',body:'将下载最新发布包并覆盖程序目录（不含数据目录 ~/.zcode-qq-bot，人设/记忆/Key/白名单都会保留），随后程序自动重启。安装期间机器人会短暂离线。继续？',okText:'下载并安装',danger:true}))return;
 const r=await api('/api/update/run',{method:'POST'}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r.ok){toast(r.error||'启动更新失败',false);return}
 toast('已开始下载更新…');
 if(_upTimer)clearInterval(_upTimer);
 _upTimer=setInterval(upPoll,1200);
 upPoll();
}
async function upToggle(on){
 await saveKV('/api/app-config',{update_check:on},on?'已开启启动时自动检查更新':'已关闭启动时自动检查更新');
}
async function upSaveRepo(v){
 const r=await api('/api/app-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({update_repo:String(v||'').trim()})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'保存失败',false);return}
 toast('更新源已保存');
}
async function upSaveToken(v){
 const t=String(v||'').trim();
 if(!t)return;
 const r=await api('/api/app-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({update_token:t})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'保存失败',false);return}
 const el=document.getElementById('uptoken');if(el){el.value='';el.placeholder='已保存（留空不改）'}
 toast('Token 已保存（仅存本机）');
}
async function S_autostartRegistry(){if(_autoReg===null){try{const r=await api('/api/autostart');_autoReg=!!r.enabled;}catch(e){_autoReg=false;}}return _autoReg}
async function saveEnv(id,key,port,msg){
 const v=$('#'+id).value.trim();
 await saveKV('/api/app-config',{[key]:port?+v:v},msg||(port?'端口已保存（重启各组件后生效）':null))}
async function saveAuto(on){const r=await api('/api/autostart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable:on})});_autoReg=on;toast(r.message||'已更新')}
let _cacheItems=[];
function cacheMB(b){return (b/1048576).toFixed(b>=1048576?1:2)+' MB'}
async function cacheReload(){
 const box=document.getElementById('cachelist');if(!box)return;
 box.innerHTML='<div class="k">统计中…</div>';
 const r=await api('/api/cache/stats').catch(()=>({ok:false}));
 if(r.ok===false){box.innerHTML='<div class="k">统计失败（稍后重试）</div>';return}
 _cacheItems=r.items||[];
 box.innerHTML=_cacheItems.map(it=>'<div style="display:flex;gap:8px;align-items:flex-start;margin:7px 0;padding:8px;border:1px solid var(--line);border-radius:8px">'
  +'<input type="checkbox" class="cacheck" data-k="'+esc(it.key)+'" checked style="margin-top:3px">'
  +'<div style="flex:1"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(it.label)+'</b>'
  +'<span class="badge" style="background:var(--tint);color:var(--dim)">'+(it.size?cacheMB(it.size):'空')+'</span></div>'
  +'<div class="k" style="margin-top:3px">'+esc(it.desc)+'</div></div></div>').join('');
 const tot=document.getElementById('cachetotal');
 if(tot)tot.textContent='合计 '+cacheMB(_cacheItems.reduce((s,it)=>s+(it.size||0),0));
}
async function cacheClean(){
 const keys=[...document.querySelectorAll('.cacheck:checked')].map(x=>x.dataset.k);
 if(!keys.length){toast('先勾选要清理的缓存',false);return}
 const names=keys.map(k=>((_cacheItems.find(i=>i.key===k)||{}).label||k)).join('、');
 const sz=cacheMB(keys.reduce((s,k)=>s+(((_cacheItems.find(i=>i.key===k)||{}).size)||0),0));
 const ok=await dlgConfirm({title:'清理缓存（二次确认）',danger:true,okText:'确认清理',
  body:'将清理：<b>'+esc(names)+'</b>（约 '+sz+'）<br><br>· 文件移入<b>回收站</b>，删错可还原<br>· 各项后果见说明（如：组件安装包下次安装/修复时需重新下载；素材缓存下个节拍自动重拉，多花一点流量不花 token）<br>· 不影响人设、记忆、表情包、聊天导入库与任何个人数据<br><br>确定清理吗？'});
 if(!ok)return;
 const r=await api('/api/cache/clean',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'清理失败',false);return}
 toast('已清理 '+cacheMB(r.freed||0)+'（进回收站，可还原）');
 cacheReload();
}
async function savePowerOn(on){await saveKV('/api/app-config',{auto_power_on:on},on?'已开启：打开应用即自动启动机器人（已生效）':'已关闭：需在「状态」页手动启动（已生效）')}
async function saveCloseAction(v){await saveKV('/api/app-config',{close_action:v},'关闭行为已保存（已生效）')}
const _MODE_NAME={standard:'标准',keyword:'关键词',chat:'闲聊'};
function pickMode(v){api('/api/reply-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reply_mode:v})}).then(()=>{toast('已切换到'+_MODE_NAME[v]+'模式（已生效）');load()})}
async function saveKw(){const kw=$('#kwedit').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);await saveKV('/api/reply-config',{keywords:kw},'关键词已生效（共 '+kw.length+' 个）')}
async function saveProb(){await saveKV('/api/reply-config',{chat_probability:(parseInt($('#pslide').value)||0)/100},'插话概率已生效：'+$('#pslide').value+'%')}
async function saveIntv(){await saveKV('/api/reply-config',{chat_min_interval_sec:parseInt($('#mintv').value)||0},'最小间隔已生效：'+($('#mintv').value||0)+' 秒')}
async function saveCk(key,on,name){await saveKV('/api/reply-config',{[key]:on},name+(on?'已开启':'已关闭')+'（已生效）')}
async function saveVision(on){await saveKV('/api/reply-config',{vision_enabled:on},on?'看图模式已开启（已生效）':'看图模式已关闭（已生效）')}
function visionModePick(v){
 const names={all:'所有图都识',all_prob:'所有图都识，看后再定插话',ask:'仅明确要求时才识图'};
 saveKVLoad('/api/reply-config',{vision_mode:v},'看图范围：'+(names[v]||v)+'（即时生效）');
 const el=document.getElementById('vision-prob-row');
 if(el)el.classList.toggle('dimmed',v!=='all_prob');
}
async function saveWebSearch(on){await saveKV('/api/reply-config',{web_search_enabled:on},on?'联网搜索已开启（下一轮回复起生效）':'联网搜索已关闭（已生效）')}
async function saveAbuseGuard(on){await saveKV('/api/reply-config',{abuse_guard_enabled:on},on?'防滥用防护已开启（已生效）':'防滥用防护已关闭（已生效，仅建议调试时关闭）')}
const _FEAT_NAME={task_bridge_enabled:'任务沙箱',remote_control_enabled:'私聊遥控',draw_enabled:'AI 生图',daily_review_enabled:'夜间自动复盘'};
async function saveFeature(key,on){await saveKVLoad('/api/reply-config',{[key]:on},(_FEAT_NAME[key]||key)+(on?'已开启':'已关闭')+'（已生效）')}
async function saveEcon(key,val){await saveKV('/api/reply-config',{[key]:val},{econ_enabled:'高峰降耗总开关',econ_manual:'开启方式',econ_days:'生效日'}[key]+'已更新（已生效）')}
async function saveEconDays(){const days=[...document.querySelectorAll('.ecday')].filter(x=>x.checked).map(x=>+x.dataset.d);await saveEcon('econ_days',days)}
async function saveEconTime(key,val){if(!val)return;const r=await saveKV('/api/reply-config',{[key]:val},'时段已更新（已生效）');if(r.ok!==false)load()}
async function savePro(key,val){await saveKV('/api/reply-config',{[key]:val},{proactive_groups_enabled:'群主动发消息',proactive_private_enabled:'私聊主动发消息'}[key]+'已更新（已生效）')}
function proCard(kind){
 const iv=Math.round((rvState['proactive_'+kind+'_min_interval_sec']??1800)/60);
 const prob=Math.round((rvState['proactive_'+kind+'_probability']??0.1)*100);
 const sOn=!!(rvState['proactive_'+kind+'_silent_start']||rvState['proactive_'+kind+'_silent_end']);
 return `<div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap;align-items:center">
  <label>周期：每 <input type="number" min="5" max="1440" value="${iv}" style="width:60px" onchange="saveProNum('${kind}_min_interval_sec',(parseInt(this.value)||30)*60,'分钟')"> 分钟一个窗口（5–1440 分钟）</label>
  <label>命中概率（每窗口掷一次）<input type="range" min="0" max="100" value="${prob}" style="width:150px" oninput="this.nextElementSibling.textContent=this.value+'%'" onchange="saveProNum('${kind}_probability',(parseInt(this.value)||0)/100,'概率')"> <b>${prob}%</b></label>
  <label><input type="checkbox" ${sOn?'checked':''} onchange="toggleProSilent('${kind}',this.checked)"> 禁用时段</label>
  ${sOn?`<label><input type="time" value="${esc(rvState['proactive_'+kind+'_silent_start']||'22:00')}" onchange="saveProTime('${kind}_silent_start',this.value)"> – <input type="time" value="${esc(rvState['proactive_'+kind+'_silent_end']||'08:00')}" onchange="saveProTime('${kind}_silent_end',this.value)">（此段内不主动发消息，支持跨午夜）</label>`:''}
 </div><div class="k" style="margin-top:4px">每个窗口只掷一次骰子：命中就发，未中也算这个窗口已过、下个窗口再掷。平均节奏 ≈ 周期 ÷ 概率（如 60 分钟 50% ≈ 平均 2 小时一条）。</div>`}
let rvState={}
async function saveProNum(field,val,what){await saveKVLoad('/api/reply-config',{['proactive_'+field]:val},'主动'+what+'已更新（已生效）')}
async function saveProTime(field,val){if(!val)return;const r=await saveKV('/api/reply-config',{['proactive_'+field]:val},'禁用时段已更新（已生效）');if(r.ok!==false)load()}
async function toggleProSilent(kind,on){
 if(on){await saveKV('/api/reply-config',{['proactive_'+kind+'_silent_start']:'22:00',['proactive_'+kind+'_silent_end']:'08:00'},'禁用时段已开启（默认 22:00–08:00，可修改）')}
 else{await saveKV('/api/reply-config',{['proactive_'+kind+'_silent_start']:'',['proactive_'+kind+'_silent_end']:''},'禁用时段已关闭（全天可发）')}
 load()}
async function rowGmPro(sel){
 const gid=sel.closest('.gm-wrap').querySelector('input.gin').value.trim();
 if(!/^\d{5,12}$/.test(gid)){toast('群号无效：先修正行内群号',false);sel.value='';return}
 const v=sel.value;
 const ov=v==='on'?{proactive:true}:(v==='off'?{proactive:false}:{proactive:null});
 const name=v==='on'?'开启主动发消息':(v==='off'?'关闭主动发消息':'主动发消息跟随全局');
 const r=await saveKV('/api/group-modes',{chat_id:gid,override:ov},'群 '+gid+'：'+name+'（已生效）');
 if(r.ok!==false)load();
}
async function saveGmProNum(gid,f,val){
 if(!/^\d{5,12}$/.test(gid)){toast('群号无效',false);return}
 const r=await saveKV('/api/group-modes',{chat_id:gid,override:{[f]:val}},'该群主动设置已生效');
 if(r.ok!==false)_gmModes[gid]=Object.assign({},_gmModes[gid],{[f]:val});
}
async function saveLimit(key,val){await saveKVLoad('/api/reply-config',{[key]:val},{token_limit_enabled:'token 限额'}[key]+'已更新（已生效）')}
async function saveLimitHours(){const v=parseInt($('#tlhours').value)||1;await saveKVLoad('/api/reply-config',{token_limit_hours:Math.max(1,Math.min(24,v))},'限额窗口已更新（已生效）')}
async function saveLimitMax(){const v=parseInt($('#tlmax').value)||0;await saveKVLoad('/api/reply-config',{token_limit_max:Math.max(1000,v)},'限额上限已更新（已生效）')}
function renderLimitUsage(u){
 const b=document.getElementById('tlimit-badge'),el=document.getElementById('tlimit-usage');if(!el)return;
 el.textContent=u.enabled?('本窗口已用 '+u.used.toLocaleString()+' / '+u.cap.toLocaleString()+' tokens（'+(u.cap?Math.min(100,Math.round(u.used/u.cap*100)):0)+'%）'):'';
 if(b)b.outerHTML=u.hold?'<span id="tlimit-badge" class="badge" style="background:var(--err);color:#fff">已达上限 · 已停响应</span>'
  :'<span id="tlimit-badge" class="badge" style="background:var(--tint);color:var(--dim)">'+(u.enabled?'限额中':'未启用')+'</span>';
}
async function addKey(){
 const label=$('#nklabel').value.trim(),key=$('#nkval').value.trim();
 const provider=$('#nkprov')?$('#nkprov').value:'zhipu';
 const base_url=$('#nkbase')?$('#nkbase').value.trim():'';
 if(!key){toast('请先粘贴 API Key',false);return}
 if(provider==='custom'&&!base_url){toast('自定义供应商需填 Base URL',false);return}
 const r=await saveKV('/api/keys/add',{label,key,provider,base_url},'Key 已添加（第一个自动成为主 Key）');
 if(r.ok!==false){load()}}
async function setKeyProvider(id,prov){
 let base_url='';
 if(prov==='custom'){
  base_url=await dlgPrompt({title:'自定义供应商',body:'填写 OpenAI 兼容 Base URL（http/https，禁止内网地址）：',placeholder:'https://api.xxx.com/v1',okText:'保存'});
  if(!base_url){load();return}}
 const r=await saveKV('/api/keys/rename',{id,provider:prov,base_url},'该 Key 供应商已改为 '+(prov==='zhipu'?'智谱':(prov==='deepseek'?'DeepSeek':'自定义')));
 if(r.ok!==false)load()}
async function delKey(id){
 if(!await dlgConfirm({title:'删除 Key',body:'删除该 Key？模型链中引用它的条目将被自动跳过（可随时重新添加并绑定）。',okText:'删除',danger:true}))return;
 const r=await saveKV('/api/keys/del',{id},'Key 已删除');
 if(r.ok!==false)load()}
async function renameKey(id,el){
 const r=await saveKV('/api/keys/rename',{id,label:el.value.trim()},'备注已更新');
 if(r.ok===false)load()}
async function setPrimary(id){
 const r=await saveKV('/api/keys/primary',{id},'主 Key 已切换（生图/搜索/未指定条目即用新主 Key）');
 if(r.ok!==false)load()}
let _memPage=0,_profPage=0;
const _PAGE=20;
async function vMemory(){
 const [d,mp]=await Promise.all([api('/api/memory'),api('/api/member-profiles').catch(()=>({members:[]}))]);
 window._memData=d.items||[];window._profData=mp.members||[];
 const total=window._memData.length;
 const paged=window._memData.slice(_memPage*_PAGE,(_memPage+1)*_PAGE);
 const rows=paged.map(r=>`<tr><td class="mono">${esc(r.time)}</td><td>${r.scope==='group'?'群':'人'}</td><td class="mono">${esc(String(r.id))}</td><td>${esc(r.text)}</td>
  <td><button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="editMemo(${r.idx})">编辑</button>
  <button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="delMemo(${r.idx})">删除</button></td></tr>`).join('');
 const ptotal=window._profData.length;
 const ppaged=window._profData.slice(_profPage*_PAGE,(_profPage+1)*_PAGE);
 const profs=ppaged.map(p=>`<tr><td>${esc(p.name||'<i>未命名</i>')}</td><td class="mono">${esc(p.qq)}</td><td>${esc(p.note)}</td>
  <td class="k" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc((p.groups||[]).join('、'))}">${esc((p.groups||[]).join('、'))||'—'}</td>
  <td><button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="editProfile('${esc(p.qq)}')">编辑</button>
  <button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="delProfile('${esc(p.qq)}')">删除</button></td></tr>`).join('');
 const pgn=(cur,ttl,fn)=>'<div style="display:flex;gap:6px;align-items:center;margin-top:10px">'+
  '<button class="act ghost" style="margin:0;padding:4px 12px;font-size:12px" '+(cur>0?'':'disabled ')+'onclick="'+fn+'(-1)">上一页</button>'+
  '<span class="k">第 '+(ttl?cur+1:0)+' / '+Math.max(1,Math.ceil(ttl/_PAGE))+' 页 · 共 '+ttl+' 条</span>'+
  '<button class="act ghost" style="margin:0;padding:4px 12px;font-size:12px" '+((cur+1)*_PAGE<ttl?'':'disabled ')+'onclick="'+fn+'(1)">下一页</button></div>';
 $('#view').innerHTML=head('长期记忆流水（自治大脑自动写入；改动即时生效，机器人下一轮对话即用新记忆）')+saveHint('每条记忆可单独编辑/删除（删除需确认）；也可手动新增一条（选择群或联系人）。')+
 `<div class="card">
 <table class="memtbl"><tr><th>时间</th><th>类型</th><th>对象</th><th>内容</th><th>操作</th></tr>${rows||'<tr><td colspan=5 class="k">空</td></tr>'}</table>
 ${pgn(_memPage,total,'memPage')}
 <div style="display:flex;gap:8px;margin-top:10px"><button class="act" onclick="addMemo()">手动新增一条记忆</button>
 <button class="act ghost danger" onclick="clearMem()">清空全部记忆流水</button></div></div>
 <div class="card" style="margin-top:14px"><div class="k" style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">群成员画像<span class="badge" style="background:var(--tint);color:var(--dim)">每日复盘自动更新</span><span class="k">开关在「插件 → 群成员画像」</span></div>
 <div class="k" style="margin-bottom:8px">机器人对每个熟人的长期了解（身份、兴趣、相处注意事项），聊天时自动带上「记得你」的感觉。数据只存本机，本页可随时查看/编辑/删除。</div>
 <table class="proftbl"><tr><th>昵称</th><th>QQ</th><th>画像</th><th>常驻群</th><th>操作</th></tr>${profs||'<tr><td colspan=5 class="k">暂无画像（夜间复盘会自动为活跃成员生成）</td></tr>'}</table>
 ${pgn(_profPage,ptotal,'profPage')}
 <div style="display:flex;gap:8px"><button class="act" onclick="editProfile('')">手动新增画像</button></div></div>`;
}
function memPage(d){_memPage=Math.max(0,_memPage+d);load()}
function profPage(d){_profPage=Math.max(0,_profPage+d);load()}
async function editProfile(qq){
 let cur={name:'',note:''};
 if(qq){const r=await api('/api/member-profiles').catch(()=>({members:[]}));cur=(r.members||[]).find(x=>x.qq===String(qq))||cur;}
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>'+(qq?'编辑群成员画像':'新增群成员画像')+'</h3>'
  +'<div class="dlg-body">画像会注入机器人的上下文：写身份特征、兴趣、称呼习惯、相处注意事项，第三人称，300 字以内。</div>'
  +'<div style="display:flex;gap:8px;margin-top:12px;align-items:center">'
  +'<input class="p-qq" value="'+esc(String(qq||''))+'" placeholder="QQ号" style="width:130px" '+(qq?'disabled':'')+'>'
  +'<input class="p-name" value="'+esc(cur.name||'')+'" placeholder="昵称" style="width:150px">'
  +'</div><textarea class="p-note" rows="4" style="width:100%;margin-top:10px;resize:vertical" maxlength="300" placeholder="如：大学生，常问编程问题，喜欢被叫明哥；给他讲东西要有耐心">'+esc(cur.note||'')+'</textarea>'
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act">保存</button></div></div>';
 document.body.appendChild(m);
 const qi=m.querySelector('.p-qq'),ni=m.querySelector('.p-name'),ta=m.querySelector('.p-note');
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  if(!/^\\d{5,12}$/.test(qi.value.trim())||!ta.value.trim()){toast('QQ 需为 5-12 位数字，画像不能为空',false);return}
  close();
  const r=await saveKV('/api/member-profiles/save',{qq:qi.value.trim(),name:ni.value.trim(),note:ta.value.trim()},'画像已保存（即时生效）');
  if(r.ok!==false)load()};
 m.onclick=e=>{if(e.target===m)close()};
 m.onkeydown=e=>{if(e.key==='Escape')close()};
 setTimeout(()=>{(qq?ta:qi).focus()},30)}
async function delProfile(qq){
 if(!await dlgConfirm({title:'删除画像',body:'删除这位成员的画像？机器人将不再记得对他的了解（夜间复盘如他继续活跃可能会重新生成）。',okText:'删除',danger:true}))return;
 const r=await saveKV('/api/member-profiles/del',{qq:String(qq)},'画像已删除');
 if(r.ok!==false)load()}
async function biliInit(){
 try{
  const s=await api('/api/bili/status');
  const ub=document.getElementById('biliuid'),lb=document.getElementById('bililogin');
  if(!lb)return;
  ub.innerHTML=s.uid?'<span class="badge b-ok">B 站已登录 UID '+esc(String(s.uid))+'</span> <button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="biliLogout()">退出登录</button>':'<span class="badge" style="background:var(--tint);color:var(--dim)">未登录</span>';
  lb.innerHTML=s.uid?'':'<button class="act" onclick="biliScan()">扫码登录 B 站</button>';
 }catch(e){}}
async function biliLogout(){
 if(!await dlgConfirm({title:'退出 B 站',body:'清除本机保存的 B 站登录凭证？机器人将停止刷视频（开关保留）。',okText:'退出',danger:true}))return;
 await api('/api/bili/logout',{method:'POST'});toast('已退出');load()}
async function biliScan(){
 const r=await api('/api/bili/login-start',{method:'POST'}).catch(e=>({ok:false,error:e}));
 if(r.ok===false){toast(r.error||'发起登录失败',false);return}
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>扫码登录 B 站</h3><div class="dlg-body">'
  +(r.qr_png?'<img src="/api/bili/qr?t='+Date.now()+'&tk='+encodeURIComponent(UI_TOKEN)+'" style="width:220px;height:220px">':'二维码生成失败，把下面的链接复制到手机 B 站 App 打开：')
  +'<div class="k mono" style="word-break:break-all;margin-top:8px;font-size:12px">'+esc(r.url)+'</div>'
  +'<div class="k" style="margin-top:8px">用<b>机器人自己的 B 站账号</b>的手机 App 扫码并确认。</div>'
  +'<div id="bilirr" class="k" style="margin-top:6px">等待扫码…</div></div>'
  +'<div class="dlg-btns"><button class="act ghost">关闭</button></div></div>';
 document.body.appendChild(m);
 const close=()=>{clearInterval(timer);m.remove()};
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=e=>{if(e.target===m)close()};
 const timer=setInterval(async()=>{
  try{
   const p=await api('/api/bili/login-poll',{method:'POST',body:JSON.stringify({key:r.key})});
   const el=m.querySelector('#bilirr');
   if(p.state==='ok'){el.textContent='登录成功！UID '+p.uid;clearInterval(timer);setTimeout(()=>{close();load()},800)}
   else if(p.state==='scanned')el.textContent='已扫码，请在手机上确认…';
   else if(p.state==='expired'){el.textContent='二维码已过期，请关闭重试';clearInterval(timer)}
  }catch(e){}
 },2000)}
let _plTab='mine',_plCat='',_mktCat='';
function plCatBar(cats,cur,fn){
 return '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px" id="plcats">'+
  [''].concat(cats).map(c=>'<button class="act '+(c===cur?'':'ghost')+'" style="margin:0;padding:4px 14px;font-size:12.5px" onclick="'+fn+'(\''+esc(c)+'\')">'+(c||'全部')+'</button>').join('')+'</div>';
}
async function vPlugins(){
 if(_plTab==='market'){await vMarket();return}
 const [r,mk]=await Promise.all([
  pwApi('/api/plugins',{plugins:[]}),
  pwApi('/api/plugins/market',{ok:false})]);
 const all=r.plugins||[];
 const mkt={};((mk&&mk.plugins)||[]).forEach(p=>{mkt[p.name]=p});
 window._mktMap=mkt;
 const costs=await plCosts();
 const totalPct=costs.reduce((s,c)=>s+c.pct,0);
 const costByName={};costs.forEach(c=>{costByName[c.name]=c.pct});
 const cats=[...new Set(all.map(p=>p.category||'其他'))].sort();
 const shown=all.filter(p=>!_plCat||p.category===_plCat);
 // 语义化版本比较：市场版本必须【大于】已装版本才提示更新——
 // 只判「不同」会把降级当更新（实测：装了 1.2.4 后市场清单还是 1.2.3，
 // 显示「更新到 v1.2.3」点了真回退）
 function verGt(a,b){
  const pa=String(a||'').split('.'),pb=String(b||'').split('.');
  for(let i=0;i<Math.max(pa.length,pb.length);i++){
   const x=parseInt(pa[i]||'0')||0,y=parseInt(pb[i]||'0')||0;
   if(x!==y)return x>y;
  }
  return false;
 }
 const rows=shown.map(p=>{
  const on=!!p.enabled;
  const m=mkt[p.name];
  const hasUpd=m&&m.version&&p.version&&verGt(m.version,p.version);
  const st='<label style="display:flex;gap:6px;align-items:center;flex:none"><input type="checkbox" '+(on?'checked':'')+' onchange="plToggle(\''+esc(p.name)+'\',this.checked)"> '+(on?'已启用':'已停用')+'</label>';
  const setBtn=(p.page||p.settings&&p.settings.length)?'<button class="act" style="margin:0;padding:5px 16px;font-size:12.5px" onclick="plPage(\''+esc(p.name)+'\')">⚙ 设置</button>':'';
  const uninstall=p.builtin?'':'<button class="act ghost danger" style="margin:0;padding:5px 12px;font-size:12px" onclick="plUninstall(\''+esc(p.name)+'\')">卸载</button>';
  const updbtn=hasUpd?'<button class="act" style="margin:0;padding:5px 12px;font-size:12px" onclick="mktInstall(\''+esc(p.name)+'\',true)">⬆ 更新到 v'+esc(m.version)+'</button>':'';
  return '<div class="card" style="margin-bottom:12px;cursor:pointer" onclick="if(!event.target.closest(\'button,label,input\'))plPage(\''+esc(p.name)+'\')"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:14px">'+esc(p.title||p.name)+'</b>'+(p.version?'<span class="badge" style="background:var(--tint);color:var(--dim)">v'+esc(p.version)+'</span>':'')+'<span class="badge" style="background:var(--tint);color:var(--dim)">'+esc(p.category||'其他')+'</span>'+(p.builtin?'<span class="badge" style="background:var(--tint);color:var(--dim)">内置</span>':'<span class="badge" style="background:var(--tint);color:var(--dim)">已安装</span>')+(costByName[p.name]?plCostBadge(costByName[p.name]):'')+'<span style="margin-left:auto;display:flex;gap:8px;align-items:center">'+updbtn+setBtn+uninstall+st+'</span></div>'+
   '<div class="k" style="margin-top:6px">'+esc(p.description||'')+'</div>'+
   '<div class="k" style="margin-top:6px;opacity:.7">点「⚙ 设置」或本卡片进入该插件的专属设置页 →</div></div>';
 }).join('');
 $('#view').innerHTML=head('插件（可按需启停的功能模块）')+saveHint('启停即时生效；每个插件的全部设置在其专属设置页里（点卡片或 ⚙ 设置进入）。')
 +((window.__pj_voice_reply||'')!==''?'':'')
 +('<div class="card" style="border-color:#f0c36d;background:#fffcf0"><div class="k"><b>📢 语音回复功能已内置</b>——0.5.0 起搬到侧边栏「<a href="#" onclick="goTab(\'voice\');return false">语音消息</a>」页（新增免费本地引擎 GPT-SoVITS）。</div>'
 +'<div class="k" style="margin-top:4px">已装过该插件的会在升级时自动停用（配置共用，「语音消息」页里所有设置原样生效），可在下方直接卸载残留文件。</div></div>')+
 '<div style="display:flex;gap:8px;margin-bottom:14px"><button class="act" style="margin:0" onclick="plTab(\'mine\')">我的插件</button><button class="act ghost" style="margin:0" onclick="plTab(\'market\')">🌐 插件市场</button><span style="flex:1"></span><button class="act ghost" style="margin:0" onclick="plInstall()">从 zip 安装…</button></div>'+
 (cats.length>1?plCatBar(cats,_plCat,'plCatSel'):'')+
 (totalPct>0?'<div class="card" style="margin-bottom:12px"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>消耗总览</b>'+plCostBadge(totalPct)+'<span class="k">按启用插件各自声明的估算口径汇总；点开插件卡片可看单项明细与口径说明</span></div></div>':'')+
 (shown.length?'<div style="margin-top:4px">'+rows+'</div>':'<div class="card">该分类暂无插件</div>');
}
function plCatSel(c){_plCat=c;load()}
function plTab(t){_plTab=t;load()}
/* 插件消耗预估（可选声明）：对启用的插件并发调其 cost route 取 {"pct":数字}，
   汇总成「预计消耗 +N%」。route 调失败/未声明/停用的不计入；任何异常静默
   （消耗徽标是辅助信息，绝不能影响插件列表本身）。 */
async function plCosts(){
 try{
  const r=await api('/api/plugins').catch(()=>({plugins:[]}));
  const cands=(r.plugins||[]).filter(p=>p.enabled&&p.cost_route);
  if(!cands.length)return [];
  const vals=await Promise.all(cands.map(p=>
   api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:p.name,method:p.cost_route,args:{}})})
    .then(v=>({name:p.name,title:p.title||p.name,pct:parseFloat((v&&v.pct)||0)||0}))
    .catch(()=>null)));
  return vals.filter(v=>v&&v.pct>0);
 }catch(e){return []}
}
function plCostBadge(total){
 const n=total.toFixed(1).replace(/\\.0$/,'');
 return '<span class="badge" style="background:#fff4e0;color:#b25e09" title="按各插件声明的估算口径汇总（相对典型上下文约 4.5k token/条回复的增幅）；仅统计启用中的插件，可在各插件设置页查看明细">预计消耗 +'+n+'%</span>';
}
async function vMarket(){
 $('#view').innerHTML=head('插件市场')+'<div style="display:flex;gap:8px;margin-bottom:14px"><button class="act ghost" style="margin:0" onclick="plTab(\'mine\')">← 我的插件</button><button class="act" style="margin:0">🌐 插件市场</button></div><div id="mktbody" class="card">正在拉取市场清单…</div>';
 const r=await api('/api/plugins/market').catch(e=>({ok:false,error:'请求失败'}));
 const el=document.getElementById('mktbody');
 if(!el)return;
 if(r.ok===false){el.innerHTML='市场拉取失败：'+esc(r.error||'')+'<div style="margin-top:8px"><button class="act" onclick="load()">重试</button></div>';return}
 const all=r.plugins||[];
 const cats=[...new Set(all.map(p=>p.category||'其他'))].sort();
 const shown=all.filter(p=>!_mktCat||p.category===_mktCat);
 const rows=shown.map(p=>{
  const has=!!p.installed, upd=has&&p.local_version&&p.version&&p.local_version!==p.version;
  const btn=p.builtin?'<span class="badge" style="background:var(--tint);color:var(--dim)">内置，无需安装</span>':
   (upd?'<button class="act" style="margin:0" onclick="mktInstall(\''+esc(p.name)+'\',true)">更新到 v'+esc(p.version)+'</button>':
   (has?'<span class="badge b-ok">已安装</span> <button class="act ghost" style="margin:0;padding:5px 12px;font-size:12px" onclick="mktInstall(\''+esc(p.name)+'\')">重装</button>':
   '<button class="act" style="margin:0" onclick="mktInstall(\''+esc(p.name)+'\')">安装</button>'));
  return '<div class="card" style="margin-bottom:12px"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:14px">'+esc(p.title||p.name)+'</b><span class="badge" style="background:var(--tint);color:var(--dim)">'+esc(p.category||'其他')+'</span>'+(p.version?'<span class="badge" style="background:var(--tint);color:var(--dim)">v'+esc(p.version)+'</span>':'')+(p.size?'<span class="k">'+(p.size/1024).toFixed(0)+'KB</span>':'')+'<span style="margin-left:auto">'+btn+'</span></div>'+
   '<div class="k" style="margin-top:6px">'+esc(p.description||'')+'</div></div>';
 }).join('');
 el.outerHTML=(cats.length>1?plCatBar(cats,_mktCat,'mktCatSel'):'')+
 (shown.length?rows:'<div class="card">该分类暂无插件</div>')+
 '<div class="card" style="margin-top:4px"><div class="k">把自己写的插件发上来？开发与上架流程见仓库 <b>PLUGINS.md</b>（github.com/Pigeon2333/zcode-qq-bot）</div></div>';
}
function mktCatSel(c){_mktCat=c;load()}
async function mktInstall(name,updating){
 const r=await runTask((updating?'更新':'安装')+'插件 '+name,'/api/plugins/market-install',{name:name});
 if(r.ok===false){toast('失败：'+(r.error||''),false);return}
 toast((r.updated?'已更新':(updating?'已更新':'已安装'))+'「'+(r.title||name)+'」'+(r.updated?'':'（默认停用，可在我的插件里启用）'));
 load();
}
async function plPage(name){
 const r=await api('/api/plugins').catch(()=>({plugins:[]}));
 const p=(r.plugins||[]).find(x=>x.name===name);
 if(!p){toast('插件不存在',false);return}
 // 插件包自带页面脚本：随 zip 分发（版本变化重新注入）——页面改版走插件更新通道
 if(p.page&&p.page_js&&(!window['__pj_'+name]||window['__pj_'+name]!==p.version)){
  const pj=await api('/api/plugins/pagejs?name='+encodeURIComponent(name)).catch(()=>({ok:false}));
  if(pj&&pj.ok&&pj.js){
   // 必须用 <script> 注入：new Function 内的 function 声明是局部作用域，
   // 挂不到 window，宿主后续 window[p.page.js] 会找不到（页面"缺少 xx()"）
   try{
    const s=document.createElement('script');
    s.textContent=pj.js;
    document.body.appendChild(s);
    window['__pj_'+name]=p.version;
   }catch(e){ toast('插件页面脚本加载失败：'+e,false) }
  }
 }
 let data={};
 if(p.page&&p.page.data){data=await api(p.page.data).catch(()=>({}))}
 const fn=p.page?window[p.page.js]:null;
 // 消耗徽标（可选）：详情页头部显示该插件自己的估算
 let costBadge='';
 if(p.enabled&&p.cost_route){
  const v=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({name:name,method:p.cost_route,args:{}})}).catch(()=>null);
  const pct=parseFloat((v&&v.pct)||0)||0;
  if(pct>0)costBadge=plCostBadge(pct);
 }
 $('#view').innerHTML=head('插件 · '+(p.title||name)+'  '+costBadge)+'<div style="margin-top:10px"><button class="act ghost" onclick="goTab(\'plugins\')">← 返回插件列表</button></div><div id="plpage" style="margin-top:12px"></div>';
 // 专属页结构：①插件自带 page（登录等专属界面）②声明式 settings 卡（任何插件都有）
 // 插件页契约（硬规矩）：page 函数必须是同步纯函数——接收预取 data、返回 HTML
 // 字符串；禁止自写 #view（会冲掉宿主注入的「← 返回插件列表」按钮与页面头）。
 // 需要异步数据的走 manifest.page.data 让宿主预取；插入后的动态填充用 setTimeout。
 let inner='';
 if(typeof fn==='function'){
   let html=fn(data,p);
   if(html&&typeof html.then==='function'){
     html.then(h=>{const el=document.getElementById('plpage');if(el&&typeof h==='string')el.innerHTML=h}).catch(()=>{});
     inner+='<div class="card">页面生成中…</div>'
   }else if(typeof html==='string'){inner+=html}
   else{inner+='<div class="card">插件页 '+esc(p.page.js)+'() 必须返回 HTML 字符串（不得自写界面容器）</div>'}
 }
 else if(p.page){inner+='<div class="card">插件设置页加载失败：缺少 '+esc(p.page.js)+'()</div>'}
 if(p.settings&&p.settings.length){
  const rowsS=(p.settings||[]).map(s=>{
   const v=p.values&&p.values[s.key]!==undefined?p.values[s.key]:s.default;
   if(s.type==='toggle')return '<div class="card" style="margin-top:10px"><label class="master"><input type="checkbox" '+(v?'checked':'')+' onchange="plSet(\''+esc(p.name)+'\',\''+esc(s.key)+'\',this.checked)"><span><b>'+esc(s.label||s.key)+'</b></span></label></div>';
   if(s.type==='number')return '<div class="card" style="margin-top:10px"><div class="k">'+esc(s.label||s.key)+'</div><div style="margin-top:6px"><input type="number" value="'+esc(String(v??''))+'" style="width:110px" onchange="plSet(\''+esc(p.name)+'\',\''+esc(s.key)+'\',parseInt(this.value)||0)"></div></div>';
   return '<div class="card" style="margin-top:10px"><div class="k">'+esc(s.label||s.key)+'</div><input value="'+esc(String(v??''))+'" style="width:100%;margin-top:6px" onchange="plSet(\''+esc(p.name)+'\',\''+esc(s.key)+'\',this.value)"></div>';
  }).join('');
  inner+='<div class="card" style="margin-top:12px"><div class="k" style="display:flex;gap:8px;align-items:center"><div class="k" style="font-weight:600">插件设置</div><span class="badge" style="background:var(--tint);color:var(--dim)">✓ 修改后自动保存，即时生效</span></div></div>'+rowsS;
 }
 document.getElementById('plpage').innerHTML=inner||'<div class="card">该插件没有可配置项</div>';
 if(typeof fn==='function'&&typeof window[p.page.js+'Init']==='function'){try{window[p.page.js+'Init'](data,p)}catch(e){}}
}
async function plInstall(){
 const r=await api('/api/plugins/install',{method:'POST'}).catch(e=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'安装失败',false);return}
 toast('已安装/更新「'+(r.title||r.name||'')+'」，请在列表中启用');load();
 if(r.warning){const n=document.getElementById('plnote');if(n)n.textContent=r.warning}}
async function plUninstall(name){
 if(!await dlgConfirm({title:'卸载插件',body:'卸载「'+name+'」？其设置将一并删除（内置插件不可卸载，只能停用）。',okText:'卸载',danger:true}))return;
 const r=await saveKV('/api/plugins/uninstall',{name:name},'插件已卸载');
 if(r.ok!==false)load()}
function biliPageHtml(d,p){
 const uid=(d&&d.uid)||'';
 const items=(d&&d.brief&&d.brief.items)||[];
 const sh=(d&&d.share)||{};
 const ownerUid=(d&&d.owner_uid)||'';
 const brief=items.slice(-6).reverse().map(it=>'<div class="k" style="margin:3px 0">📺 '+esc(String(it.title||'').slice(0,44))+(it.feeling?'<span class="k">（'+esc(String(it.feeling).slice(0,40))+'）</span>':'')+'</div>').join('');
 const tg=(k,label,desc)=>'<div style="margin:6px 0"><label class="master" style="margin:0"><input type="checkbox" '+(sh[k]?'checked':'')+' onchange="biliShareSet(\''+k+'\',this.checked)"><span><b>'+label+'</b><div class="k" style="margin-top:2px">'+desc+'</div></span></label></div>';
 return '<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">B 站账号 '+(uid?'<span class="badge b-ok">已登录 UID '+esc(String(uid))+'</span> <button class="act ghost danger" style="margin:0;padding:4px 12px;font-size:12px" onclick="biliLogout()">退出登录</button>':'<span class="badge" style="background:var(--tint);color:var(--dim)">未登录</span> <button class="act" style="margin:0" onclick="biliScan()">扫码登录 B 站</button>')+'</div>'+
  '<div class="k" style="margin-top:8px">用<b>机器人自己单独注册的 B 站账号</b>扫码（不要用你的大号）。Cookie 只存本机。</div>'+
  '<div style="display:flex;gap:8px;margin-top:10px;align-items:center"><button class="act" style="margin:0" onclick="biliRun()">现在刷一轮</button><span class="k">后台执行，结果看运行日志 AGENT_BILI_*</span></div></div>'+
 '<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:4px">主动分享<span class="badge" style="background:var(--tint);color:var(--dim)">刷到真心觉得好的才发</span></div>'+
  tg('share_enabled','开启主动分享','总开关：关闭时下面全部渠道都不发（挑选与人设/记忆兴趣不受影响）')+
  '<div class="'+(sh.share_enabled?'':'dimmed')+'">'+
  '<div class="k" style="margin:10px 0 2px;font-weight:600">通过 B 站站内私信分享</div>'+
   tg('share_bili_owner','主动给主人分享','开启后按下面的 UID 发站内私信')+
    '<div class="'+(sh.share_bili_owner?'':'dimmed')+'" style="display:flex;gap:8px;margin:4px 0 8px;align-items:center"><input id="pbiliuid" value="'+esc(String(ownerUid))+'" placeholder="主人的 B 站 UID" style="width:170px" onchange="pbUidSave()"></div>'+
   tg('share_bili_others','主动给其他人分享','开启后按下面填的 UID 一起发（想清楚再开：对方会收到机器人的安利）')+
    '<div class="'+(sh.share_bili_others?'':'dimmed')+'" style="margin:4px 0 8px"><input id="pshbiliuids" value="'+esc(String(sh.bili_uids||''))+'" placeholder="其他 B 站 UID（中英文逗号、顿号、空格分隔均可）" style="width:100%" onchange="biliShareSave(\'share_bili_uids\',\'pshbiliuids\')"></div>'+
  '<div class="k" style="margin:10px 0 2px;font-weight:600">通过 QQ 私信分享</div>'+
   tg('share_qq_owner','主动给主人分享','开启后发 QQ 私信给主人（用设置里的主人 QQ 号，无需填写）')+
   tg('share_qq_others','主动给其他人分享','开启后按下面填的 QQ 号一起发')+
    '<div class="'+(sh.share_qq_others?'':'dimmed')+'" style="margin:4px 0 8px"><input id="pshqqu" value="'+esc(String(sh.qq_users||''))+'" placeholder="其他 QQ 号（中英文逗号、顿号、空格分隔均可）" style="width:100%" onchange="biliShareSave(\'share_qq_users\',\'pshqqu\')"></div>'+
  '<div class="k" style="margin:10px 0 2px;font-weight:600">主动分享到 QQ 群</div>'+
   tg('share_qq_group','主动发到群里','开启后按下面的群号发（发群请确认该群欢迎机器人闲聊）')+
    '<div class="'+(sh.share_qq_group?'':'dimmed')+'" style="display:flex;gap:8px;margin:4px 0 8px;align-items:center"><input id="pshqqg" value="'+esc(String(sh.qq_groups||''))+'" placeholder="群号（中英文逗号、顿号、空格分隔均可；留空=默认）" style="flex:1" onchange="biliShareSave(\'share_qq_groups\',\'pshqqg\')"><button class="act ghost" style="margin:0;flex:none" onclick="biliImportGroups()">从白名单群导入</button></div>'+
  '</div>'+
 '</div>'+
 (brief?'<div class="card"><div class="k">最近刷到的见闻（会自然出现在她的聊天里）</div>'+brief+'</div>':'');
}
async function biliShareSet(key,on){await plSet('bili_scroll',key,on);plPage('bili_scroll')}
async function biliShareSave(key,id){
 const v=document.getElementById(id).value.trim();
 if(v&&!v.split(/[,，]/).every(x=>/^\d{1,12}$/.test(x.trim()))){toast('只填数字，逗号分隔',false);load();return}
 await plSet('bili_scroll',key,v)}
async function biliImportGroups(){
 const g=await api('/api/groups').catch(()=>null);
 const ids=((g&&g.groups)||[]).map(String).filter(x=>/^\d+$/.test(x));
 if(!ids.length){toast('白名单里没有群',false);return}
 const el=document.getElementById('pshqqg');
 const cur=el.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
 const merged=[...cur];
 ids.forEach(x=>{if(!merged.includes(x))merged.push(x)});
 el.value=merged.join(',');
 await biliShareSave('share_qq_groups','pshqqg');
 toast('已并入白名单群，共 '+merged.length+' 个')}
async function pbUidSave(){
 const v=document.getElementById('pbiliuid').value.trim();
 if(v&&!/^\d{1,12}$/.test(v)){toast('UID 应为数字',false);return}
 const r=await saveKV('/api/app-config',{owner_bilibili_uid:v},'B 站 UID 已保存');
 if(r.ok!==false)toast('已保存')}
async function plToggle(name,on){
 const r=await saveKV('/api/plugins/toggle',{name:name,enabled:on},'插件已'+(on?'启用':'停用')+'（即时生效）');
 if(r.ok!==false)load()}
async function plSet(name,key,value){
 try{
  const r=await api('/api/plugins/setting',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,key:key,value:value})});
  if(r.ok===false){toast('保存失败: '+(r.error||''),false);return}
  toast('✓ 已保存（即时生效）');
 }catch(e){toast('保存失败：网络错误',false)}}
// ---- 聊天记录导入与学习（QQ/微信共用页，source=qq|wx） ----
function chatImpPage(d,p){
 const src=d&&d.source||'qq', label=(d&&d.label)||'聊天', hint=(d&&d.hint)||'';
 const ss=(d&&d.sessions)||[];
 const pb=(d&&d.probe)||{};
 const rows=ss.map(s=>'<div class="card" style="margin-top:10px"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(s.name)+'</b><span class="badge" style="background:var(--tint);color:var(--dim)">'+s.count+' 条</span>'+(s.range&&s.range[0]?'<span class="k">'+esc(s.range[0])+' ~ '+esc(s.range[1])+'</span>':'')+'<span style="margin-left:auto;display:flex;gap:6px"><button class="act" style="margin:0;padding:4px 14px;font-size:12.5px" onclick="ciLearn(\''+src+'\',\''+esc(s.name)+'\')">✨ 学习成人设</button><button class="act ghost danger" style="margin:0;padding:4px 12px;font-size:12px" onclick="ciDelete(\''+src+'\',\''+esc(s.name)+'\')">删除</button></span></div>'+(s.senders&&s.senders.length?'<div class="k" style="margin-top:4px">说话人：'+esc(s.senders.join('、'))+'</div>':'')+'</div>').join('');
 const envRow=(ok,txt)=>'<div class="k" style="margin:3px 0">'+(ok?'✅ ':'⚠ ')+esc(txt)+'</div>';
 const dirsHtml=(pb.data_dirs||[]).map(x=>'<div class="k mono" style="font-size:12px;margin:3px 0">📂 '+esc(x.label)+'：'+esc(x.path)+'</div>').join('')||'<div class="k">未在常见位置找到聊天记录数据库（可能装在自定义目录）</div>';
 const isQQ=src==='qq';
 const cp=(id,txt)=>'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:11.5px" onclick="ciCopy(\''+id+'\')">复制</button><textarea id="'+id+'" style="position:absolute;left:-9999px;top:0">'+esc(txt)+'</textarea>';
 const wxReady=(pb.wx_skill_installed&&pb.wx_cli_cmd);
 const step2=isQQ
  ?((pb.qq_cli_available
    ?'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">方式 A（推荐）：本机已装 qq-cli，直接导出</div>'+(pb.python_ok===false?'<div class="k" style="color:var(--danger,#c00)">⚠ 未检测到系统 Python（打包版调用 qq-cli 需要）：python.org 安装 Python 3 后重试</div>':'')+'<div style="display:flex;gap:8px;align-items:center"><input id="ci-chat" placeholder="QQ号 / 昵称 / 群名（需先运行过一次 qq_cli.py init 提取密钥）" style="flex:1" onchange=""><button class="act" style="margin:0" onclick="ciQqCliExport()">导出并导入</button></div></div><div class="k" style="margin-top:6px">方式 B：自己用 qq-cli 导出 md/json 后点下面按钮导入</div>'
    :'<div class="k">本机未装 qq-cli 技能——可在 ZCode 里安装 skillhub 的 <b>xujianpeng/qq-cli</b>（装好后回本页即可一键导出），或自己用 qq-cli / 任意工具导出 md/json</div>')
   +'<div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div>'
   +'<div class="k" style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px"><b>方式 C（免导出工具，任何 QQ 版本可用）：</b>在 QQ 里长按选中想让我学的聊天记录 → 合并转发发给机器人，并在消息里说「导入」。<span class="k">例如私聊里发一条合并转发+文字「导入 和小明的聊天」——内容直接进下面的隔离库。每次转发有限条数，记录多可分几次转发（会话名相同会自动编号，学习时可分别点）。</span></div>')
  :(wxReady
    ?'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">方式 A（推荐）：本机 wechat-cli 已就绪，直接导出</div><div style="display:flex;gap:8px;align-items:center"><input id="ci-wxchat" placeholder="联系人备注/昵称 或 群名" style="flex:1" onchange=""><button class="act" style="margin:0" onclick="ciWxCliExport()">导出并导入</button></div></div><div class="k" style="margin-top:6px">首次使用需先提取密钥（微信登录状态下，终端跑一次技能 scripts/extract_keys.py）</div><div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div>'
    :'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">装好下面两步即可解锁「一键导出」（当前：'+(pb.wx_skill_installed?'技能 ✅':'技能 ⚠ 未装')+' · '+(pb.wx_cli_cmd?'命令 ✅':'命令 ⚠ 未装')+'）</div>'
     +'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">① skillhub install wechat-cli --namespace user_514405d2 '+cp('ci-cmd1','skillhub install wechat-cli --namespace user_514405d2')+'</div>'
     +'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">② pip install git+https://github.com/jidechao/py-wechat-cli.git '+cp('ci-cmd2','pip install git+https://github.com/jidechao/py-wechat-cli.git')+'</div>'
     +'<div class="k" style="margin-top:6px">②装完后再登录微信提取一次密钥（技能 scripts/extract_keys.py）；两步都 ✅ 后回本页刷新。装不动也没关系：用任意工具导出 md/txt 后点下面按钮导入。</div>'
     +'<div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div></div>');
 return '<div class="card"><div class="k"><b>① 环境检测</b></div>'
 +envRow(pb.client_running,label+' 客户端正在运行'+(pb.client_running?'':'（导出/读库前请先打开并登录'+label+'）'))
 +'<div class="k" style="margin-top:4px">本机聊天数据目录：</div>'+dirsHtml
 +(isQQ?envRow(pb.qq_cli_available,'qq-cli 技能'+(pb.qq_cli_available?'已安装（位于 ~/.zcode/skills/qq-cli）':'未安装'))
   +envRow(pb.python_ok!==false,'系统 Python（打包版调用技能导出用）'+(pb.python_ok===false?'未检测到（python.org 安装）':'可用'))
   +envRow(pb.qq_key_ready,'QQ 数据库密钥'+(pb.qq_key_ready?'已配置（若导出报解密失败请重新提取）':'未提取——首次导出前需跑一次 init'))
   +(pb.qq_key_ready?'':'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">'+esc(pb.qq_cli_init_cmd||'')+' '+cp('ci-cmdinit',pb.qq_cli_init_cmd||'')+'</div><div class="k" style="font-size:12px;margin:2px 0 6px">保持 QQ 登录状态运行，按提示在 QQ 搜索框输入任意词触发提取（密钥从你登录中的 QQ 进程提取）；数据目录不在默认位置时加 --db-dir 指到 nt_db 所在目录。命令可直接复制粘贴（已含你的用户名绝对路径，PowerShell/cmd 通用）</div>'):envRow(pb.wx_skill_installed,'wechat-cli 技能（skillhub user_514405d2）'+(pb.wx_skill_installed?'已安装':'未安装'))+envRow(!!pb.wx_cli_cmd,'wechat-cli 命令（py-wechat-cli）'+(pb.wx_cli_cmd?'可用：'+esc(pb.wx_cli_cmd):'未安装')))
 +'</div>'
 +'<div class="card"><div class="k"><b>② 导出聊天记录</b></div><div class="k" style="margin-top:6px">'+esc(hint)+'</div>'+step2+'</div>'
 +'<div class="card"><div class="k"><b>③ 隔离库中的会话</b>（导入后在这里，点「学习成人设」让机器人消化）</div>'+(ss.length?rows:'<div class="k" style="margin-top:6px">还没有导入任何会话。</div>')+'</div>'
 +'<div class="k" style="margin-top:6px">隔离库位置：<span class="mono">~/.zcode-qq-bot/chat_import/</span>（不进记忆、不进上下文，只是学习原料）</div>';
}
function qqChatImpPage(d,p){return chatImpPage(Object.assign({},d,{source:'qq',label:'QQ',hint:'见上方方式 A/B'}),p)}
function wxChatImpPage(d,p){return chatImpPage(Object.assign({},d,{source:'wx',label:'微信',hint:'就绪后可直接一键导出；未就绪按第②步两行命令装好后回本页'}),p)}
let _VM=null;
// 插件页契约：page 函数接收宿主预取的 data、返回 HTML 字符串；
// 不允许自写 #view（会把宿主注入的「← 返回插件列表」按钮冲掉）
// （旧的 voicePage 整页构建已删：vVoice/vVoiceCards 重构后成为死代码，
//  曾因其残留的 v-prov 重复 id 干扰批量修改）
function voiceBill(){const P=_VM.providers||{};const k=document.getElementById('v-prov').value;const el=document.getElementById('v-bill');el.textContent=P[k]?P[k].billing:''}
// 配置字段即改即存（该卡所有字段共用；Key/目录/端口/音色类填完失焦自动保存）
async function voiceFieldSave(extra,note){
 const v=Object.assign(voiceCollect(),extra||{});
 const r=await api('/api/voice/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:v})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('保存失败：'+((r&&r.error)||''),false);return}
 if(r.config)_VM.config=r.config;
 toast(note||'已保存（即时生效）');
}
function voiceProvChange(){
 voiceBill();
 const c=_VM.config||{};const k=document.getElementById('v-prov').value;
 const box=document.getElementById('v-provbox');
 if(k==='gsv'){
  box.innerHTML='<div class="k" style="margin-bottom:6px">音色管理在下方<b>「GPT-SoVITS 本地引擎」</b>卡的音色列表里（启用/试听/删除），此处不重复展示。</div>'}
 else if(k==='edge'){box.innerHTML='<div class="k" style="margin-bottom:4px">音色（微软公共接口，免费）</div><select id="v-ev" onchange="voiceFieldSave({edge_voice:this.value},\'Edge 音色已保存（即时生效）\')">'+edgeOptsHtml()+'</select>'}
 else if(k==='zhipu'){
  box.innerHTML='<div class="k" style="margin-bottom:4px">智谱 API Key（留空 = 复用模型管理里的智谱 Key）</div>'
  +'<input id="v-zkey" type="password" value="'+esc(String(c.zhipu_key||''))+'" placeholder="留空自动复用" style="max-width:420px" onchange="voiceFieldSave({zhipu_key:this.value.trim()},\'智谱 Key 已保存（即时生效；留空=复用模型管理的智谱 Key）\')">'
  +'<input type="hidden" id="v-zv" value="'+esc(String(c.zhipu_voice||'tongtong'))+'">'
  +'<input type="hidden" id="v-zid" value="'+esc(String(c.zhipu_speaker_id||''))+'">'
  +'<div id="zv-list" style="margin-top:12px"></div>'
  +'<div style="margin-top:12px;border:1px dashed var(--line);border-radius:8px;padding:12px">'
  +'<div class="k" style="margin-bottom:8px"><b>克隆新音色</b><span class="k">（逐步引导 · 克隆与合成都按智谱计费）</span></div>'
  +'<div id="vc-step1">'
  +'<div class="k" style="margin-bottom:6px"><b>第 1 步 · 选择素材</b><span class="k">（支持 mp4/mov/mp3/m4a/wav；智谱限 3-30 秒、≤10MB，页面会自动截取转码）</span></div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
  +'<input type="file" id="v-file" accept="audio/*,video/*" style="flex:1;min-width:180px" onchange="vcStep1Done()">'
  +'<label class="k">起始 <input id="v-start" type="number" min="0" step="1" value="0" style="width:58px"> 秒</label>'
  +'<label class="k">时长 <input id="v-dur" type="number" min="3" max="30" step="1" value="12" style="width:52px"> 秒</label></div>'
  +'<input id="v-sample" placeholder="可选：这段音频里说的话（填了克隆更像）" style="width:100%;margin-top:6px">'
  +'</div>'
  +'<div id="vc-step2" class="dimmed" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'
  +'<div class="k" style="margin-bottom:6px"><b>第 2 步 · 开始克隆</b><span class="k">（上传样本到智谱并复刻音色，约 10-30 秒）</span></div>'
  +'<button class="act" style="margin:0" onclick="voiceClone()">开始克隆</button>'
  +'</div>'
  +'<div id="vc-prog" style="display:none;margin-top:10px">'
  +'<div style="display:flex;gap:8px;align-items:center"><div style="flex:1;height:4px;background:var(--tint);border-radius:2px;overflow:hidden"><div id="vc-bar" style="height:100%;width:0%;background:var(--btn);transition:width .4s"></div></div><span class="k" id="vc-pct" style="flex:none;min-width:36px;text-align:right">0%</span></div>'
  +'<div class="k" id="v-cmsg" style="margin-top:4px">准备中…</div></div>'
  +'<div id="vc-step3" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'
  +'<div class="k" style="margin-bottom:6px"><b>第 3 步 · 试听效果</b><span class="k">（用新音色合成了一句，听听像不像）</span></div>'
  +'<div class="k" id="vc-step3msg">合成试听中…</div></div>'
  +'<div id="vc-step4" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'
  +'<div class="k" style="margin-bottom:6px"><b>第 4 步 · 满意就命名保存</b></div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
  +'<input id="v-vname" placeholder="音色名（如 爱音）" style="width:130px">'
  +'<button class="act" style="margin:0" onclick="zvCloneSave()">保存到音色列表</button>'
  +'<button class="act ghost danger" style="margin:0" onclick="zvCloneDiscard()">不满意，放弃重来</button></div></div></div>'}
 else if(k==='siliconflow'){box.innerHTML='<div style="display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">硅基流动 API Key</div><input id="v-sfk" type="password" value="'+esc(String(c.sf_key||''))+'" onchange="voiceFieldSave({sf_key:this.value.trim()},\'硅基流动 Key 已保存（即时生效）\')"></div>'
  +'<div><div class="k" style="margin-bottom:4px">模型</div><select id="v-sfm" onchange="voiceFieldSave({sf_model:this.value},\'模型已保存（即时生效）\')">'+sfMOptsHtml()+'</select></div>'
  +'<div><div class="k" style="margin-bottom:4px">音色</div><select id="v-sfv" onchange="voiceFieldSave({sf_voice:this.value},\'音色已保存（即时生效）\')">'+sfVOptsHtml()+'</select></div></div>'}
 else{box.innerHTML='<div style="display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">接口地址（https，含 /v1/）</div><input id="v-cb" value="'+esc(String(c.custom_base||''))+'" placeholder="https://api.openai.com/v1/" onchange="voiceFieldSave({custom_base:this.value.trim()},\'接口地址已保存（即时生效）\')"></div>'
  +'<div><div class="k" style="margin-bottom:4px">API Key</div><input id="v-ck" type="password" value="'+esc(String(c.custom_key||''))+'" onchange="voiceFieldSave({custom_key:this.value.trim()},\'Key 已保存（即时生效）\')"></div>'
  +'<div><div class="k" style="margin-bottom:4px">模型</div><input id="v-cm" value="'+esc(String(c.custom_model||'tts-1'))+'" onchange="voiceFieldSave({custom_model:this.value.trim()},\'模型已保存（即时生效）\')"></div>'
  +'<div><div class="k" style="margin-bottom:4px">音色</div><input id="v-cv" value="'+esc(String(c.custom_voice||'alloy'))+'" onchange="voiceFieldSave({custom_voice:this.value.trim()},\'音色已保存（即时生效）\')"></div></div>'}
 // 引擎卡折叠：只在 GPT-SoVITS 供应商下展开
 const gwrap=document.getElementById('gsv-card-wrap');
 if(gwrap)gwrap.style.display=(k==='gsv')?'':'none';
 const gteaser=document.getElementById('gsv-teaser-wrap');
 if(gteaser)gteaser.style.display=(k==='gsv')?'':'none';
 zvRender();
}
function edgeOptsHtml(){return(_VM.edge_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).edge_voice||'zh-CN-XiaoxiaoNeural')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
// 智谱音色列表（统一：预设 + 克隆都在一个列表；使用中高亮不消失）
function zvRender(){
 const box=document.getElementById('zv-list');
 if(!box)return;
 const c=_VM.config||{};
 const curSid=String(c.zhipu_speaker_id||'');
 const curPre=String(c.zhipu_voice||'tongtong');
 const useClone=!!curSid;
 const clones=c.zhipu_saved_voices||[];
 const rows=[];
 (_VM.zhipu_voices||[]).forEach(p=>{rows.push({name:p[0],tag:'预设',val:p[1],active:!useClone&&p[1]===curPre,clone:false})});
 clones.forEach(v=>{rows.push({name:v.name||'(未命名)',tag:'克隆',sid:String(v.speaker_id||''),active:useClone&&String(v.speaker_id)===curSid,clone:true})});
 box.innerHTML='<div class="k" style="margin-bottom:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:15px">音色列表</b>'
  +'<span class="badge" style="background:var(--tint);color:var(--dim)">预设 '+rows.filter(r=>!r.clone).length+' · 克隆 '+clones.length+'</span>'
  +'<span class="k">点「启用」给机器人用，使用中的音色会高亮标记</span></div>'
  +'<div style="display:grid;gap:6px">'+rows.map(r=>
   '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 10px;border:1.5px solid '+(r.active?'#1a7f37':'var(--line)')+';border-radius:8px;background:'+(r.active?'#f0faf0':'transparent')+'">'
   +'<b style="min-width:56px">'+esc(r.name)+'</b>'
   +'<span class="badge" style="'+(r.clone?'background:#fff4e0;color:#b25e09':'background:var(--tint);color:var(--dim)')+'">'+r.tag+'</span>'
   +(r.active?'<span class="badge" style="background:#1a7f37;color:#fff">✓ 使用中</span>':'')
   +(r.clone&&r.sid?'<span class="k mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px" title="'+esc(r.sid)+'">'+esc(r.sid.slice(0,16))+'…</span>':'')
   +'<span style="margin-left:auto;display:flex;gap:6px">'
   +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="zvPreview(\''+esc(r.clone?r.sid:'')+'\',\''+esc(r.clone?'':r.val)+'\')">试听</button>'
   +(r.active?'':'<button class="act" style="margin:0;padding:3px 12px;font-size:12px" onclick="zvUse(\''+esc(r.clone?r.sid:r.val)+'\','+(r.clone?'true':'false')+',\''+esc(r.name)+'\')">启用</button>')
   +(r.clone?'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="zvDel(\''+esc(r.sid)+'\',\''+esc(r.name)+'\')">移除</button>':'')
   +'</span></div>').join('')+'</div>'
  +'<div class="k" style="margin-top:6px">「移除」只删本地列表条目；平台侧音色在 bigmodel.cn 控制台管理。预设不删、随官方更新。</div>';
}
async function zvUse(id,isClone,name){
 const zv=document.getElementById('v-zv'),zid=document.getElementById('v-zid');
 if(isClone){if(zid)zid.value=id;}
 else{if(zv)zv.value=id;if(zid)zid.value='';}
 const r=await api('/api/voice/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:Object.assign(voiceCollect(),{zhipu_voice:(zv?zv.value:((_VM.config||{}).zhipu_voice||'tongtong')),zhipu_speaker_id:(zid?zid.value:'')})})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('启用失败：'+((r&&r.error)||''),false);return}
 if(r.config)_VM.config=r.config;
 toast('已启用「'+name+'」——机器人语音现在用这个音色');
 zvRender();
}
async function zvPreview(sid,pval){
 toast('合成试听中…（按智谱计费）');
 const vv={provider:'zhipu'};
 if(sid)vv.zhipu_speaker_id=sid;else vv.zhipu_voice=pval;
 const r=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:vv,text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('试听失败：'+((r&&r.error)||''),false);return}
 const au=document.getElementById('v-audio');
 au.src=r.audio;au.style.display='block';au.play().catch(()=>{});
}
async function zvDel(sid,name){
 if(!await dlgConfirm({title:'从列表移除「'+name+'」？',body:'仅删除本地列表条目，不影响智谱平台侧的音色。',danger:true,okText:'移除'}))return;
 await api('/api/voice/zvoice-del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speaker_id:sid})}).catch(()=>({}));
 if(_VM.config)_VM.config.zhipu_saved_voices=((_VM.config.zhipu_saved_voices)||[]).filter(v=>String(v.speaker_id)!==sid);
 toast('已移除「'+name+'」');
 zvRender();
}
function zzOptsHtml(){return(_VM.zhipu_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).zhipu_voice||'tongtong')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfMOptsHtml(){return(_VM.sf_models||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).sf_model||'FunAudioLLM/cosyvoice2-0.5B')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfVOptsHtml(){return(_VM.sf_voices||[]).map(v=>'<option value="'+esc(v)+'" '+(v===((_VM.config||{}).sf_voice||'anna')?'selected':'')+'>'+esc(v)+'</option>').join('')}
function voiceCollect(){
 const k=document.getElementById('v-prov').value;
 const mode=(document.querySelector('input[name="vmode"]:checked')||{}).value||'keyword';
 const v={enabled:document.getElementById('v-on').checked,provider:k,mode,
  private_enabled:document.getElementById('v-priv').checked,
  group_enabled:document.getElementById('v-grp').checked,
  with_text:document.getElementById('v-wt').checked,
  keywords:document.getElementById('v-kw').value.split(/[、,，]/).map(x=>x.trim()).filter(Boolean),
  prob:(parseFloat(document.getElementById('v-prob').value)||0)/100};
 const g=id=>{const el=document.getElementById(id);return el?el.value:''};
 // 跨供应商字段：当前供应商的表单不存在时沿用已存配置——切到别家再
 // 「保存设置」不该把这家选好的音色/Key 清掉（曾把 edge/zhipu 音色抹回默认）
 const gv=(id,cfgKey,dft)=>{const el=document.getElementById(id);return el?el.value:String(((_VM.config||{})[cfgKey])??dft??'')};
 v.edge_voice=gv('v-ev','edge_voice','zh-CN-XiaoxiaoNeural');
 v.zhipu_voice=gv('v-zv','zhipu_voice','tongtong');
 v.zhipu_speaker_id=gv('v-zid','zhipu_speaker_id','');
 // Key fallback：表单字段可能还没渲染（首次加载 zhipu 表单区时序问题），
 // 读不到就用已保存的配置值（用户已保存过 Key 的话）
 v.zhipu_key=g('v-zkey')||((_VM.config||{}).zhipu_key||'');
 v.sf_key=g('v-sfk')||((_VM.config||{}).sf_key||'');
 v.sf_model=gv('v-sfm','sf_model','FunAudioLLM/cosyvoice2-0.5B');
 v.sf_voice=gv('v-sfv','sf_voice','anna');
 v.custom_base=g('v-cb')||((_VM.config||{}).custom_base||'');
 v.custom_key=g('v-ck')||((_VM.config||{}).custom_key||'');
 v.custom_model=gv('v-cm','custom_model','tts-1');
 v.custom_voice=gv('v-cv','custom_voice','alloy');
 v.gsv_dir=gv('v-gdir','gsv_dir','');v.gsv_port=gv('v-gport','gsv_port','9880');
 v.gsv_voice_id=gv('v-gvid','gsv_voice_id','');   // v-gvid 已移除（音色管理归引擎卡），恒走回退
 v.gsv_batch_size=gv('v-gbatch','gsv_batch_size','1');
 v.gsv_speed=gv('v-gspeed','gsv_speed','1');
 v.gsv_split=gv('v-gsplit','gsv_split','cut0');
 v.gsv_keepalive=document.getElementById('v-gkeep')?document.getElementById('v-gkeep').checked:((_VM.config||{}).gsv_keepalive!==false);
 v.gsv_ref_wav=gv('v-gref','gsv_ref_wav','');v.gsv_prompt_text=gv('v-gptext','gsv_prompt_text','');v.gsv_prompt_lang=gv('v-gplang','gsv_prompt_lang','zh');
 return v}
async function voiceSave(){
 const r=await api('/api/voice/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect()})}).catch(()=>({ok:false,error:'请求失败'}));
 const el=document.getElementById('v-ok');
 if(r.ok===false){toast(r.error||'保存失败',false);if(el)el.textContent='保存失败：'+(r.error||'');return}
 _VM.config=r.config||voiceCollect();toast('语音设置已保存（即时生效）');if(el)el.textContent='已保存 ✓'}
async function voicePreview(){
 const msg=document.getElementById('v-pmsg');const au=document.getElementById('v-audio');
 msg.textContent='合成中…（免费供应商约 1-3 秒）';au.style.display='none';
 const r=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect(),text:document.getElementById('v-ptext').value})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){msg.textContent='试听失败：'+(r.error||'');return}
 au.src=r.audio;au.style.display='block';au.play().catch(()=>{});
 msg.textContent='试听音频 '+((r.size||0)/1024).toFixed(0)+' KB（每次试听按该供应商计费规则计费）'}
// ---- 一键克隆：浏览器解码 → 截取 → 16k 单声道 WAV → 上传智谱 ----
function vcB64(u8){let s='';const CH=0x8000;for(let i=0;i<u8.length;i+=CH){s+=String.fromCharCode.apply(null,u8.subarray(i,i+CH))}return btoa(s)}
function vcWav(ab,start,dur){
 const sr=16000;
 return new Promise((res,rej)=>{
  let off;try{off=new OfflineAudioContext(1,Math.max(1,Math.ceil(dur*sr)),sr)}catch(e){rej(e);return}
  const src=off.createBufferSource();src.buffer=ab;src.connect(off.destination);
  try{src.start(0,Math.max(0,start),dur)}catch(e){rej(e);return}
  off.startRendering().then(rendered=>{
   const f=rendered.getChannelData(0);const buf=new ArrayBuffer(44+f.length*2);const dv=new DataView(buf);
   const ws=(o,s)=>{for(let i=0;i<s.length;i++)dv.setUint8(o+i,s.charCodeAt(i))};
   ws(0,'RIFF');dv.setUint32(4,36+f.length*2,true);ws(8,'WAVE');ws(12,'fmt ');
   dv.setUint32(16,16,true);dv.setUint16(20,1,true);dv.setUint16(22,1,true);
   dv.setUint32(24,sr,true);dv.setUint32(28,sr*2,true);dv.setUint16(32,2,true);dv.setUint16(34,16,true);
   ws(36,'data');dv.setUint32(40,f.length*2,true);
   let o=44;for(let i=0;i<f.length;i++){const v=Math.max(-1,Math.min(1,f[i]));dv.setInt16(o,v<0?v*0x8000:v*0x7FFF,true);o+=2}
   res(new Uint8Array(buf));
  }).catch(rej);
 })}
let __ZVSID=null;   // 刚克隆成功、待命名保存的智谱音色 speaker_id
// ---- 引导式克隆向导（智谱）----
function vcStep1Done(){
 const f=(document.getElementById('v-file').files||[])[0];
 const s2=document.getElementById('vc-step2');
 if(s2){s2.style.display=f?'block':'none';s2.classList.toggle('dimmed',!f)}
}
let _vcProgTimer=null;
function vcProgStart(){          // 进度条：阶段推进 + 区间内缓慢爬升
 const box=document.getElementById('vc-prog');if(!box)return;
 box.style.display='block';
 const bar=document.getElementById('vc-bar'),pct=document.getElementById('vc-pct');
 if(!bar||!pct)return;
 bar.style.width='0%';pct.textContent='0%';
 if(_vcProgTimer)clearInterval(_vcProgTimer);
 let cur=0,target=10;
 _vcProgTimer=setInterval(()=>{
  if(cur<target)cur=Math.min(target,cur+Math.random()*3);
  bar.style.width=cur.toFixed(0)+'%';pct.textContent=cur.toFixed(0)+'%';
 },400);
}
function vcProgStage(target,label){  // 切阶段：设新目标上限+文案
 const bar=document.getElementById('vc-bar'),pct=document.getElementById('vc-pct'),msg=document.getElementById('v-cmsg');
 if(bar)bar.dataset.target=target;
 if(msg&&label)msg.textContent=label;
 vcProgTarget=target;
}
let vcProgTarget=10;
function vcProgDone(ok){        // 收尾：成功到 100%，失败变红
 if(_vcProgTimer){clearInterval(_vcProgTimer);_vcProgTimer=null}
 const bar=document.getElementById('vc-bar'),pct=document.getElementById('vc-pct');
 if(bar&&pct){
  if(ok===true){bar.style.width='100%';pct.textContent='100%'}
  else if(ok===false){bar.style.background='var(--err)'}
 }
}
async function voiceClone(){
 const msg=document.getElementById('v-cmsg');
 const f=(document.getElementById('v-file').files||[])[0];
 if(!f){msg.textContent='先在第 1 步选择一个音频或视频文件';return}
 if(f.size>120*1024*1024){msg.textContent='文件太大（超过 120MB），请先剪出 10-30 秒再上传';return}
 const start=Math.max(0,parseFloat(document.getElementById('v-start').value)||0);
 const dur=Math.min(30,Math.max(3,parseFloat(document.getElementById('v-dur').value)||12));
 vcProgStart();
 vcProgStage(15,'解码音频中…（长视频需要几秒）');
 let wav;
 try{
  const buf=await f.arrayBuffer();
  const Ctx=window.AudioContext||window.webkitAudioContext;
  const ac=new Ctx();
  const ab=await ac.decodeAudioData(buf);
  if(ac.close)ac.close();
  if(!ab||!ab.duration){vcProgDone(false);msg.textContent='解码失败：浏览器读不出这段音频';return}
  if(ab.duration<start+1){vcProgDone(false);msg.textContent='起始位置超出音频时长（该文件约 '+Math.round(ab.duration)+' 秒）';return}
  vcProgStage(25,'截取 '+dur+' 秒并转 WAV…');
  wav=await vcWav(ab,start,dur);
 }catch(e){vcProgDone(false);msg.textContent='音频解码失败：'+((e&&e.message)||e)+'。换 mp3/wav/m4a 试试，或先用工具剪一段。';return}
 vcProgStage(70,'上传样本并克隆中…（约 10-30 秒，按智谱计费）');
 const r=await api('/api/voice/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect(),audio:vcB64(wav),sample_text:document.getElementById('v-sample').value,preview_text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){
  vcProgDone(false);
  const e=String((r&&r.error)||'未知错误');
  if(e.indexOf('1113')>=0||e.indexOf('余额')>=0){
   msg.textContent='克隆失败：所有已配置的智谱 Key 都没有克隆额度（1113 余额不足）。可到 bigmodel.cn 充值后重试，或改用 GPT-SoVITS 本地引擎克隆（完全免费）——引擎卡就在本页下方。';return}
  if(e.indexOf('1234')>=0||e.indexOf('网络错误')>=0||e.indexOf('HTTP 5')>=0){
   msg.textContent='克隆失败：平台处理这段样本时出错（多为样本本身或平台抖动）。已自动换 Key 重试过——请换一段 3-30 秒的清晰人声（单一说话人、无背景音乐）再试；仍失败可稍后再试，或先用本地引擎克隆。';return}
  msg.textContent='克隆失败：'+e;return}
 __ZVSID=r.voice_id;
 vcProgDone(true);
 const s3=document.getElementById('vc-step3');
 if(s3){s3.style.display='block'}
 const s3m=document.getElementById('vc-step3msg');
 if(s3m)s3m.textContent='✓ 克隆成功，正在用新音色合成试听…';
 const pr=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:{provider:'zhipu',zhipu_speaker_id:__ZVSID},text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false}));
 const s4=document.getElementById('vc-step4');
 if(pr&&pr.ok!==false){
  const au=document.getElementById('v-audio');
  au.src=pr.audio;au.style.display='block';au.play().catch(()=>{});
  if(s3m)s3m.textContent='✓ 试听播放中（上方播放器可反复听）。满意就进入第 4 步；不满意可回第 1 步调整片段重来（会再次计费）。';
  if(s4){s4.style.display='block'}
  const ne=document.getElementById('v-vname');if(ne)ne.focus();
 }else{
  if(s3m)s3m.textContent='✓ 克隆完成（试听失败：'+((pr&&pr.error)||'网络问题')+'）。可稍后在音色列表里试听。';
  if(s4){s4.style.display='block'}
 }
}
async function zvCloneDiscard(){
 __ZVSID=null;
 ['vc-step2','vc-step3','vc-step4','vc-prog'].forEach(function(id){const el=document.getElementById(id);if(el)el.style.display='none'});
 const s2=document.getElementById('vc-step2');
 const au=document.getElementById('v-audio');if(au){try{au.pause()}catch(e){}au.style.display='none';au.removeAttribute('src')}
 const f=document.getElementById('v-file');if(f)f.value='';
 const n=document.getElementById('v-vname');if(n)n.value='';
 if(s2){s2.classList.add('dimmed')}
 vcStep1Done();
 const msg=document.getElementById('v-cmsg');if(msg)msg.textContent='';
 toast('已放弃本次克隆（不保存）。重新选素材即可再来（会再次计费）');
}
async function zvCloneSave(){
 const msg=document.getElementById('v-cmsg');
 const name=(document.getElementById('v-vname').value||'').trim();
 if(!__ZVSID){toast('先完成第 2 步克隆，成功后再保存',false);return}
 if(!name){toast('先填音色名再保存',false);return}
 const r=await api('/api/voice/zvoice-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,speaker_id:__ZVSID})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){msg.textContent='保存失败：'+((r&&r.error)||'');return}
 __ZVSID=null;
 ['vc-step2','vc-step3','vc-step4','vc-prog'].forEach(function(id){const el=document.getElementById(id);if(el)el.style.display='none'});
 const m=await api('/api/voice/meta').catch(()=>null);
 if(m&&m.ok!==false&&m.config)_VM.config=m.config;
 if(msg)msg.textContent='';
 toast('✓ 已保存「'+name+'」到音色列表，在列表里「启用」即可给机器人用');
 zvRender();
}
// ---------------- 语音消息页（0.5.0 内置；原语音回复插件功能整体搬迁至此） ----------------
let _VM2=null;
function vGsvCardTeaser(){
 // 未安装：不显示大卡，只留一行可展开入口（含环境检测）
 return '<div class="card" style="border-style:dashed"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
  +'<b>GPT-SoVITS 本地引擎</b><span class="badge" style="background:var(--tint);color:var(--dim)">可选 · 完全免费 · 需 NVIDIA 显卡</span>'
  +'<button class="act ghost" style="margin:0;margin-left:auto" onclick="gsvExpandSetup(this)">了解如何安装</button></div>'
  +'<div id="gsv-setup" style="display:none;margin-top:10px"><div class="k" id="gsv-env">环境检测中…</div>'
  +'<div id="gsv-steps" style="display:none">'
  +'<div class="k" style="margin-top:8px">① <b>下载整合包</b>（约 6.4GB）：<a href="https://hf-mirror.com/lj1995/GPT-SoVITS-windows-package/resolve/main/GPT-SoVITS-v3lora-20250228.7z?download=true" target="_blank">国内镜像直链</a> 或 <a href="https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e/dkxgpiy9zb96hob4#KTvnO" target="_blank">官方网盘列表</a></div>'
  +'<div class="k" style="margin-top:6px">② 用 <a href="https://www.7-zip.org" target="_blank">7-Zip</a> 解压到 <b class="mono">E:\\GPTSoVITS\\</b>（放其他盘也行，装好后可在引擎卡里填目录）</div>'
  +'<div class="k" style="margin-top:6px">③ 解压完刷新本页——引擎卡会自动出现，点「启动引擎」即可（首次加载约 1 分钟）。</div>'
  +'<div class="k" style="margin-top:6px">合成完全免费（本机 GPU 推理）；没有 NVIDIA 显卡建议用云端的智谱 GLM-TTS。</div></div></div></div>';
}
 async function gsvExpandSetup(btn){
 btn&&(btn.disabled=true);
 const box=document.getElementById('gsv-setup');box&&(box.style.display='block');
 const env=document.getElementById('gsv-env');
 try{
  const r=await api('/api/voice/gsv/env',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(r&&r.ok){
   env.innerHTML=r.has_nvidia
    ?('✓ 检测到 '+esc(r.gpu)+'（显存 '+(r.vram_mb||0)+'MB）'+(r.ok?' —— 可以安装。':' —— 显存偏小，可用但偶尔会回退。'))
    :('⚠ '+esc((r.notes&&r.notes[0])||'未检测到 NVIDIA 显卡。'));
  }else{env.textContent='环境检测失败（不影响手动安装尝试）';}
 }catch(e){env.textContent='环境检测失败（不影响手动安装尝试）';}
 const st=document.getElementById('gsv-steps');st&&(st.style.display='block');
 btn&&btn.parentElement&&btn.remove();
}
// 引擎自启动加载期徽标轮询：8s 探一次 meta，就绪即变「运行中」并 toast
// （只改徽标不动表单——整页重渲染会丢用户正在填的输入）
let _gsvPollTimer=null,_gsvPollT0=0;
function gsvBadgePoll(m){
 if(_gsvPollTimer){clearInterval(_gsvPollTimer);_gsvPollTimer=null}
 const g=(m&&m.gsv)||{},c=(m&&m.config)||{};
 if(g.running||!c.gsv_autostart||!g.dir_ok)return;
 _gsvPollT0=Date.now();
 _gsvPollTimer=setInterval(async()=>{
  const r=await api('/api/voice/meta').catch(()=>null);
  const gg=(r&&r.gsv)||{};
  const el=document.getElementById('gsv-badge');
  if(!el){if(_gsvPollTimer){clearInterval(_gsvPollTimer);_gsvPollTimer=null}return}
  if(gg.running){
   if(_gsvPollTimer){clearInterval(_gsvPollTimer);_gsvPollTimer=null}
   el.style.background='#e6f7e6';el.style.color='#1a7f37';
   el.textContent='● 运行中（端口 '+gg.port+'）';
   toast('✓ 语音引擎已就绪（本地合成可用）');
  }else{
   const secs=Math.round((Date.now()-_gsvPollT0)/1000);
   el.textContent='◐ 启动中…（模型加载约 40-90 秒，已等 '+secs+' 秒）';
  }
 },8000);
}
function vGsvCardInstalled(m,g){
 const c=m.config||{};
 // 自启动开启但端口未就绪 = 大概率正在加载（模型加载 40-90 秒）——
 // 此前这段窗口显示「已安装 · 未启动」，用户以为自启没生效（实测误解）
 const badge=g.running?'<span class="badge" id="gsv-badge" style="background:#e6f7e6;color:#1a7f37">● 运行中（端口 '+g.port+'）</span>'
  :(c.gsv_autostart?'<span class="badge" id="gsv-badge" style="background:#fff7e0;color:#8a6100">◐ 启动中…（模型加载约 40-90 秒，本页会自动刷新状态）</span>'
  :'<span class="badge" id="gsv-badge" style="background:var(--tint);color:var(--dim)">已安装 · 未启动</span>');
 return '<div class="card" id="gsv-card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"><b>GPT-SoVITS 本地引擎</b>'+badge
  +'<span style="margin-left:auto;display:flex;gap:8px">'
  +(g.running?'<button class="act ghost" style="margin:0" onclick="gsvStop()">停止引擎</button>':'<button class="act" style="margin:0" onclick="gsvStart()">启动引擎</button>')
  +'<button class="act ghost" style="margin:0" onclick="vVoice()">刷新状态</button></span></div>'
  +'<div class="k">整合包：'+esc(g.dir)+'</div>'
  +'<div class="k" style="margin-top:6px">引擎没开时合成会自动回退其他来源（不丢回复）。'
  +'<label style="margin-left:8px"><input type="checkbox" id="v-gauto" '+(c.gsv_autostart?'checked':'')+' onchange="gsvAutoSet(this.checked)"> 随应用自动启动引擎</label>'
  +(c.gsv_autostart?'<span class="k">（已开启）</span>':'')+'</div>'
  +'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px">'
  +'<div><div class="k" style="margin-bottom:4px">参考语言（参考音频说的语言）</div><select id="v-gplang" onchange="voiceFieldSave({gsv_prompt_lang:this.value},\'参考语言已保存（即时生效）\')">'
  +'<option value="zh"'+((c.gsv_prompt_lang||'zh')==='zh'?' selected':'')+'>中文</option>'
  +'<option value="ja"'+((c.gsv_prompt_lang)==='ja'?' selected':'')+'>日文</option>'
  +'<option value="en"'+((c.gsv_prompt_lang)==='en'?' selected':'')+'>英文</option></select></div>'
  +'<div><div class="k" style="margin-bottom:4px">整合包目录（默认自动探测 E:\\GPTSoVITS）</div><input id="v-gdir" value="'+esc(String(c.gsv_dir||''))+'" placeholder="留空自动探测" onchange="voiceFieldSave({gsv_dir:this.value.trim()},\'整合包目录已保存\')"></div>'
  +'<div><div class="k" style="margin-bottom:4px">端口</div><input id="v-gport" value="'+esc(String(c.gsv_port||'9880'))+'" onchange="voiceFieldSave({gsv_port:this.value.trim()},\'端口已保存（重启引擎后生效）\')"></div></div>'
  +'<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)"><div class="k" style="margin-bottom:4px"><b>合成优化</b>（性能与听感微调；普通用户保持默认即可）<span class="badge" style="background:#fff4e0;color:#b25e09;margin-left:8px">实测稳态 1.3-1.5s/条</span></div>'
  +'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:6px">'
  +'<div><div class="k" style="margin-bottom:4px">合成批次 <input type="number" id="v-gbatch" min="1" max="16" value="'+esc(String(c.gsv_batch_size||1))+'" style="width:56px" onchange="voiceFieldSave({gsv_batch_size:Math.max(1,Math.min(16,parseInt(this.value)||1))},\'合成批次已保存\')"><span class="k">（1-16；长句切分后并行合成，调大更快、更吃显存，4060 建议 2-4）</span></div></div>'
  +'<div><div class="k" style="margin-bottom:4px">语速 <input type="number" id="v-gspeed" min="0.5" max="2.5" step="0.05" value="'+esc(String(c.gsv_speed||1))+'" style="width:60px" onchange="voiceFieldSave({gsv_speed:Math.max(0.5,Math.min(2.5,parseFloat(this.value)||1))},\'语速已保存（即时生效）\')"><span class="k">（0.5-2.5，1=正常）</span></div></div>'
  +'<div><div class="k" style="margin-bottom:4px">长文切分 <select id="v-gsplit" onchange="voiceFieldSave({gsv_split:this.value},\'切分方式已保存\')">'
  +'<option value="cut0"'+((c.gsv_split||'cut0')==='cut0'?' selected':'')+'>不切分（短句最快）</option>'
  +'<option value="cut5"'+((c.gsv_split)==='cut5'?' selected':'')+'>按标点切（长文更稳）</option>'
  +'<option value="cut4"'+((c.gsv_split)==='cut4'?' selected':'')+'>中英符号切</option></select></div></div>'
  +'</div>'
  +'<label class="master" style="display:flex;gap:8px;align-items:flex-start;margin-top:10px"><input type="checkbox" id="v-gkeep" style="flex:none;margin-top:2px" '+(c.gsv_keepalive!==false?'checked':'')+' onchange="voiceFieldSave({gsv_keepalive:this.checked?1:0},\'防冷启动保活已\'+(this.checked?\'开启\':\'关闭\')+\'（即时生效）\')"> <span><b>防冷启动保活（推荐开启）</b><div class="k" style="margin-top:3px">引擎空闲超过 5 分钟，批处理/CUDA 缓存会被系统回收——下次合成第一条会慢 3 秒+。开启后每 5 分钟自动用一句超短文本热身（无声卡占用、显存代价数百 MB），让你每次发语音都吃到 1.3-1.5s 的稳态速度</div></span></label></div>'
 +'<div style="margin-top:10px;border:1px dashed var(--line);border-radius:8px;padding:12px">'
 +'<div class="k" style="margin-bottom:8px"><b>克隆新音色</b><span class="k">（逐步引导 · 完全免费）</span></div>'
 +'<div id="gw-step1">'
 +'<div class="k" style="margin-bottom:6px"><b>第 1 步 · 选择素材</b><span class="k">（参考音频限 3-10 秒——引擎硬限制，取一小段最代表音色的即可；样本语言要与参考语言一致）</span></div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input type="file" id="v-gfile" accept="audio/*,video/*" style="flex:1;min-width:180px" onchange="gwStep1Done()">'
 +'<label class="k">起始 <input id="v-gstart" type="number" min="0" step="1" value="0" style="width:58px"> 秒</label>'
 +'<label class="k">时长 <input id="v-gdur" type="number" min="3" max="10" step="1" value="8" style="width:52px"> 秒</label></div>'
 +'<input id="v-gptext" placeholder="强烈建议填：这段音频里说的话（语言要与上方参考语言一致）" style="width:100%;margin-top:6px">'
 +'</div>'
 +'<div id="gw-step2" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'
 +'<div class="k" style="margin-bottom:6px"><b>第 2 步 · 开始克隆</b><span class="k">（本地截取转码+合成试听，引擎约几秒）</span></div>'
 +'<button class="act" style="margin:0" onclick="gsvVoiceTry()">开始克隆</button>'
 +'<button class="act ghost danger" style="margin:0" onclick="gsvVoiceReset()">重来</button></div>'
 +'<div id="gw-prog" style="display:none;margin-top:10px">'
 +'<div style="display:flex;gap:8px;align-items:center"><div style="flex:1;height:4px;background:var(--tint);border-radius:2px;overflow:hidden"><div id="gw-bar" style="height:100%;width:0%;background:var(--btn);transition:width .4s"></div></div><span class="k" id="gw-pct" style="flex:none;min-width:36px;text-align:right">0%</span></div>'
 +'<div class="k" id="v-gmsg" style="margin-top:4px">准备中…</div></div>'
 +'<div id="gw-step4" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">'
 +'<div class="k" style="margin-bottom:6px"><b>第 3 步 · 满意就命名保存</b><span class="k">（试听在上方播放器，可反复听；不满意点「重来」调整片段）</span></div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="v-gname" placeholder="音色名（如 安和昴）" style="flex:1;min-width:150px">'
 +'<button class="act" style="margin:0" onclick="gsvVoiceSave()">保存到音色列表</button></div></div></div>'
 +'<div style="margin-top:10px"><div class="card" style="border:1px solid var(--border);padding:10px">'
 +'<div class="k" style="margin-bottom:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:15px">微调模型</b><span style="margin-left:auto"><button class="act ghost" style="margin:0" onclick="ftImportPick()">＋ 导入微调模型</button></span></div>'
 +'<div class="k" style="margin-bottom:8px">社区训练好的完整声音模型（.ckpt+.pth）——音色和说话习惯都来自大量素材训练，<b>还原度最高</b>；适合长期使用的固定音色。同一时刻只能加载一个，切换需重启引擎；生效期间克隆音色自动挂起，回退预设后恢复。</div>'
 +'<div id="gsv-ftlist">加载中…</div>'
 +'<div id="ft-prog" style="display:none;margin-top:8px">'
 +'<div style="display:flex;gap:8px;align-items:center"><div style="flex:1;height:4px;background:var(--tint);border-radius:2px;overflow:hidden"><div id="ft-bar" style="height:100%;width:0%;background:var(--btn);transition:width .4s"></div></div><span class="k" id="ft-pct" style="flex:none;min-width:36px;text-align:right">0%</span></div>'
 +'<div class="k" id="ft-progmsg" style="margin-top:4px">准备中…</div></div>'
 +'<div id="gsv-ftimport" style="display:none;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)">'
 +'<div class="k" style="margin-bottom:6px"><b>导入微调模型</b>（选包含 .ckpt + .pth 的文件夹——社区训练包原样即可；有 wav 参考音频会自动带上一条）</div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="ft-src" placeholder="模型文件夹路径（点右侧浏览选择）" style="flex:1;min-width:220px" readonly>'
 +'<button class="act ghost" style="margin:0" onclick="ftPickDir()">浏览…</button>'
 +'<input id="ft-name" placeholder="音色名（如 安和昴V2.5）" style="width:150px">'
 +'<button class="act" style="margin:0" onclick="ftImportGo()">导入</button>'
 +'<span class="k" id="ft-msg"></span></div>'
 +'<div class="k" style="margin-top:4px">导入只是复制文件；要点列表里的「启用」才会切换引擎模型（切换需重启引擎，约 40-90 秒）。</div></div></div>'
 +'<div class="card" style="border:1px solid var(--border);padding:10px">'
 +'<div class="k" style="margin-bottom:4px"><b style="font-size:15px">克隆音色</b></div>'
 +'<div class="k" style="margin-bottom:8px">用一段 3-10 秒的参考音频即时克隆——<b>快速灵活</b>，随时用新素材做一个；相似度略逊于微调模型。与微调模型<b>二选一</b>：启用微调模型时克隆音色自动挂起，回退预设后恢复。</div>'
 +'<div id="gsv-vlist"></div></div></div>'
 +'</div>';
}
async function gsvAutoSet(on){
 on=!!on;
 const r=await api('/api/voice/gsv/autostart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:on})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('保存失败：'+((r&&r.error)||''),false);return}
 toast(on?'已开启：应用启动时自动拉起本地引擎':'已关闭：引擎需手动启动');
 if(on){ // 立即生效：若引擎没在跑就顺手启动
  const st=await api('/api/voice/meta').catch(()=>null);
  if(st&&st.gsv&&st.gsv.dir_ok&&!st.gsv.running){toast('正在启动引擎…（模型加载约 1 分钟）');await api('/api/voice/gsv/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
 }
}
// ---- 微调模型（方案A）----
let _GSV_RUN=false;   // 引擎端口是否就绪（gsvVoicesLoad 拉最新 meta 后更新）
async function ftListLoad(){
 const box=document.getElementById('gsv-ftlist');
 if(!box)return;
 const r=await api('/api/voice/gsv/ft-list',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>null);
 const ms=(r&&r.models)||[];
 const using=(r&&r.using_ft)||false;
 const missing=(r&&r.missing)||[];
 let warn='';
 if(missing.length){
  warn='<div class="k" style="color:var(--err);margin-bottom:6px;padding:6px 8px;border:1px solid var(--err);border-radius:6px">⚠ '+missing.map(m=>esc(m.name)).join('、')+' 的模型文件已不存在（可能被手动删除）——'+(missing.some(m=>m.active)?'当前启用的模型丢失，请点「回退预设模型」；':'')+'可点对应条目的「删除」清理索引</div>';
 }
 if(!ms.length&&!using){box.innerHTML='<div class="k" style="padding:8px 2px">（还没导入微调模型——点右上「＋ 导入微调模型」）</div>'+warn;return}
 let html=warn+'<div style="display:grid;gap:6px">'+ms.map(m=>{const miss=missing.find(x=>x.id===m.id);return '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 10px;border:1.5px solid '+(m.active?'#1a7f37':'var(--line)')+';border-radius:8px;background:'+(m.active?'#f0faf0':'transparent')+'">'
  +'<b style="min-width:56px">'+esc(m.name)+'</b>'
  +(miss?'<span class="badge" style="background:var(--err);color:#fff">文件缺失</span>':'')
  +(m.active?'<span class="badge" style="background:#1a7f37;color:#fff">'+(_GSV_RUN?'✓ 生效中':'✓ 引擎加载中…')+'</span>':'')
  +((m.has_ref&&!miss)?'<span class="k">含参考音频</span>':'<span class="k" style="color:var(--err)">⚠ 无参考音频</span>')
  +'<span class="k">'+(m.created?new Date(m.created*1000).toLocaleDateString():'')+'</span>'
  +'<span style="margin-left:auto;display:flex;gap:6px">'
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="ftPreview(\''+esc(m.id)+'\')">试听</button>'
  +(m.active?'':'<button class="act" style="margin:0;padding:3px 12px;font-size:12px" onclick="ftUse(\''+esc(m.id)+'\',\''+esc(m.name)+'\')">启用</button>')
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="ftDel(\''+esc(m.id)+'\',\''+esc(m.name)+'\')">删除</button></span></div>'}).join('')+'</div>';
 if(using)html+='<div style="margin-top:6px"><button class="act ghost" style="margin:0;padding:3px 12px;font-size:12px" onclick="ftUse(\'\',\'预设模型\')">↩ 回退预设模型</button></div>';
 box.innerHTML=html;
}
async function ftPreview(id){
 // 微调模型试听：需先启用（引擎加载该模型）且含参考音频——用参考音频+当前引擎合成一句
 const r=await api('/api/voice/gsv/ft-list',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>null);
 const m=((r&&r.models)||[]).find(x=>x.id===id);
 if(!m){toast('模型不存在',false);return}
 if(!m.active){toast('先「启用」这个模型再试听（引擎需加载它，切换约 40-90 秒）',false);return}
 if(!m.has_ref){toast('该模型没有参考音频，无法试听（导入时自动找 wav 或用克隆向导配）',false);return}
 toast('合成试听中…（本地引擎约几秒）');
 const rr=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:{provider:'gsv',gsv_ft_ref:id},text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!rr||rr.ok===false){toast('试听失败：'+((rr&&rr.error)||''),false);return}
 const au=document.getElementById('v-audio');
 au.src=rr.audio;au.style.display='block';au.play().catch(()=>{});
}
function ftImportPick(){
 const box=document.getElementById('gsv-ftimport');
 if(box)box.style.display=(box.style.display==='none')?'block':'none';
}
async function ftPickDir(){
 const r=await api('/api/pick-dir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'选择微调模型文件夹（含 .ckpt 和 .pth）'})}).catch(()=>({dir:''}));
 if(r&&r.dir){document.getElementById('ft-src').value=r.dir}
}
async function ftImportGo(){
 const msg=document.getElementById('ft-msg');
 const src=document.getElementById('ft-src').value.trim();
 const name=document.getElementById('ft-name').value.trim();
 if(!src){msg.textContent='先浏览选择模型文件夹';return}
 if(!name){msg.textContent='给模型起个音色名';return}
 // 命名冲突预检（与已导入的微调模型重名提示确认）
 const cur=await api('/api/voice/gsv/ft-list',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({models:[]}));
 if(((cur&&cur.models)||[]).some(m=>m.name===name)){
  if(!await dlgConfirm({title:'已有同名模型',body:'已导入名为「'+name+'」的微调模型，继续导入会出现两个同名条目。仍要继续？',okText:'继续导入'}))return}
 msg.textContent='导入中…（复制模型文件，约几秒到十几秒）';
 const r=await api('/api/voice/gsv/ft-import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src_dir:src,name:name,prompt_lang:(document.getElementById('v-gplang')||{}).value||'zh'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){msg.textContent='导入失败：'+((r&&r.error)||'');return}
 msg.textContent='';
 toast('✓ 已导入「'+name+'」'+(r.has_ref?'（含参考音频）':'（未找到参考音频——启用后无法试听/合成需手动配）')+'；点列表「启用」切换引擎模型');
 document.getElementById('ft-src').value='';
 document.getElementById('ft-name').value='';
 ftListLoad();
}
// ---- 微调模型切换进度条（预估时间轴：写配置→拉起→加载权重，就绪即 100%）----
let _ftProgTimer=null,_ftBusy=false;
function ftProgStart(){
 const box=document.getElementById('ft-prog');if(!box)return;
 box.style.display='block';
 if(_ftProgTimer)clearInterval(_ftProgTimer);
 const bar0=document.getElementById('ft-bar'),pct0=document.getElementById('ft-pct');
 if(bar0){bar0.style.width='0%';bar0.style.background='var(--btn)'}
 if(pct0)pct0.textContent='0%';
 const t0=Date.now();let cur=0;
 _ftProgTimer=setInterval(()=>{
  const el=(Date.now()-t0)/1000;
  const tgt=el<2?8:(el<5?22:Math.min(92,22+(el-5)));
  cur=Math.min(tgt,cur+0.8+Math.random()*2.5);
  const bar=document.getElementById('ft-bar'),pct=document.getElementById('ft-pct'),msg=document.getElementById('ft-progmsg');
  if(bar)bar.style.width=cur.toFixed(0)+'%';
  if(pct)pct.textContent=cur.toFixed(0)+'%';
  if(msg)msg.textContent=el<2?'写入配置、停止旧引擎…':(el<5?'拉起引擎进程…':(el<75?'加载模型权重中…（共约 40-90 秒，因显卡而异）':'仍在加载，比预估慢一点…引擎就绪即完成'));
 },350);
}
function ftProgDone(ok,txt){
 if(_ftProgTimer){clearInterval(_ftProgTimer);_ftProgTimer=null}
 const bar=document.getElementById('ft-bar'),pct=document.getElementById('ft-pct'),msg=document.getElementById('ft-progmsg');
 if(bar&&pct){
  if(ok===true){bar.style.width='100%';pct.textContent='100%'}
  else if(ok===false){bar.style.background='var(--err)'}
 }
 if(msg&&txt)msg.textContent=txt;
}
function ftProgHide(){const box=document.getElementById('ft-prog');if(box)box.style.display='none'}
async function ftUse(id,name,skipConfirm){
 if(_ftBusy){toast('引擎切换进行中，稍候…',false);return}
 if(!skipConfirm){
  const cl=String((((_VM||{}).config)||{}).gsv_voice_id||'');
  if(id&&!await dlgConfirm({title:'切换到「'+name+'」？',body:'引擎将重启加载新模型（约 40-90 秒），期间语音合成暂不可用（自动回退其他来源）。'+(cl?'当前克隆音色会自动挂起，回退预设模型后恢复。':''),okText:'切换'}))return;
  if(!id&&!await dlgConfirm({title:'回退预设模型？',body:'引擎将重启加载预设模型（约 40-90 秒）。',okText:'回退'}))return;
 }
 _ftBusy=true;
 toast(id?('正在切换到「'+name+'」…'):'正在回退预设模型…');
 ftProgStart();
 const r=await api('/api/voice/gsv/ft-use',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).catch(()=>({ok:false,error:'请求失败'}));
 _ftBusy=false;
 if(!r||r.ok===false){ftProgDone(false,'✗ 切换失败：'+((r&&r.error)||''));toast('切换失败：'+((r&&r.error)||''),false);setTimeout(ftProgHide,6000);return}
 if(r.fallback){ftProgDone(false,'⚠ 模型加载失败，已自动回退预设模型——语音功能不受影响');toast(r.error||'模型加载失败，已自动回退预设模型',false);setTimeout(ftProgHide,8000)}
 else if(r.engine&&r.engine.ok){ftProgDone(true,'✓ 引擎已就绪，「'+(r.name||'')+'」生效中');toast('✓ 已切换到「'+(r.name||'')+'」');setTimeout(ftProgHide,6000)}
 else{ftProgDone(false,'配置已写入，但引擎启动异常：'+(((r.engine||{}).error)||''));toast('引擎重启异常：'+(((r.engine||{}).error)||''),false);setTimeout(ftProgHide,8000)}
 ftListLoad();gsvVoicesLoad();voiceBarRefresh();
}
async function voiceBarRefresh(){
 const m=await api('/api/voice/meta').catch(()=>null);
 if(!m||m.ok===false)return;
 _VM=m;
 const el=document.getElementById('voice-active-bar');
 if(el){const t=document.createElement('div');t.innerHTML=voiceActiveBar(m);const n=t.firstElementChild;if(n)el.replaceWith(n)}
}
async function ftDel(id,name){
 if(!await dlgConfirm({title:'删除微调模型「'+name+'」？',body:'删除复制的模型文件（约 240MB/个）；若正在使用会先回退预设模型。',danger:true,okText:'删除'}))return;
 const r=await api('/api/voice/gsv/ft-del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('删除失败：'+((r&&r.error)||''),false);return}
 toast('已删除「'+name+'」'+(r.was_active?'（已回退预设模型并重启引擎）':''));
 ftListLoad();
}
async function gsvVoicesLoad(){
 const box=document.getElementById('gsv-vlist');
 if(!box)return;
 const m=await api('/api/voice/meta').catch(()=>({ok:false}));
 if(m&&m.ok!==false){_VM=m;_GSV_RUN=!!((m.gsv)||{}).running}   // 挂起判定/状态条/生效徽标都要最新状态
 ftListLoad();
 const vs=(m&&m.gsv_voices)||[];
 const cur=((m&&m.config)||{}).gsv_voice_id||'';
 const ftOn=!!String(((m&&m.config)||{}).gsv_ft_model||'');
 if(!vs.length){box.innerHTML='<div class="k" style="padding:8px;border:1px dashed var(--line);border-radius:8px">（还没有本地音色——上面「克隆新音色」做一个，完全免费）</div>';return}
 box.innerHTML='<div style="display:grid;gap:6px">'+vs.map(v=>'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 10px;border:1.5px solid '+(v.id===cur?'#1a7f37':'var(--line)')+';border-radius:8px;background:'+(v.id===cur?'#f0faf0':'transparent')+'">'
  +'<b style="min-width:56px">'+esc(v.name||'(未命名)')+'</b>'
  +(v.id===cur?(ftOn?'<span class="badge" style="background:#8a8a8a;color:#fff">⏸ 挂起（微调模型生效中）</span>':'<span class="badge" style="background:#1a7f37;color:#fff">✓ 使用中</span>'):'')
  +(v.dur?'<span class="k">参考 '+v.dur+'s</span>':'')
  +(v.warn?'<span class="k" style="color:#b42318">⚠ '+esc(v.warn)+'</span>':'')
  +'<span class="k" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(v.prompt_text||'（未填原文）')+'</span>'
  +'<span style="margin-left:auto;display:flex;gap:6px">'
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="gsvVoicePreview(\''+esc(v.id)+'\')">试听</button>'
  +(v.id===cur?'':'<button class="act" style="margin:0;padding:3px 12px;font-size:12px" onclick="gsvVoiceUse(\''+esc(v.id)+'\')">启用</button>')
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="gsvVoiceDel(\''+esc(v.id)+'\',\''+esc(v.name||v.id)+'\')">删除</button></span></div>').join('')+'</div>'
  +'<div class="k" style="margin-top:6px">'+(ftOn?'微调模型生效中，已选的克隆音色自动挂起（回退预设模型后恢复）。':'')+'删除不可恢复；删「使用中」的音色机器人自动退回其他来源。</div>';
}
let __GSVWAV=null;   // 已截取待保存的参考音频（b64）；试听→满意→保存三段共用
function gwStep1Done(){
 const f=(document.getElementById('v-gfile').files||[])[0];
 const s2=document.getElementById('gw-step2');
 if(s2){s2.style.display=f?'block':'none'}
}
let _gwProgTimer=null;
function gwProgStart(){
 const box=document.getElementById('gw-prog');if(!box)return;
 box.style.display='block';
 const bar=document.getElementById('gw-bar'),pct=document.getElementById('gw-pct');
 if(!bar||!pct)return;
 bar.style.width='0%';bar.style.background='var(--btn)';pct.textContent='0%';
 if(_gwProgTimer)clearInterval(_gwProgTimer);
 let cur=0;
 _gwProgTimer=setInterval(()=>{
  if(cur<gwProgTarget)cur=Math.min(gwProgTarget,cur+Math.random()*4);
  bar.style.width=cur.toFixed(0)+'%';pct.textContent=cur.toFixed(0)+'%';
 },350);
}
let gwProgTarget=15;
function gwProgStage(t,label){gwProgTarget=t;const m=document.getElementById('v-gmsg');if(m&&label)m.textContent=label}
function gwProgDone(ok){
 if(_gwProgTimer){clearInterval(_gwProgTimer);_gwProgTimer=null}
 const bar=document.getElementById('gw-bar'),pct=document.getElementById('gw-pct');
 if(bar&&pct){
  if(ok===true){bar.style.width='100%';pct.textContent='100%'}
  else if(ok===false){bar.style.background='var(--err)'}
 }
}
async function gsvVoiceTrim(){
 const msg=document.getElementById('v-gmsg');
 const f=(document.getElementById('v-gfile').files||[])[0];
 if(!f){msg.textContent='先在第 1 步选择一个音频/视频文件';return false}
 if(f.size>120*1024*1024){msg.textContent='文件太大（超 120MB），先剪出 10-30 秒';return false}
 const start=Math.max(0,parseFloat(document.getElementById('v-gstart').value)||0);
 const dur=Math.min(10,Math.max(3,parseFloat(document.getElementById('v-gdur').value)||8));
 gwProgStage(30,'解码音频中…');
 let wav;
 try{
  const buf=await f.arrayBuffer();
  const Ctx=window.AudioContext||window.webkitAudioContext;
  const ac=new Ctx();
  const ab=await ac.decodeAudioData(buf);
  if(ac.close)ac.close();
  if(!ab||!ab.duration){gwProgDone(false);msg.textContent='解码失败：浏览器读不出这段音频';return false}
  if(ab.duration<start+1){gwProgDone(false);msg.textContent='起始位置超出音频时长（约 '+Math.round(ab.duration)+' 秒）';return false}
  gwProgStage(50,'截取 '+dur+' 秒并转码…');
  wav=await vcWav(ab,start,dur);
 }catch(e){gwProgDone(false);msg.textContent='解码失败：'+((e&&e.message)||e);return false}
 __GSVWAV=vcB64(wav);
 return true;
}
async function gsvVoiceTry(){
 const msg=document.getElementById('v-gmsg');
 gwProgStart();
 if(!await gsvVoiceTrim())return;   // 每次都用当前文件+起始/时长重新截取（曾因复用缓存导致调参数无效）
 gwProgStage(85,'本地引擎合成试听中…（约几秒）');
 const r=await api('/api/voice/gsv/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio:__GSVWAV,prompt_text:document.getElementById('v-gptext').value,prompt_lang:document.getElementById('v-gplang').value,text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){gwProgDone(false);msg.textContent='克隆/试听失败：'+((r&&r.error)||'');return}
 gwProgDone(true);
 const au=document.getElementById('v-audio');
 au.src=r.audio;au.style.display='block';au.play().catch(()=>{});
 msg.textContent='✓ 试听播放中（上方播放器可反复听）。';
 const s4=document.getElementById('gw-step4');
 if(s4){s4.style.display='block'}
 const ne=document.getElementById('v-gname');if(ne)ne.focus();
}
function gsvVoiceReset(){
 __GSVWAV=null;
 ['gw-step2','gw-step4','gw-prog'].forEach(function(id){const el=document.getElementById(id);if(el)el.style.display='none'});
 const f=document.getElementById('v-gfile');if(f)f.value='';
 const au=document.getElementById('v-audio');if(au){try{au.pause()}catch(e){}au.style.display='none';au.removeAttribute('src')}
 const n=document.getElementById('v-gname');if(n)n.value='';
 const msg=document.getElementById('v-gmsg');if(msg)msg.textContent='';
 toast('已清空本次克隆，重新选素材即可再来');
}
async function gsvVoiceSave(){
 const msg=document.getElementById('v-gmsg');
 const name=(document.getElementById('v-gname').value||'').trim();
 if(!name){toast('先填音色名再保存',false);return}
 if(!__GSVWAV && !await gsvVoiceTrim())return;
 msg.textContent='保存中…';
 const r=await api('/api/voice/gsv/voice-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,audio:__GSVWAV,prompt_text:document.getElementById('v-gptext').value,prompt_lang:document.getElementById('v-gplang').value})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){msg.textContent='保存失败：'+((r&&r.error)||'');return}
 __GSVWAV=null;
 ['gw-step2','gw-step4','gw-prog'].forEach(function(id){const el=document.getElementById(id);if(el)el.style.display='none'});
 if(msg)msg.textContent='';
 toast('✓ 已保存「'+name+'」到音色列表，在列表里「启用」即可给机器人用');
 await gsvVoicesLoad();
}
async function gsvVoicePreview(id){
 toast('合成试听中…（本地引擎约几秒）');
 const r=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:{provider:'gsv',gsv_voice_id:id},text:'你好呀，我是你的语音小助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('试听失败：'+((r&&r.error)||''),false);return}
 const au=document.getElementById('v-audio');
 au.src=r.audio;au.style.display='block';au.play().catch(()=>{});
}
async function gsvVoiceUse(id){
 if(!id)return;   // 空占位项（还没有本地音色）
 const ftid=String((((_VM||{}).config)||{}).gsv_ft_model||'');
 if(ftid){
  // 互斥：微调模型生效中 → 启用克隆音色 = 退出微调模型 + 重启引擎
  const ft=((_VM.gsv_ft_models)||[]).find(x=>x.id===ftid);
  if(!await dlgConfirm({title:'切换到克隆音色？',body:'当前微调模型「'+((ft&&ft.name)||ftid)+'」生效中。改用克隆音色需退出微调模型并重启引擎（约 40-90 秒），期间语音合成暂不可用。',okText:'切换'}))return;
  await ftUse('','预设模型',true);
 }
 const r=await api('/api/voice/gsv/voice-use',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('启用失败：'+((r&&r.error)||''),false);return}
 toast('已启用「'+(r.name||id)+'」——机器人语音现在用这个音色');
 gsvVoicesLoad();voiceBarRefresh();
}
async function gsvVoiceDel(id,name){
 if(!await dlgConfirm({title:'删除本地音色「'+name+'」？',body:'删除本机保存的参考音频（不可恢复）；机器人当前若在用会自动退回。',danger:true,okText:'删除'}))return;
 await api('/api/voice/gsv/voice-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
 toast('已删除「'+name+'」');
 gsvVoicesLoad();
}
let _VM2OLD=null;
// 当前真实生效的供应商+音色状态条（读服务端配置，非表单态——用户区分"现在用的是啥"）
function voiceActiveBar(m){
 const P=m.providers||{};
 const cfg=m.config||{};
 const prov=String(cfg.provider||'edge');
 const label=(P[prov]&&P[prov].label)||prov;
 let voice='';
 if(prov==='gsv'){
  const ftid=String(cfg.gsv_ft_model||'');
  if(ftid){
   // 微调模型生效：音色由训练的说话人决定（克隆音色此时挂起）
   const ft=(_VM.gsv_ft_models||[]).find(x=>x.id===ftid);
   voice='微调模型「'+(ft?ft.name:ftid)+'」';
   if(!((m.gsv)||{}).running)voice+='（引擎未就绪——启动中或已停止）';
  }else{
   const vid=String(cfg.gsv_voice_id||'');
   const v=(_VM.gsv_voices||[]).find(x=>x.id===vid);
   voice=vid?((v&&v.name)||vid):'（未选本地音色——去引擎卡启用）';
  }
 }else if(prov==='zhipu'){
  const sid=String(cfg.zhipu_speaker_id||'').trim();
  if(sid){const zv=(_VM.config&&_VM.config.zhipu_saved_voices||[]).find(x=>String(x.speaker_id)===sid);
   voice=(zv&&zv.name)||('克隆音色 '+sid.slice(0,8)+'…')}
  else{const zv=String(cfg.zhipu_voice||'tongtong');
   voice=(_VM.zhipu_voices||[]).reduce((a,x)=>x[1]===zv?x[0]:a,'')||zv}
 }else if(prov==='edge'){voice=(_VM.edge_voices||[]).reduce((a,x)=>x[1]===(cfg.edge_voice||'zh-CN-XiaoxiaoNeural')?x[0]:a,'')||'晓晓（默认）'}
 else if(prov==='siliconflow'){voice=String(cfg.sf_voice||'anna')}
 else{voice=String(cfg.custom_voice||'alloy')}
 const on=cfg.enabled?'':'（语音回复总开关未开——下方「开关与策略」里开启）';
 return '<div class="card" id="voice-active-bar" style="border-color:var(--acc-border);background:var(--acc-soft);padding:10px 14px">'
  +'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
  +'<span style="flex:none;font-weight:600">🎙 当前生效</span>'
  +'<span class="badge" style="background:var(--btn);color:var(--btn-fg)">'+esc(label)+'</span>'
  +'<span>音色：<b>'+esc(voice)+'</b></span>'
  +'<span class="k" style="margin-left:auto">'+esc(on)+'</span></div></div>';
}

async function vVoice(){
 const m=await pwApi('/api/voice/meta',{ok:false});
 if(!m||m.ok===false){$('#view').innerHTML=head('语音引擎数据加载失败，刷新重试')+'<div class="card">加载失败：'+esc((m&&m.error)||'')+'</div>';return}
 _VM2=m;_VM=m;   // _VM 同步：voiceProvChange/voiceCollect 等共用函数读它
 const g=m.gsv||{};
 const provHtml=vVoiceCards(m,g);
 $('#view').innerHTML=head('让机器人开口说话：回复改发语音（内容跟人设走，只换发送形式）。')+voiceActiveBar(m)+provHtml;
 setTimeout(()=>{voiceProvChange();gsvVoicesLoad();zvRender();gsvBadgePoll(m);},0);
}
function vVoiceCards(m,g){
 const P=m.providers||{};const c=m.config||{};
 const prov=Object.keys(P);
 const cur=c.provider||'edge';
 const vopts=prov.map(k=>'<option value="'+k+'" '+(k===cur?'selected':'')+'>'+esc(P[k].label)+'</option>').join('');
 const mode=String(c.mode||'keyword');
 const kw=(c.keywords||[]).join('、');
 const gsvCard=(g.dir_ok?vGsvCardInstalled(m,g):vGsvCardTeaser());
 // 顺序：开关策略 → 语音来源与音色 → 本地引擎（非 gsv 供应商时折叠）→ 计费说明（垫底）
 const provCard='<div class="card"><div class="k" style="margin-bottom:8px"><b>语音来源与音色</b>（供应商可随时换，互不影响）</div>'
 +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="k">供应商</span><select id="v-prov" onchange="voiceProvChange();voiceFieldSave(null,\'语音来源已保存（即时生效）\')">'+vopts+'</select></div>'
 +'<div class="k" id="v-bill" style="margin-top:8px;padding:8px;border:1px solid var(--line);border-radius:8px"></div>'
 +'<div id="v-provbox" style="margin-top:10px"></div>'
 +'<div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">'
 +'<input id="v-ptext" value="你好呀，我是你的语音小助手，听听这个声音怎么样？" style="flex:1;min-width:240px">'
 +'<button class="act ghost" style="margin:0" onclick="voicePreview()">试听（用当前表单配置）</button></div>'
 +'<audio id="v-audio" controls style="margin-top:8px;width:100%;display:none"></audio>'
 +'<div class="k" id="v-pmsg" style="margin-top:6px"></div></div>';
 const billCard='<div class="card"><div class="k"><b>计费说明</b>（各家语音合成的收费情况，选前看清楚）</div>'
 +prov.map(k=>'<div style="margin-top:8px"><b>'+esc(P[k].label)+'</b><div class="k" style="margin-top:3px">'+esc(P[k].billing)+'</div></div>').join('')
 +'</div>';
 return '<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><b>开关与策略</b><span class="badge" style="background:var(--tint);color:var(--dim)">语音内容跟人设走，只换发送形式</span></div>'
 +'<label class="master"><input type="checkbox" id="v-on" '+(c.enabled?'checked':'')+' onchange=""> <b>启用语音回复</b><div class="k" style="margin-top:3px">开启后按下面的策略把部分/全部回复改用语音发送；合成失败自动回退发文字，不会丢回复</div></label>'
 +'<div style="display:flex;gap:14px;margin-top:8px;flex-wrap:wrap">'
 +'<label><input type="checkbox" id="v-priv" '+(c.private_enabled!==false?'checked':'')+'> 私聊启用</label>'
 +'<label><input type="checkbox" id="v-grp" '+(c.group_enabled?'checked':'')+'> 群聊启用</label>'
 +'<label title="勾上=同一条回复发两个版本（先语音后同样内容的文字）；不勾=只发语音"><input type="checkbox" id="v-wt" '+(c.with_text?'checked':'')+'> 命中语音时同时发文字版</label></div>'
 +'<div class="k" style="margin-top:10px">触发方式</div>'
 +'<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:4px">'
 +'<label><input type="radio" name="vmode" value="keyword" '+(mode==='keyword'?'checked':'')+'> 对方消息含触发词时</label>'
 +'<label><input type="radio" name="vmode" value="always" '+(mode==='always'?'checked':'')+'> 所有回复都用语音</label>'
 +'<label><input type="radio" name="vmode" value="mixed" '+(mode==='mixed'?'checked':'')+'> 触发词必语音，其余按概率</label></div>'
 +'<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="v-kw" value="'+esc(kw)+'" placeholder="触发词（中英文逗号、顿号、空格分隔均可；如：念一下、发语音）" style="flex:1;min-width:220px">'
 +'<label class="k">语音概率 <input id="v-prob" type="number" min="0" max="100" step="5" value="'+esc(String(Math.round((c.prob||0)*100)))+'" style="width:64px"> %</label><span class="k">（触发词必语音；其余消息按此概率语音回复）</span></div>'
 +'<div style="margin-top:10px"><button class="act" style="margin:0" onclick="voiceSave()">保存设置</button><span class="k" id="v-ok" style="margin-left:8px"></span></div></div>'
 +provCard
 +'<div id="gsv-card-wrap" style="display:'+(cur==='gsv'?'':'none')+'">'+gsvCard+'</div>'
 +'<div id="gsv-teaser-wrap" style="display:'+(cur==='gsv'?'none':'')+'">'+(g.dir_ok?'':gsvCard)+'</div>'
 +billCard
 +'</div>';
}
async function gsvStart(){
 const b=event.target;b&&(b.disabled=true);
 toast('引擎启动中…（模型加载约 1 分钟）');
 const r=await api('/api/voice/gsv/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 b&&(b.disabled=false);
 if(!r||r.ok===false){toast('启动失败：'+((r&&r.error)||''),false);return}
 toast('本地引擎已启动（免费合成已可用）');
 vVoice();
}
async function gsvStop(){
 const r=await api('/api/voice/gsv/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 toast(r&&r.ok?'引擎已停止':'停止失败',!(r&&r.ok));
 vVoice();
}
function vGsvRefresh(){}
let _LM=null;
function larkPage(d){
 _LM=(d&&d.ok!==undefined)?d:(_LM||{config:{}});
 if(!_LM||_LM.ok===false){return '<div class="card">飞书数据加载失败，刷新重试</div>'}
 const c=_LM.config||{};
 const sdkTip=_LM.sdk?'':'（飞书→QQ 方向需要先在终端 pip install lark-oapi 并重启机器人；只开 QQ→飞书 则无需安装）';
 const brs=c.bridges||[];
 const rows=brs.map((b,i)=>'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0;padding:8px;border:1px solid var(--line);border-radius:8px">'
  +'<input class="lkqq" value="'+esc(b.qq_group)+'" placeholder="QQ 群号" style="width:120px">'
  +'<span class="k">↔</span>'
  +'<input class="lklk" value="'+esc(b.lark_chat_id)+'" placeholder="飞书 chat_id（点右侧拉取选择）" style="flex:1;min-width:200px">'
  +'<label><input type="checkbox" class="lk2f" '+(b.to_lark!==false?'checked':'')+'> QQ→飞书</label>'
  +'<label><input type="checkbox" class="lk2q" '+(b.to_qq?'checked':'')+'> 飞书→QQ</label>'
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="lkDel(this)">删</button></div>').join('');
 return '<div class="k" style="margin-bottom:10px">把 QQ 群和飞书群连起来：消息双向转发（各自可开关）。配置保存后即时生效。</div>'+
 '<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><b>飞书自建应用</b><span class="badge" style="background:var(--tint);color:var(--dim)">免费 · 免公网</span></div>'
 +'<div class="k">先在 <a href="https://open.feishu.cn" target="_blank">open.feishu.cn</a> 免费创建「企业自建应用」：①添加「机器人」能力 ②权限管理开通 im:chat 与 im:message（收发消息） ③发布版本 ④把机器人拉进目标飞书群。然后填 App ID / App Secret（Secret 只存本机）。'+sdkTip+'飞书方向用 WebSocket 长连接收事件，不需要公网服务器。</div>'
 +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px">'
 +'<div><div class="k" style="margin-bottom:4px">App ID</div><input id="lk-aid" value="'+esc(String(c.app_id||''))+'"></div>'
 +'<div><div class="k" style="margin-bottom:4px">App Secret（留空=保持已存）</div><input id="lk-sec" type="password" placeholder="'+(c.app_id?'已保存（留空不改）':'')+'"></div></div>'
 +'<label class="master" style="margin-top:8px"><input type="checkbox" id="lk-on" '+(c.enabled?'checked':'')+'> <b>启用互通</b><span class="k">（保存后生效；QQ→飞书即时转发，飞书→QQ 在重启机器人后接入长连接）</span></label></div>'
 +'<div class="card" id="lk-card"><div class="k" style="margin-bottom:6px"><b>桥接映射</b>（哪些 QQ 群和飞书群互通）</div>'+ (rows||'<div class="k">还没有桥接。点下面「添加桥接」。</div>')
 +'<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap"><button class="act ghost" style="margin:0" onclick="lkAdd()">＋ 添加桥接</button>'
 +'<button class="act ghost" style="margin:0" onclick="lkChats()">拉取飞书群列表</button>'
 +'<button class="act ghost" style="margin:0" onclick="lkTest()">发测试消息</button>'
 +'<button class="act" style="margin:0" onclick="lkSave()">保存</button></div>'
 +'<div class="k" id="lk-msg" style="margin-top:6px"></div></div>'
 +'<div class="card"><div class="k"><b>隐私说明</b></div><div class="k" style="margin-top:6px">桥接开启后，命中群的消息文本会经你的飞书应用转发到对应飞书群（带说话人昵称）；app_secret 与消息都不经过第三方。仅转发文本，图片/语音等不转发。</div></div>';
}
function lkAdd(){const d=document.createElement('div');d.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0;padding:8px;border:1px solid var(--line);border-radius:8px';
 d.innerHTML='<input class="lkqq" value="" placeholder="QQ 群号" style="width:120px"><span class="k">↔</span><input class="lklk" value="" placeholder="飞书 chat_id" style="flex:1;min-width:200px"><label><input type="checkbox" class="lk2f" checked> QQ→飞书</label><label><input type="checkbox" class="lk2q"> 飞书→QQ</label><button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="this.parentElement.remove()">删</button>';
 const card=document.getElementById('lk-card');
 const btnrow=card.querySelector('div[style]');
 card.insertBefore(d,card.children[card.children.length-1])}
function lkDel(btn){btn.parentElement.remove()}
function lkCollect(){
 const rows=[...document.querySelectorAll('.lkqq')].map(inp=>inp.parentElement);
 const bridges=rows.map(d=>({qq_group:d.querySelector('.lkqq').value.trim(),
  lark_chat_id:d.querySelector('.lklk').value.trim(),
  to_lark:d.querySelector('.lk2f').checked,to_qq:d.querySelector('.lk2q').checked}))
  .filter(b=>b.qq_group&&b.lark_chat_id);
 return {enabled:document.getElementById('lk-on').checked,
  app_id:document.getElementById('lk-aid').value.trim(),
  app_secret:document.getElementById('lk-sec').value.trim(),
  bridges:bridges}}
async function lkSave(){
 const r=await api('/api/lark/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:lkCollect()})}).catch(()=>({ok:false,error:'请求失败'}));
 const m=document.getElementById('lk-msg');
 if(r.ok===false){toast(r.error||'保存失败',false);if(m)m.textContent='保存失败：'+(r.error||'');return}
 _LM=null;toast('飞书配置已保存');if(m)m.textContent='已保存 ✓（飞书→QQ 若首次启用，重启机器人后接入）'}
async function lkChats(){
 const m=document.getElementById('lk-msg');if(m)m.textContent='拉取中…';
 const r=await api('/api/lark/chats').catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){if(m)m.textContent='拉取失败：'+(r.error||'')+'（检查 App ID/Secret 与 im:chat 权限，且机器人已被拉进群）';return}
 const list=r.chats||[];
 if(!list.length){if(m)m.textContent='没拉到群：先把机器人拉进至少一个飞书群';return}
 const names=list.map(x=>x.name+'  =  '+x.chat_id).join('\n');
 prompt('复制需要的 chat_id 填到桥接里：',names);
 if(m)m.textContent='已拉到 '+list.length+' 个群（见弹窗）'}
async function lkTest(){
 const m=document.getElementById('lk-msg');if(m)m.textContent='发送中…';
 const bid=(document.querySelector('.lklk')||{}).value||'';
 if(!bid){toast('先填一个飞书 chat_id',false);return}
 const r=await api('/api/lark/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:bid})}).catch(()=>({ok:false,error:'请求失败'}));
 if(m)m.textContent=r.ok?'测试消息已发，去飞书群看看 ✓':'发送失败：'+(r.error||'')}
async function ciQqCliExport(){
 const chat=document.getElementById('ci-chat').value.trim();
 if(!chat){toast('先填会话（QQ号/昵称/群名）',false);return}
 const r=await runTask('QQ 导出「'+chat.slice(0,12)+'」','/api/chatimport/qqcli-export',{chat:chat});
 if(r.ok===false){toast(r.error||'导出失败',false);return}
 toast('已导入「'+(r.session||chat)+'」'+(r.count||0)+' 条');load()}
async function ciWxCliExport(){
 const chat=document.getElementById('ci-wxchat').value.trim();
 if(!chat){toast('先填会话（联系人备注/昵称 或 群名）',false);return}
 const r=await runTask('微信导出「'+chat.slice(0,12)+'」','/api/chatimport/wxcli-export',{chat:chat});
 if(r.ok===false){toast(r.error||'导出失败',false);return}
 toast('已导入「'+(r.session||chat)+'」'+(r.count||0)+' 条');load()}
function ciCopy(id){
 const t=document.getElementById(id);if(!t)return;
 t.select();document.execCommand('copy');toast('已复制，去终端粘贴执行')}
async function ciPick(src){
 toast('请选择导出文件…');
 const r=await api('/api/chatimport/pick',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'解析失败',false);return}
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>导入到隔离库</h3><div class="dlg-body">'
 +'解析成功：<b>'+r.count+'</b> 条 · '+esc((r.range||[])[0]||'?')+' ~ '+esc((r.range||[])[1]||'?')+'<br>说话人：'+esc((r.senders||[]).join('、')||'?')+'<div class="k mono" style="margin-top:8px;font-size:12px">'+(r.preview||[]).map(x=>esc(x)).join('<br>')+'</div>'
 +'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px">会话名（用于隔离库展示）</div><input id="ci-name" value="'+esc((r.path||'').split(/[\\\\/]/).pop()||'').replace(/\\.[^.]+$/,'')+'" style="width:100%"></div>'
 +'</div><div class="dlg-btns"><button class="act ghost">取消</button><button class="act">导入</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=e=>{if(e.target===m)close()};
 m.querySelector('.dlg-btns .act').onclick=async()=>{
  const name=document.getElementById('ci-name').value.trim()||'会话';
  close();
  const st=await api('/api/chatimport/store',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name,path:r.path})}).catch(()=>({ok:false,error:'请求失败'}));
  if(st.ok===false){toast(st.error||'导入失败',false);return}
  toast('已导入「'+name+'」'+st.count+' 条到隔离库');load()};
}
async function ciDelete(src,name){
 if(!await dlgConfirm({title:'删除会话',body:'删除隔离库中的「'+esc(name)+'」？学习原料将不可恢复（已保存的人设/记忆不受影响）。',okText:'删除',danger:true}))return;
 const r=await api('/api/chatimport/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name})}).catch(()=>({ok:false}));
 if(r.ok!==false)load();else toast('删除失败',false);
}
async function ciLearn(src,name){
 const who=await dlgPrompt({title:'学习「'+name+'」',body:'要让机器人重点学习谁的说话风格？<br>留空 = 学习整体氛围与主要说话人。',placeholder:'说话人名字（可留空）',okText:'开始学习'});
 if(who===null)return;
 toast('机器人正在消化记录（约 30-90 秒）…');
 const r=await api('/api/chatimport/learn',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name,who:who||''})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'学习失败',false);return}
 const ps=await api('/api/personas').catch(()=>({personas:[]}));
 const exist=(ps.personas||[]).map(x=>x.name);
 const suggest=r.persona_name||'';
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:640px"><h3>学习结果 → 人设草稿</h3><div class="dlg-body">'
 +'<div class="k" style="margin-bottom:4px">人设名'+(exist.includes(suggest)?'（<b style="color:var(--err)">与现有人设重名，保存将覆盖</b>）':'')+'</div><input id="ci-pname" value="'+esc(suggest)+'" style="width:60%">'
 +'<div class="k" style="margin:8px 0 4px">人设卡正文（可编辑）</div><textarea id="ci-ptext" rows="12" style="width:100%;resize:vertical;font-size:13px">'+esc(r.persona)+'</textarea>'
 +'<div class="k" style="margin:8px 0 4px">候选记忆（勾选后写入长期记忆；需填关联对象）</div><div id="ci-mems" style="max-height:140px;overflow:auto">'+(r.memories||[]).map((x,i)=>'<label style="display:flex;gap:6px;align-items:flex-start;margin:3px 0"><input type="checkbox" checked data-i="'+i+'"> <span class="k">'+esc(x)+'</span></label>').join('')+'</div>'
 +'<div style="display:flex;gap:8px;margin-top:8px;align-items:center"><span class="k" style="flex:none">记忆关联</span><input id="ci-mid" placeholder="QQ号/群号（不填则不写记忆）" style="flex:1"></div>'
 +'</div><div class="dlg-btns"><button class="act ghost">取消</button><button class="act">保存人设</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=e=>{if(e.target===m)close()};
 m.querySelector('.dlg-btns .act').onclick=async()=>{
  const pname=document.getElementById('ci-pname').value.trim();
  const ptext=document.getElementById('ci-ptext').value.trim();
  if(!pname||ptext.length<20){toast('人设名与正文不能为空/过短',false);return}
  if(exist.includes(pname)){
   const old=(ps.personas||[]).find(x=>x.name===pname);
   if(!await dlgConfirm({title:'覆盖人设「'+pname+'」',body:'已存在同名的人设卡（'+((old&&old.notes)||'')+'…）。<br><b>覆盖后旧内容将丢失，确定？</b>',okText:'覆盖保存',danger:true}))return;
  }
  const sv=await api('/api/personas/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:pname,text:ptext})}).catch(()=>({ok:false}));
  if(sv.ok===false){toast('人设保存失败',false);return}
  toast('人设「'+pname+'」已保存（人设页可启用）');
  const mid=document.getElementById('ci-mid').value.trim();
  const picked=[...document.querySelectorAll('#ci-mems input:checked')].map(cb=>(r.memories||[])[+cb.dataset.i]).filter(Boolean);
  if(mid&&/^\d{5,12}$/.test(mid)&&picked.length){
   for(const t of picked){await api('/api/memory/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope:'contact',id:mid,text:t})}).catch(()=>{})}
   toast('已写入 '+picked.length+' 条记忆');
  }
  close();load()};
}
async function biliRun(){
 const r=await api('/api/bili/run',{method:'POST'}).catch(e=>({ok:false,error:e}));
 if(r.ok===false){toast(r.error||'触发失败',false);return}
 toast('已在后台刷一轮，稍后看运行日志 AGENT_BILI_*')}
async function editMemo(idx){
 const d=await api('/api/memory');
 const it=(d.items||[]).find(x=>x.idx===idx);if(!it)return;
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>编辑记忆</h3>'
  +'<div class="dlg-body">修改这条记忆的类型、对象或内容，保存后机器人下一轮对话即用新记忆。</div>'
  +'<div style="display:flex;gap:8px;margin-top:12px;align-items:center">'
  +'<select class="mini"><option value="group" '+(it.scope==='group'?'selected':'')+'>群</option><option value="contact" '+(it.scope==='contact'?'selected':'')+'>联系人</option></select>'
  +'<input value="'+esc(String(it.id))+'" placeholder="群号/QQ号" style="width:130px">'
  +'</div><textarea rows="4" style="width:100%;margin-top:10px;resize:vertical" maxlength="300">'+esc(it.text)+'</textarea>'
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act">保存</button></div></div>';
 document.body.appendChild(m);
 const sel=m.querySelector('select'),cid=m.querySelector('input'),ta=m.querySelector('textarea');
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  if(!/^\d{5,12}$/.test(cid.value.trim())||!ta.value.trim()){toast('对象需为群号/QQ号，内容不能为空',false);return}
  close();
  const r=await saveKV('/api/memory/update',{idx:+idx,scope:sel.value,id:cid.value.trim(),text:ta.value.trim()},'记忆已更新（即时生效）');
  if(r.ok!==false)load()};
 m.onclick=e=>{if(e.target===m)close()};
 m.onkeydown=e=>{if(e.key==='Escape')close()};
 setTimeout(()=>{ta.focus()},30)}
async function delMemo(idx){
 if(!await dlgConfirm({title:'删除记忆',body:'删除这条记忆？机器人将不再记得此事（不可恢复）。',okText:'删除',danger:true}))return;
 const r=await saveKV('/api/memory/delete',{idx:+idx},'记忆已删除（即时生效）');
 if(r.ok!==false)load()}
async function addMemo(){
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>手动新增记忆</h3>'
  +'<div class="dlg-body">给指定群或联系人写入一条记忆，机器人与其对话时会记得。例如：选群 → 输入群号 →「周三有小组作业讨论」。</div>'
  +'<div style="display:flex;gap:8px;margin-top:12px;align-items:center">'
  +'<select class="mini"><option value="group">群</option><option value="contact">联系人</option></select>'
  +'<input placeholder="群号/QQ号" style="width:130px">'
  +'</div><textarea rows="3" style="width:100%;margin-top:10px;resize:vertical" maxlength="300" placeholder="记忆内容（不超过300字）"></textarea>'
  +'<div class="dlg-btns"><button class="act ghost">取消</button><button class="act">写入</button></div></div>';
 document.body.appendChild(m);
 const sel=m.querySelector('select'),cid=m.querySelector('input'),ta=m.querySelector('textarea');
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.querySelector('.dlg-btns .act:not(.ghost)').onclick=async()=>{
  if(!/^\d{5,12}$/.test(cid.value.trim())||!ta.value.trim()){toast('对象需为群号/QQ号，内容不能为空',false);return}
  close();
  const r=await saveKV('/api/memory/add',{scope:sel.value,id:cid.value.trim(),text:ta.value.trim()},'记忆已写入（即时生效）');
  if(r.ok!==false)load()};
 m.onclick=e=>{if(e.target===m)close()};
 m.onkeydown=e=>{if(e.key==='Escape')close()};
 setTimeout(()=>cid.focus(),30)}
async function clearMem(){if(!await dlgConfirm({title:'清空记忆流水',body:'确认清空记忆流水？此操作不可恢复。',okText:'清空',danger:true}))return;await api('/api/memory/clear',{method:'POST'});toast('记忆流水已清空');load()}
let _curPool=null;
async function vStickers(){
 const d=await api('/api/stickers');
 const pools=d.pools||[{id:'_global',label:'通用库'}];
 let cur=_curPool&&pools.find(x=>x.id===_curPool)?_curPool:null;
 if(!cur)cur=(d.pools&&d.pools.find(x=>x.id===(d.pool||'_global')))?(d.pool||'_global'):(d.fav_pool||'_global');
 _curPool=cur;
 const sel=pools.map(x=>`<option value="${esc(x.id)}" ${x.id===cur?'selected':''}>${esc(x.label)}</option>`).join('');
 const items=_curPool===(d.pool||'_global')?d.items:(await api('/api/stickers?pool='+encodeURIComponent(cur))).items;
 const rows=stickerRows(items||[],cur);
 $('#view').innerHTML=head('表情包分库管理：机器人选表情 = 当前人设库 + 通用库 + QQ收藏库（收藏库可开关）')+
 `${d.fav_pool?`<div class="card"><label class="master"><input type="checkbox" ${d.fav_enabled!==false?'checked':''} onchange="toggleFavPool(this.checked)">
  <span><b>QQ收藏表情参与回复</b><div class="k" style="margin-top:3px">关闭后机器人选表情时只用人设库和通用库；收藏库数据与同步功能保留，随时可再开启（下一轮回复即生效）</div></span></label></div>`:''}
 <div class="card">
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
  <select id="stpool">${sel}</select>
  <input id="sttags" placeholder="标签（中英文逗号、顿号、空格分隔均可）" style="width:180px">
  <input id="stdesc" placeholder="描述" style="width:160px">
  <label style="margin:0"><input type="file" id="stfile" accept=".jpg,.jpeg,.png,.gif,.webp,.bmp" multiple></label>
  <button class="act" style="margin:0" onclick="uploadStickers()">上传到当前库</button>
  <button class="act ghost" style="margin:0" onclick="syncQQ()">同步 QQ 收藏表情到当前库</button>
 </div>
 <table style="margin-top:14px"><tr><th>预览</th><th>ID</th><th>备注</th><th>标签</th><th>使用</th><th>操作</th></tr>${rows||'<tr><td colspan=6 class="k">库为空</td></tr>'}</table></div>`;
 $('#stpool').onchange=()=>{_curPool=$('#stpool').value;loadStickerPool(_curPool)};
}
function stickerRows(items,pool){
 return items.map(s=>`<tr>
  <td><img src="/api/stickers/thumb?pool=${encodeURIComponent(pool)}&id=${s.id}" style="height:44px;max-width:64px;object-fit:contain;border-radius:6px;background:var(--tint)" loading="lazy" onerror="this.style.visibility='hidden'"></td>
  <td class="mono">${esc(String(s.id))}</td><td>${esc(s.desc||'')}</td><td>${esc((s.tags||[]).join(', '))}</td><td class="mono">用过${s.uses||0}次</td>
  <td><button class="act ghost" style="margin:0" onclick="editSticker(${s.id},'${esc(pool)}')">备注</button>
  <button class="act ghost danger" style="margin:0" onclick="delSticker(${s.id},'${esc(pool)}')">删除</button></td></tr>`).join('');
}
async function loadStickerPool(pool){const r=await api('/api/stickers?pool='+encodeURIComponent(pool));
 document.querySelector('#view table').innerHTML='<tr><th>预览</th><th>ID</th><th>备注</th><th>标签</th><th>使用</th><th>操作</th></tr>'+(stickerRows(r.items||[],pool)||'<tr><td colspan=6 class="k">库为空</td></tr>');}
async function uploadStickers(){
 const files=$('#stfile').files;if(!files.length){toast('请先选择图片',false);return}
 const pool=$('#stpool').value;const tags=$('#sttags').value;const desc=$('#stdesc').value;let okN=0,failN=0;
 for(const f of files){
  try{
   const b64=await new Promise((res,rej)=>{
    const rd=new FileReader();
    rd.onload=()=>{try{
      const url=String(rd.result);const b64part=url.slice(url.indexOf(',')+1);
      res(b64part);
     }catch(e){rej(e)}};
    rd.onerror=()=>rej(rd.error||new Error('读取失败'));
    rd.readAsDataURL(f);
   });
   const r=await api('/api/stickers/upload',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pool,filename:f.name,data_b64:b64,tags,desc})});
   if(r.ok)okN++;else{failN++;toast('上传失败: '+(r.error||'')+'（'+f.name+'）',false)}
  }catch(e){failN++;toast('读取失败: '+f.name,false)}
 }
 if(okN)toast('已上传 '+okN+' 张到 '+(pool==='_global'?'通用库':pool)+(failN?('，失败 '+failN):''));
 load();
}
async function editSticker(id,pool){
 const r=await api('/api/stickers?pool='+encodeURIComponent(pool));
 const it=(r.items||[]).find(x=>x.id===id);if(!it)return;
 const desc=await dlgPrompt({title:'编辑表情 · 备注',body:'备注（给机器人的使用提示，可留空）：\n例如：对方难过时发的安慰图 / 胜利嘲讽专用',value:it.desc||'',okText:'下一步'});
 if(desc===null)return;
 const tagsStr=await dlgPrompt({title:'编辑表情 · 标签',body:'标签（中英文逗号/顿号/空格分隔均可；点取消=保持原标签不动）：\n例如：开心,猫猫',value:(it.tags||[]).join(','),okText:'保存'});
 const body={pool,id,desc:desc};
 if(tagsStr!==null)body.tags=tagsStr.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
 const res=await api('/api/stickers/update',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)});
 toast(res.ok?'已保存，机器人下次选表情即可感知':'保存失败: '+(res.error||''),res.ok===false);load();
}
async function delSticker(id,pool){if(!await dlgConfirm({title:'删除表情',body:'删除表情包 #'+id+'？',okText:'删除',danger:true}))return;await api('/api/stickers/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:+id,pool})});toast('已删除');load()}
async function toggleFavPool(on){const r=await api('/api/stickers/fav-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on})});toast(r.ok?(on?'QQ收藏表情已启用（已生效，参与选表情）':'QQ收藏表情已停用（已生效）'):'保存失败: '+(r.error||''),r.ok!==false);load()}
async function syncQQ(){
 toast('同步中：拉取收藏表情并下载（可能需要几十秒）…');
 try{
  const r=await api('/api/stickers/sync-qq',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pool:$('#stpool').value,count:100})});
  toast(r.ok?('同步完成：新增 '+r.added+' 张（拉取 '+r.fetched+'，失败 '+r.failed+'，重复自动跳过）'):('同步失败: '+(r.error||'')),r.ok!==false);load();
 }catch(e){toast('同步失败: '+e,false)}
}
/* ---- 长任务进度卡：runTask(label, url, body) → 右下角任务卡轮询直至完成 ----
   后端约定：POST 返回 {task: id}；GET /api/task/status?id= → {phase,pct,msg,done,ok,result} */
const _PHASE_TXT={download:'下载',export:'导出',store:'解析入库',install:'安装',starting:'准备',done:'完成',error:'失败'};
function _phaseTxt(p){return _PHASE_TXT[p]||p||''}
async function runTask(label,url,body){
 let st=document.getElementById('taskbox');
 if(!st){st=document.createElement('div');st.id='taskbox';
  st.style.cssText='position:fixed;right:16px;bottom:16px;z-index:9999;max-width:340px';
  document.body.appendChild(st)}
 const card=document.createElement('div');card.className='card';
 card.style.cssText='margin-top:8px;box-shadow:0 4px 16px rgba(0,0,0,.25)';
 const t0=Date.now();
 card.innerHTML='<div class="k" style="display:flex;gap:8px;align-items:center"><b>'+esc(label)+'</b><span class="k taskphase" style="margin-left:auto"></span></div>'
 +'<div style="height:6px;border-radius:3px;background:var(--line);margin-top:8px;overflow:hidden"><div class="taskbar" style="height:100%;width:0%;border-radius:3px;background:var(--accent,#4a7dff);transition:width .4s"></div></div>'
 +'<div class="k taskmsg" style="margin-top:6px;font-size:12px"></div>';
 st.appendChild(card);
 const set=(phase,pct,msg)=>{card.querySelector('.taskbar').style.width=(pct||0)+'%';
  card.querySelector('.taskphase').textContent=_phaseTxt(phase)+(pct?' '+pct+'%':'')+' · '+Math.round((Date.now()-t0)/1000)+'s';
  if(msg)card.querySelector('.taskmsg').textContent=msg};
 set('starting',0,'');
 const r=await api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false||!r.task){card.remove();return r}
 const tid=r.task;
 return new Promise(res=>{
  const iv=setInterval(async()=>{
   const s=await api('/api/task/status?id='+encodeURIComponent(tid)).catch(()=>null);
   if(!s)return;
   if(!s.done){set(s.phase,s.pct,s.msg);return}
   clearInterval(iv);set(s.phase,100,s.ok?'完成':'失败：'+((s.result||{}).error||''));
   setTimeout(()=>card.remove(),s.ok?3500:8000);
   res(s.result||{ok:s.ok})
  },1200)})
}
load(); setInterval(()=>{if(TAB==='status')load()},10000);
</script></body></html>"""


def _tail_lines(path, n=250):
    """读日志文件尾部 n 行（忽略读取异常，返回字符串行列表）。"""
    p = pathlib.Path(path)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:] if ln.strip()]
    except OSError:
        return []


_LINE_TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s?")


def _split_ts(ln):
    """剥离日志行首 [HH:MM:SS] 时间戳（旧日志无前缀返回空串）。"""
    m = _LINE_TS.match(ln)
    return (m.group(1), ln[m.end():]) if m else ("", ln)


def _parse_daemon_line(ln):
    """daemon.log 单行 → 展示记录 dict；AGENT_* 前缀做轻量摘要。"""
    ts, ln = _split_ts(ln)
    if ln.startswith("AGENT_EVENT "):
        try:
            ev = json.loads(ln[len("AGENT_EVENT "):])
            if not ts:
                ts = time.strftime("%H:%M:%S", time.localtime(float(ev.get("ts", 0) or 0)))
            chat = ("群%s" % ev.get("chat_id")) if ev.get("chat_type") == "group" \
                else ("私聊%s" % ev.get("chat_id"))
            trig = ev.get("trigger_reason")
            tag = "触发:%s" % trig if ev.get("triggered") else "旁观"
            text = str(ev.get("text", ""))[:48]
            return {"src": "daemon", "level": "info" if ev.get("triggered") else "dim",
                    "ts": ts,
                    "text": "[%s] %s：%s（%s）" % (chat,
                                                  ev.get("sender_name", "?"), text, tag)}
        except ValueError:
            pass
        return {"src": "daemon", "level": "dim", "ts": ts, "text": ln}
    if ln.startswith("AGENT_LOG "):
        try:
            rec = json.loads(ln[len("AGENT_LOG "):])
            msg = str(rec.get("msg", ""))
            detail = str(rec.get("detail", ""))[:80]
            if msg == "ws_connected":
                return {"src": "daemon", "level": "ok", "ts": ts, "text": "WS 已连接：%s" % detail}
            if msg == "ws_disconnected":
                return {"src": "daemon", "level": "warn", "ts": ts, "text": "WS 断开（自动重连中）：%s" % detail}
            if msg == "login":
                return {"src": "daemon", "level": "ok", "ts": ts,
                        "text": "已登录：%s(%s)" % (rec.get("nickname", "?"), rec.get("user_id", "?"))}
            return {"src": "daemon", "level": "dim", "ts": ts, "text": msg + (" " + detail if detail else "")}
        except ValueError:
            pass
    if "Traceback" in ln:
        return {"src": "daemon", "level": "err", "ts": ts, "text": "异常堆栈（详见 daemon.log）"}
    return {"src": "daemon", "level": "dim", "ts": ts, "text": ln[:200]}


def _parse_agent_lines(lines):
    """agent.log 行列表 → 展示记录列表；Python 堆栈连续段折叠为一条。"""
    out, i, n = [], 0, len(lines)
    while i < n:
        ts, ln = _split_ts(lines[i])
        if ln.startswith("Traceback"):
            j = i + 1
            cause = ""
            while j < n and (lines[j].startswith("  ") or lines[j].startswith("\t")
                             or lines[j].startswith("File ")):
                if ": " in lines[j] and not lines[j].lstrip().startswith("File"):
                    cause = lines[j].strip()
                j += 1
            out.append({"src": "agent", "level": "err", "ts": ts,
                        "text": "异常：" + (cause or "堆栈见 agent.log")[:120]})
            i = j
            continue
        if ln.startswith("AGENT_DONE "):
            out.append({"src": "agent", "level": "ok", "ts": ts, "text": "回复完成 " + ln[11:140]})
        elif ln.startswith("AGENT_SKIP "):
            out.append({"src": "agent", "level": "dim", "ts": ts, "text": "静默跳过 " + ln[11:140]})
        elif ln.startswith("AGENT_READY "):
            out.append({"src": "agent", "level": "info", "ts": ts, "text": "大脑就绪 " + ln[12:140]})
        elif ln.startswith("AGENT_MODEL_FAIL"):
            out.append({"src": "agent", "level": "warn", "ts": ts, "text": "模型失败 " + ln[17:150]})
        elif ln.startswith("AGENT_VISION_FAIL"):
            out.append({"src": "agent", "level": "warn", "ts": ts, "text": "视觉失败 " + ln[18:150]})
        elif ln.startswith("AGENT_ERROR "):
            out.append({"src": "agent", "level": "err", "ts": ts, "text": "处理失败 " + ln[12:150]})
        elif ln.startswith("AGENT_EVOLVE "):
            out.append({"src": "agent", "level": "info", "ts": ts, "text": ln[:140]})
        elif ln.startswith("AGENT_LOOP_ERROR"):
            out.append({"src": "agent", "level": "err", "ts": ts, "text": ln[:140]})
        elif ln.startswith("AGENT_SHUTDOWN"):
            out.append({"src": "agent", "level": "warn", "ts": ts, "text": "大脑已停止"})
        else:
            out.append({"src": "agent", "level": "dim", "ts": ts, "text": ln[:140]})
        i += 1
    return out


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _parse_napcat_lines(lines):
    """NapCat 控制台输出 → 运行日志条目（去 ANSI 色码；保留密钥与二维码原文）。"""
    out = []
    for ln in lines:
        s = _ANSI_RE.sub("", ln).rstrip()
        if not s:
            continue
        m = re.match(r"^(?:\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s*(.*)$", s)
        if m:
            lvl = {"error": "err", "warn": "warn"}.get(m.group(2), "info")
            out.append({"src": "napcat", "level": lvl,
                        "ts": m.group(1), "text": m.group(3)[:400]})
        else:
            out.append({"src": "napcat", "level": "info", "ts": "",
                        "text": s[:400]})
    return out


def collect_runlog(src="all", tail=250):
    """聚合 daemon/agent/NapCat 日志与启动任务步骤 → 展示记录列表（正序）。"""
    items = []
    if src in ("all", "daemon"):
        items += [_parse_daemon_line(ln) for ln in
                  _tail_lines(os.path.join(C.logs_dir(), "daemon.log"), tail)]
    if src in ("all", "agent"):
        items += _parse_agent_lines(
            _tail_lines(os.path.join(C.logs_dir(), "agent.log"), tail))
    if src == "napcat":
        items += _parse_napcat_lines(_tail_lines(
            str(S.napcat_console_log()), tail))
    if src in ("all", "boot"):
        _boot_state_load()
        with _boot_lock:
            steps = list(_boot_state["steps"])
            meta = {"running": _boot_state["running"], "failed": _boot_state["failed"],
                    "final": _boot_state["final"]}
        for s in steps:
            items.append({"src": "boot", "level": "info",
                          "ts": time.strftime("%H:%M:%S", time.localtime(float(s.get("ts", 0) or 0))),
                          "text": "[%s] %s" % (s.get("component", "?"), s.get("message", ""))})
        if meta["final"]:
            items.append({"src": "boot", "level": "err" if meta["failed"] else "info",
                          "ts": time.strftime("%H:%M:%S"),
                          "text": "启动任务：%s" % meta["final"]})
    return items


def read_const(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def load_json_const(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_const(path, text):
    """写模块级常量路径文件（本文件所有写入路径均为常量）。
    首次保存时父目录可能尚不存在（如 styles/ 在第一次存风格卡前不会建）
    → 先自建目录，否则 tmp.write_text 直接抛 FileNotFoundError，旧版表现为
    前端"保存失败：网络错误"；tmp 带进程号，避免固定名被杀软反复盯上。
    os.replace 可能撞上 agent/daemon 进程的瞬时读句柄（Windows 读句柄不共享
    删除权）→ PermissionError；重试若干次后就地写兜底，避免保存必现失败。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("%s.ui.%d.tmp" % (p.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    for i in range(6):
        try:
            os.replace(str(tmp), str(p))
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:
        p.write_text(text, encoding="utf-8")
        tmp.unlink()
    except OSError:
        raise


def _sticker_pool_items(data, pool):
    """取指定库条目；兼容旧版 {"items":[...]} 扁平结构（视为通用库）。"""
    if not isinstance(data, dict):
        return []
    if pool == "_global" and isinstance(data.get("items"), list) and "_global" not in data:
        return data["items"]
    entry = data.get(pool)
    if isinstance(entry, dict):
        return entry.get("items", [])
    return []


def _sticker_pool_next_id(data, pool):
    entry = data.get(pool) if isinstance(data, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("next_id"), int):
        return entry["next_id"]
    return 1


def _sniff_image_ext(raw):
    """按魔数识别图片扩展名（QQ 表情 URL 无扩展名）。"""
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:2] == b"BM":
        return ".bmp"
    return ""


def _assert_public_host(host, port):
    """字面量 + DNS 解析后逐地址复核公网（防内网/rebinding），通过后返回连接用的 host。"""
    import ipaddress
    import socket
    h = str(host or "").strip().rstrip(".").lower()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        raise ValueError("blocked host: %r" % h)
    try:
        addr = ipaddress.ip_address(h)
        if not addr.is_global:
            raise ValueError("blocked non-global ip: %s" % h)
        return h
    except ValueError as e:
        if "blocked" in str(e):
            raise
    infos = socket.getaddrinfo(h, int(port), socket.AF_UNSPEC, socket.SOCK_STREAM)
    for info in infos:
        ip = info[4][0]
        if not ipaddress.ip_address(ip).is_global:
            raise ValueError("host %s 解析到非公网地址 %s" % (h, ip))
    return h


def _http_get_public(url, timeout, max_bytes=8 * 1024 * 1024):
    """下载公网 URL：仅 http/https；host 双重校验后用 http.client 直连
    （不跟随自动重定向，逐跳重新校验；https 走系统证书校验）。"""
    import ssl
    current = str(url)
    for _ in range(4):
        p = urllib.parse.urlsplit(current)
        if p.scheme not in ("http", "https"):
            raise ValueError("bad scheme")
        port = p.port or (443 if p.scheme == "https" else 80)
        host = _assert_public_host(p.hostname, port)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0 qq-bot"})
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                loc = resp.headers.get("Location")
                resp.read()
                if not loc:
                    raise ValueError("redirect without location")
                current = urllib.parse.urljoin(current, loc)
                continue
            if resp.status != 200:
                raise ValueError("http_%d" % resp.status)
            return resp.read(max_bytes)
        finally:
            conn.close()
    raise ValueError("too many redirects")


def _sticker_thumb_bytes(pool, sid):
    """按 pool+id 读表情包缩略图文件（路径组件校验 + 归属 stickers 根目录）。
    返回 (content_type, bytes) 或抛 ValueError。"""
    data = load_json_const(STICKERS_PATH, {})
    entry = data.get(pool) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise ValueError("库不存在")
    name = ""
    for it in entry.get("items", []):
        if it.get("id") == sid:
            name = str(it.get("file", ""))
            break
    if not name:
        raise ValueError("条目不存在")
    import re as _re
    _BAD_SEG = _re.compile(r'[/:*?"<>|\x00]')
    parts = [q for q in name.replace("\\", "/").split("/") if q]
    if not parts or any(len(q) > 80 or _BAD_SEG.search(q) or q in (".", "..") for q in parts):
        raise ValueError("文件名非法")
    ext = os.path.splitext(parts[-1])[1].lower()
    ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext)
    if not ctype:
        raise ValueError("非图片类型")
    root = os.path.realpath(str(STICKERS_ROOT))
    path = os.path.realpath(os.path.join(root, *parts))
    if not path.startswith(root + os.sep):
        raise ValueError("路径越界")
    raw = pathlib.Path(path).read_bytes()
    return ctype, raw


def _napcat_action(action, params, timeout_sec=25):
    """连 NapCat WS 调用一次 action 并等待 echo 响应（复用 daemon 的 WS 客户端）。"""
    import threading as _th
    import qq_daemon as QD
    token = read_const(WS_TOKEN_PATH)
    box = {}

    def cb(ev):
        if isinstance(ev, dict) and ev.get("echo") == "ui_sync":
            box["resp"] = ev

    ws = QD.WSClient("ws://127.0.0.1:%d" % C.load_app_config()["napcat_ws_port"],
                     token, on_event=cb)
    _th.Thread(target=ws.run_forever, daemon=True).start()
    try:
        deadline = time.time() + 10
        while not ws.connected and time.time() < deadline:
            time.sleep(0.3)
        if not ws.connected:
            raise RuntimeError("NapCat WS 未连接（机器人未启动？）")
        ws.send_text(json.dumps({"action": action, "params": params,
                                 "echo": "ui_sync"}, ensure_ascii=False))
        deadline = time.time() + timeout_sec
        while "resp" not in box and time.time() < deadline:
            time.sleep(0.3)
        if "resp" not in box:
            raise TimeoutError("NapCat 响应超时")
        return box["resp"]
    finally:
        ws.close()


def sync_qq_favorite_stickers(pool, count):
    """拉取当前登录 QQ 的收藏表情 URL 并下载入库（按 URL 去重）。"""
    import hashlib
    import qq_daemon as QD
    resp = _napcat_action("fetch_custom_face", {"count": int(count)})
    if resp.get("retcode") not in (0, 1):
        raise RuntimeError("fetch_custom_face 失败: %s" % resp.get("wording"))
    urls = resp.get("data") or []
    if not isinstance(urls, list):
        raise RuntimeError("返回结构异常")
    data = load_json_const(STICKERS_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = data.get(pool)
    if not isinstance(entry, dict):
        entry = {"next_id": 1, "items": []}
        data[pool] = entry
    known_urls = {str(it.get("url", "")) for it in entry.get("items", [])}
    added, failed = 0, 0
    subdir = pathlib.Path(STICKERS_ROOT) if pool == "_global" \
        else pathlib.Path(STICKERS_ROOT) / pool
    subdir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        url = str(url)
        if url in known_urls:
            continue
        try:
            raw = _http_get_public(url, timeout=15)
            ext = _sniff_image_ext(raw) or ".gif"
            name = "f_%s%s" % (hashlib.md5(url.encode()).hexdigest()[:12], ext)
            (subdir / name).write_bytes(raw)
            sid = int(entry.get("next_id", 1))
            entry["next_id"] = sid + 1
            entry["items"].append({
                "id": sid,
                "file": (name if pool == "_global" else pool + "/" + name),
                "pool": "" if pool == "_global" else pool,
                "tags": ["QQ收藏"], "desc": "QQ收藏表情", "uses": 0,
                "url": url, "added": int(time.time())})
            known_urls.add(url)
            added += 1
        except Exception:
            failed += 1
    write_const(STICKERS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    return added, failed, len(urls)


def proxy_get(host, port, path, token="", timeout=2.5):
    headers = {"Accept": "application/json"}
    if token:
        headers["X-QQBot-Token"] = token
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        conn.close()


def proxy_post(host, port, path, body, token="", timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-QQBot-Token"] = token
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers=headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8"))
    except ConnectionRefusedError:
        # 机器人服务没起来（用户截图反馈：原始报错看不懂）——给人话
        return {"ok": False,
                "error": "机器人服务未启动：请到「状态」页点「启动机器人」后再试"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        conn.close()


class UiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_bytes(self, content_type, raw):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, tk=""):
        token = ui_token()
        # strip：用户从记事本复制令牌常带末尾换行/空格，逐字比对会永远落锁
        page = PAGE.replace("__UITOKEN__", token if tk.strip() == token else "")
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, u, q):
        """API 鉴权：X-UI-Token 头或 ?tk= 与令牌一致。页面本身始终可加载（锁定态）。"""
        want = ui_token().strip()
        # strip：粘贴的令牌/URL 参数可能带首尾空白（记事本复制常见），容错比对
        got = (self.headers.get("X-UI-Token", "") or str(q.get("tk") or "")).strip()
        return got == want

    def handle_one_request(self):
        # 兜底：任何 handler 异常都返回 JSON 而不是让连接挂断——
        # 否则前端只能显示无信息的「网络错误」，真实原因被埋没
        try:
            super().handle_one_request()
        except Exception as e:
            try:
                self._reply(500, {"ok": False, "error": "%r" % e})
            except Exception:
                pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/" or u.path == "/index.html":
            return self._html(str(q.get("tk") or ""))
        if not self._authed(u, q):
            return self._reply(401, {"ok": False, "error": "unauthorized"})
        if u.path == "/api/daemon-status":
            return self._reply(200, proxy_get("127.0.0.1", _daemon_port(), "/status",
                                              read_const(TOKEN_PATH)))
        if u.path == "/api/agent-status":
            r = proxy_get("127.0.0.1", _agent_port(), "/admin/status")
            return self._reply(200, r if r.get("ok") else {"ok": False})
        if u.path == "/api/power/status":
            _boot_state_load()
            with _boot_lock:
                snap = {"running": _boot_state["running"], "done": _boot_state["done"],
                        "failed": _boot_state["failed"], "final": _boot_state["final"],
                        "started_at": _boot_state["started_at"],
                        "steps": list(_boot_state["steps"])}
            return self._reply(200, {"ok": True, **snap})
        if u.path == "/api/group-modes":
            data = load_json_const(GROUP_MODES_PATH, {})
            modes = {str(k): v for k, v in data.items() if isinstance(v, dict)} \
                if isinstance(data, dict) else {}
            return self._reply(200, {"ok": True, "modes": modes})
        if u.path == "/api/keys":
            data = load_json_const(KEYS_PATH, {})
            keys = data.get("keys") if isinstance(data.get("keys"), list) else []
            primary = str(data.get("primary", "")).strip()
            # 一次性迁移：尚无备用 Key 且旧主 Key 文件存在 → 收编为 k1 并设主
            if not keys:
                legacy = ""
                try:
                    with open(os.path.join(C.DATA_DIR, "zhipu_api_key"),
                              "r", encoding="utf-8") as f:
                        legacy = f.read().strip()
                except OSError:
                    pass
                if legacy:
                    keys = [{"id": "k1", "label": "默认", "key": legacy}]
                    primary = "k1"
                    write_const(KEYS_PATH, json.dumps(
                        {"keys": keys, "primary": primary},
                        ensure_ascii=False, indent=2))
            out = []
            for e in keys:
                if isinstance(e, dict) and e.get("id"):
                    k = str(e.get("key", ""))
                    prov = str(e.get("provider") or "zhipu")
                    out.append({"id": e["id"], "label": str(e.get("label", "")),
                                "mask": (k[:6] + "…" + k[-4:]) if len(k) > 14 else "…",
                                "primary": e["id"] == primary,
                                "provider": prov,
                                "provider_label": C.API_PROVIDERS.get(
                                    prov, {}).get("label", "自定义"),
                                "base_url": str(e.get("base_url", ""))[:120]
                                if prov == "custom" else ""})
            return self._reply(200, {"ok": True, "keys": out})
        if u.path == "/api/app-info":
            cfg = C.load_app_config()
            with _update_lock:
                cached = dict(_update_cache) if _update_cache else None
            return self._reply(200, {
                "ok": True, "version": C.APP_VERSION,
                "frozen": bool(getattr(sys, "frozen", False)),
                "repo": cfg.get("update_repo") or C.DEFAULT_UPDATE_REPO,
                "update_check": bool(cfg.get("update_check", True)),
                "has_update_token": bool(str(cfg.get("update_token") or "").strip()),
                "cached_update": cached})
        if u.path == "/api/update/check":
            import qq_update as _U
            info = _U.check(C.load_app_config())
            if info.get("ok"):
                with _update_lock:
                    _update_cache.clear()
                    _update_cache.update(info)
            return self._reply(200, info)
        if u.path == "/api/update/status":
            import qq_update as _U
            return self._reply(200, {"ok": True, **_U.status()})
        if u.path == "/api/napcat-dl-status":
            import qq_napcat_dl as _ND
            return self._reply(200, {"ok": True, **_ND.status()})
        if u.path == "/api/qq-compat":
            lvl, msg = S.napcat_compat_check()
            cfg_c = C.load_app_config()
            return self._reply(200, {"ok": True, "level": lvl, "message": msg,
                                     "qq_version": C.qq_current_version(),
                                     "lock_active": S.qq_update_block_active(),
                                     "lock_wanted": bool(cfg_c.get(
                                         "qq_version_lock", True)),
                                     "napcat_mode": str(cfg_c.get("napcat_mode") or "qqnt")})
        if u.path == "/api/napcat-runtime":
            import qq_napcat_runtime as _RT
            cfg_c = C.load_app_config()
            return self._reply(200, {"ok": True,
                                     "mode": str(cfg_c.get("napcat_mode") or "qqnt"),
                                     **_RT.status()})
        if u.path == "/api/napcat-login-info":
            cfg = C.load_app_config()
            cfg_dir = S.napcat_config_dir(cfg)
            tok = S.napcat_webui_token(cfg)
            src = ""
            if tok:
                src = ("NapCat 配置 webui.json" if cfg_dir and os.path.isfile(
                    os.path.join(cfg_dir, "webui.json")) else "NapCat 控制台日志")
            base = "http://127.0.0.1:6099/webui"
            return self._reply(200, {
                "ok": True,
                "webui_up": S.port_open(6099),
                "ws_ready": S.port_open(int(cfg.get("napcat_ws_port") or 3001)),
                "token": tok, "source": src,
                "url": (base + "?token=" + urllib.parse.quote(tok, safe=""))
                if tok else base})
        if u.path == "/api/runlog":
            src = q.get("src", "all")
            if src not in ("all", "daemon", "agent", "boot", "napcat"):
                src = "all"
            return self._reply(200, {"ok": True, "src": src,
                                     "lines": collect_runlog(src, tail=250)})
        if u.path == "/api/config":
            cfg = load_json_const(MODEL_CHAIN_PATH, {"models": [], "paused": False})
            return self._reply(200, {"ok": True, "models": cfg.get("models", []),
                                     "vision_model": cfg.get("vision_model", ""),
                                     "thinking": cfg.get("thinking", {})})
        if u.path == "/api/groups":
            data = load_json_const(GROUPS_PATH, {})
            return self._reply(200, {"ok": True,
                                     "enabled": bool(data.get("enabled", True)),
                                     "allow_all": bool(data.get("allow_all", False)),
                                     "groups": data.get("groups", [])})
        if u.path == "/api/private-config":
            data = load_json_const(PRIVATE_CONFIG_PATH, {})
            return self._reply(200, {"ok": True,
                                     "enabled": bool(data.get("enabled", False)),
                                     "allow_all": bool(data.get("allow_all", True)),
                                     "qq_whitelist": data.get("qq_whitelist", []),
                                     "typing_enabled": bool(data.get("typing_enabled", False)),
                                     "typing_seconds": max(1, min(10, int(data.get("typing_seconds") or 2)))})
        if u.path == "/api/personas":
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            active = str(data.get("_active", ""))
            names = [k for k in data.keys() if not k.startswith("_")]
            return self._reply(200, {"ok": True, "active": active, "names": names,
                                     "global": str(data.get("_global", "")),
                                     "text": str(data.get(active, ""))})
        if u.path == "/api/personas/detail":
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            name = q.get("name", "")
            if not name or name.startswith("_") or name not in data:
                return self._reply(400, {"ok": False, "error": "人设不存在"})
            return self._reply(200, {"ok": True, "name": name, "text": str(data.get(name, ""))})
        if u.path == "/api/personas/migrate-cmd":
            import qq_persona_io as _PIO
            return self._reply(200, {"ok": True, "command": _PIO.migrate_command()})
        if u.path == "/api/evolved":
            data = load_json_const(EVOLVED_PATH, {})
            personas = load_json_const(PERSONA_COLLECTION_PATH, {})
            active = str(personas.get("_active", "")) if isinstance(personas, dict) else ""
            rows = []
            if isinstance(personas, dict):
                for name in personas.keys():
                    if str(name).startswith("_"):
                        continue
                    entry = data.get(name) if isinstance(data, dict) else None
                    notes = [str(n.get("note", "")) for n in (entry or {}).get("notes", [])
                             if isinstance(n, dict) and n.get("note")] if isinstance(entry, dict) else []
                    rows.append({"name": str(name), "active": name == active,
                                 "enabled": bool(entry.get("enabled", True)) if isinstance(entry, dict) else True,
                                 "notes": notes})
            cfg2 = C.load_app_config()
            try:
                maxn = max(30, min(300, int(cfg2.get("evolved_max_notes") or 30)))
            except (TypeError, ValueError):
                maxn = 30
            try:
                inj = max(4, min(40, int(cfg2.get("evolved_inject_notes") or 12)))
            except (TypeError, ValueError):
                inj = 12
            return self._reply(200, {"ok": True, "personas": rows,
                                     "max_notes": maxn,
                                     "inject_notes": inj})
        if u.path == "/api/file":
            kind = q.get("kind")
            if kind == "persona":
                data = load_json_const(PERSONA_COLLECTION_PATH, {})
                return self._reply(200, {"ok": True, "text": str(data.get(str(data.get("_active", "")), ""))})
            if kind == "style":
                return self._reply(200, {"ok": True, "text": read_const(STYLE_PATH)})
            return self._reply(400, {"ok": False, "error": "kind 必须是 persona|style"})
        if u.path == "/api/memory":
            items = []
            try:
                with open(MEMORY_STREAM_PATH, "r", encoding="utf-8") as f:
                    for i, ln in enumerate(f):
                        try:
                            r = json.loads(ln)
                        except ValueError:
                            continue
                        r["idx"] = i  # 文件全局行号（编辑/删除定位用）
                        r["time"] = time.strftime(
                            "%m-%d %H:%M", time.localtime(r.get("ts", 0)))
                        items.append(r)
            except OSError:
                pass
            return self._reply(200, {"ok": True, "items": items[-100:]})
        if u.path == "/api/member-profiles":
            try:
                d = load_json_const(MEMBER_PROFILES_PATH, {})
            except Exception:
                d = {}
            members = d.get("members") if isinstance(d, dict) else {}
            rows = []
            for qq, m in (members or {}).items():
                if not isinstance(m, dict):
                    continue
                rows.append({"qq": str(qq), "name": str(m.get("name") or ""),
                             "note": str(m.get("note") or ""),
                             "groups": [str(g) for g in (m.get("groups") or [])],
                             "updated": int(m.get("updated") or 0)})
            rows.sort(key=lambda x: (-x["updated"], x["qq"]))
            return self._reply(200, {"ok": True, "members": rows})
        if u.path == "/api/plugins/market":
            try:
                import qq_plugins as _qp
                ml = _qp.market_list()
                if not ml.get("ok"):
                    return self._reply(200, ml)
                host = _qp.PluginHost()
                local = host.scan()  # {name: (manifest, dir, builtin)}
                for it in ml["plugins"]:
                    loc = local.get(it["name"])
                    it["installed"] = bool(loc)
                    it["builtin"] = bool(loc and loc[2])
                    if loc:
                        it["local_version"] = str((loc[0] or {}).get("version") or "")
                return self._reply(200, ml)
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/plugins":
            import qq_plugins as _qp
            host = _qp.PluginHost()
            out = []
            for name, (mf, pdir, builtin) in sorted(host.scan().items()):
                mf = dict(mf) if isinstance(mf, dict) else {}
                st = host._state()
                en_map = st.get("enabled") if isinstance(st.get("enabled"), dict) else {}
                set_map = (st.get("settings") or {}).get(name) if isinstance(st.get("settings"), dict) else {}
                defaults = {s.get("key"): s.get("default")
                            for s in (mf.get("settings") or []) if isinstance(s, dict)}
                values = dict(defaults)
                if isinstance(set_map, dict):
                    values.update(set_map)
                out.append({"name": name, "title": str(mf.get("title") or name),
                            "category": str(mf.get("category") or "其他"),
                            "description": str(mf.get("description") or ""),
                            "version": str(mf.get("version") or ""),
                            "builtin": builtin,
                            "default_enabled": bool(mf.get("default_enabled", False)),
                            "enabled": host.enabled(name, bool(mf.get("default_enabled", False))),
                            "settings": mf.get("settings") or [],
                            "values": values,
                            "page": mf.get("page") or None,
                            "page_js": os.path.isfile(os.path.join(str(pdir), "page.js")),
                            # 消耗预估（可选）：manifest 声明 cost_estimate.route
                            # → 前端对启用插件调该 route 取 {"pct": 数字}
                            "cost_route": (mf.get("cost_estimate") or {}).get("route") or ""})
            return self._reply(200, {"ok": True, "plugins": out})
        if u.path == "/api/daily-summaries":
            # 分天存储的日报+热点历史（daily_summaries/<date>.json）
            sdir = pathlib.Path(DATA_DIR) / "daily_summaries"
            date = str(q.get("date") or "").strip()
            if date:
                p = sdir / (date[:10] + ".json")
                if not p.is_file():
                    return self._reply(404, {"ok": False, "error": "该日无记录"})
                try:
                    return self._reply(200, json.loads(
                        p.read_text(encoding="utf-8")))
                except (OSError, ValueError) as e:
                    return self._reply(200, {"ok": False, "error": str(e)[:120]})
            out = []
            if sdir.is_dir():
                for f in sorted(sdir.glob("*.json"), reverse=True):
                    try:
                        d = json.loads(f.read_text(encoding="utf-8"))
                        out.append({"date": d.get("date") or f.stem[:10],
                                    "report": str(d.get("report") or "")[:80],
                                    "hot": str(d.get("hot") or "")[:60],
                                    "stats": d.get("stats") or {}})
                    except (OSError, ValueError):
                        continue
            return self._reply(200, {"ok": True, "days": out[:90]})
        if u.path == "/api/plugins/pagejs":
            # 插件包自带页面脚本（随插件 zip 分发，更新插件即更新页面）
            import qq_plugins as _qp
            name = str(q.get("name") or "").strip()
            if not _qp._safe_name(name):
                return self._reply(400, {"ok": False, "error": "插件名非法"})
            host = _qp.PluginHost()
            for nm, (_mf, pdir, _b) in host.scan().items():
                if nm == name:
                    pj = os.path.join(str(pdir), "page.js")
                    if os.path.isfile(pj):
                        try:
                            return self._reply(200, {"ok": True, "js":
                                pathlib.Path(pj).read_text(encoding="utf-8")[:200000]})
                        except OSError as e:
                            return self._reply(200, {"ok": False, "error": repr(e)})
                    return self._reply(200, {"ok": False, "error": "该插件不带 page.js"})
            return self._reply(404, {"ok": False, "error": "插件不存在"})
        if u.path == "/api/persona-routes":
            d = load_json_const(PERSONA_ROUTES_PATH, {})
            routes = (d.get("routes") or []) if isinstance(d, dict) else []
            return self._reply(200, {"ok": True,
                                     "routes": [r for r in routes if isinstance(r, dict)],
                                     "active": str(load_json_const(
                                         PERSONA_COLLECTION_PATH, {}).get("_active", ""))})
        if u.path == "/api/bili/status":
            try:
                import qq_bilibili as _qb
                import qq_plugins as _qp
                cfg0 = C.load_app_config()
                ph = _qp.PluginHost()
                def _ps(k, dv):
                    return ph.setting("bili_scroll", k, dv)
                return self._reply(200, {"ok": True,
                                         "uid": _qb.logged_in_uid(),
                                         "owner_uid": str(cfg0.get("owner_bilibili_uid") or ""),
                                         "brief": _qb.brief_load(),
                                         "share": {"share_enabled": bool(_ps("share_enabled", False)),
                                                   "share_bili_owner": bool(_ps("share_bili_owner", _ps("share_bili_dm", False))),
                                                   "share_bili_others": bool(_ps("share_bili_others", False)),
                                                   "share_bili_uids": str(_ps("share_bili_uids", "") or ""),
                                                   "share_qq_owner": bool(_ps("share_qq_owner", _ps("share_qq", False))),
                                                   "share_qq_others": bool(_ps("share_qq_others", False)),
                                                   "share_qq_users": str(_ps("share_qq_users", "") or ""),
                                                   "share_qq_group": bool(_ps("share_qq_group", False)),
                                                   "share_qq_groups": str(_ps("share_qq_groups", "") or "")}})
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/chatimport/status":
            # 聊天记录隔离库状态（qq/wx 两源共用端点；会话仅存 DATA_DIR）
            import qq_chatimport as _CI
            src = q.get("source", "qq")
            return self._reply(200, {"ok": True, "source": src,
                                     "label": _CI.SOURCES.get(src, {}).get("label", ""),
                                     "hint": _CI.SOURCES.get(src, {}).get("hint", ""),
                                     "probe": _CI.probe(src),
                                     "sessions": _CI.sessions(src)})
        if u.path == "/api/bili/qr":
            p = pathlib.Path(os.path.join(C.DATA_DIR, "bilibili_qr.png"))
            if p.exists():
                return self._reply_bytes("image/png", p.read_bytes())
            return self._reply(404, {"ok": False, "error": "二维码不存在"})
        if u.path == "/api/stickers/thumb":
            try:
                ctype, raw = _sticker_thumb_bytes(q.get("pool", "_global"),
                                                  int(q.get("id", "0")))
                return self._reply_bytes(ctype, raw)
            except Exception as e:
                body = json.dumps({"ok": False, "error": repr(e)}).encode("utf-8")
                return self._reply_bytes("application/json", body)
        if u.path == "/api/stickers":
            pool = q.get("pool", "_global")
            data = load_json_const(STICKERS_PATH, {})
            items = _sticker_pool_items(data, pool)
            bot_qq = str(C.load_app_config().get("bot_qq") or "").strip()
            fav_pool = ("fav_" + bot_qq) if bot_qq else ""
            fav_present = bool(fav_pool) and isinstance(data.get(fav_pool), dict) \
                and data[fav_pool].get("items")
            pools = [{"id": "_global", "label": "通用库"}]
            personas = load_json_const(PERSONA_COLLECTION_PATH, {})
            if isinstance(personas, dict):
                for n in personas.keys():
                    if not str(n).startswith("_"):
                        pools.append({"id": str(n), "label": "%s（人设库）" % n})
            if fav_present:
                pools.append({"id": fav_pool,
                              "label": "%s的收藏表情（QQ收藏）" % bot_qq, "fav": True})
            settings = data.get("_settings") if isinstance(data.get("_settings"), dict) else {}
            return self._reply(200, {"ok": True, "pool": pool, "items": items,
                                     "pools": pools, "fav_pool": fav_pool if fav_present else "",
                                     "fav_enabled": bool(settings.get("fav_enabled", True)),
                                     "bot_qq": bot_qq})
        if u.path == "/api/app-config":
            cfg = C.load_app_config()
            cfg["detected_qqnt"] = C.detect_qqnt_dir()
            cfg["autostart_with_windows"] = S.autostart_enabled()
            cfg["zcode_installed"] = C.zcode_installed()
            qqnt = C.qqnt_dir_resolved(cfg)
            cfg["napcat_ok"] = bool(qqnt and os.path.isfile(
                os.path.join(qqnt, "launcher-user.bat")))
            return self._reply(200, {"ok": True, **cfg})
        if u.path == "/api/apikey-status":
            key = read_const(API_KEY_PATH)
            mask = (key[:6] + "..." + key[-4:]) if len(key) >= 12 else ("已设置" if key else "")
            return self._reply(200, {"ok": True, "set": bool(key), "mask": mask})
        if u.path == "/api/autostart":
            return self._reply(200, {"ok": True, "enabled": S.autostart_enabled()})
        if u.path == "/api/cache/stats":
            return self._reply(200, S.cache_stats())
        if u.path == "/api/task/status":
            tid = str(q.get("id") or "")
            with _TASKS_LOCK:
                t = _TASKS.get(tid)
                t = dict(t) if t else None
            if not t:
                return self._reply(404, {"ok": False, "error": "任务不存在"})
            return self._reply(200, t)
        if u.path == "/api/voice/meta":
            # 语音回复元数据：供应商（含计费说明）+ 预设音色表 + 本地引擎状态
            import qq_tts as _T
            import qq_gsv_ft as _F
            return self._reply(200, {
                "ok": True,
                "providers": {k: {"label": v["label"], "billing": v["billing"]}
                              for k, v in _T.PROVIDERS.items()},
                "edge_voices": _T.EDGE_VOICES,
                "zhipu_voices": _T.ZHIPU_VOICES,
                "sf_models": _T.SF_MODELS,
                "sf_voices": _T.SF_VOICES,
                "config": _T.voice_cfg(),
                "gsv": _T.gsv_status(_T.voice_cfg()),
                "gsv_voices": _T.gsv_voice_list(),
                "gsv_ft_models": [{"id": str(mid), "name": str(m.get("name") or mid)}
                                  for mid, m in (_F.load_index() or {}).items()],
            })
        if u.path == "/api/lark/meta":
            # 飞书互通：配置 + SDK 可用性
            import qq_lark as _L
            c = _L.cfg()
            c = dict(c)
            c.pop("app_secret", None)
            return self._reply(200, {"ok": True, "config": c,
                                     "sdk": _L.sdk_available()})
        if u.path == "/api/lark/chats":
            # 拉取机器人所在的飞书群（设置页下拉）
            import qq_lark as _L
            try:
                return self._reply(200, {"ok": True, "chats": _L.list_chats()})
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:200]})
        if u.path == "/api/usage":
            usage_path = pathlib.Path(os.path.join(DATA_DIR, "usage_log.jsonl"))
            now = time.time()
            bounds = {"d30": now - 30 * 86400, "d7": now - 7 * 86400}
            today_start = time.mktime(time.strptime(
                time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            totals = {"total_tokens": 0, "total_calls": 0,
                      "d30_tokens": 0, "d30_calls": 0,
                      "d7_tokens": 0, "d7_calls": 0,
                      "today_tokens": 0, "today_calls": 0}
            daily = {}
            hourly = {}
            by_model = {}
            try:
                for ln in usage_path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    ts = float(r.get("ts", 0) or 0)
                    mt = int(r.get("total_tokens", 0) or 0)
                    mp = int(r.get("prompt_tokens", 0) or 0)
                    mc = int(r.get("completion_tokens", 0) or 0)
                    model = str(r.get("model", "?"))
                    totals["total_tokens"] += mt
                    totals["total_calls"] += 1
                    if ts >= bounds["d30"]:
                        totals["d30_tokens"] += mt
                        totals["d30_calls"] += 1
                        day = time.strftime("%Y-%m-%d", time.localtime(ts))
                        de = daily.setdefault(day, {"date": day, "tokens": 0, "calls": 0})
                        de["tokens"] += mt
                        de["calls"] += 1
                    if ts >= bounds["d7"]:
                        totals["d7_tokens"] += mt
                        totals["d7_calls"] += 1
                    if ts >= today_start:
                        totals["today_tokens"] += mt
                        totals["today_calls"] += 1
                        hour = time.strftime("%H:00", time.localtime(ts))
                        he = hourly.setdefault(hour, {"hour": hour, "tokens": 0, "calls": 0})
                        he["tokens"] += mt
                        he["calls"] += 1
                    e = by_model.setdefault(model, {"model": model, "calls": 0,
                                                    "prompt": 0, "completion": 0,
                                                    "total": 0, "_last": 0})
                    e["calls"] += 1
                    e["prompt"] += mp
                    e["completion"] += mc
                    e["total"] += mt
                    e["_last"] = max(e["_last"], ts)
            except OSError:
                pass
            rows = []
            for e in sorted(by_model.values(), key=lambda x: -x["total"]):
                rows.append({"model": e["model"], "calls": e["calls"],
                             "prompt": e["prompt"], "completion": e["completion"],
                             "total": e["total"],
                             "last": time.strftime("%m-%d %H:%M",
                                                   time.localtime(e["_last"])) if e["_last"] else "-"})
            # 近30天逐日序列（无数据日补零，供趋势曲线）
            daily_rows = []
            for i in range(29, -1, -1):
                day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
                de = daily.get(day, {"date": day, "tokens": 0, "calls": 0})
                daily_rows.append({"date": day, "tokens": de["tokens"], "calls": de["calls"]})
            now_hour = int(time.strftime("%H", time.localtime(now)))
            hourly_rows = []
            for h in range(0, now_hour + 1):
                key = "%02d:00" % h
                he = hourly.get(key, {"hour": key, "tokens": 0, "calls": 0})
                hourly_rows.append({"label": key, "tokens": he["tokens"], "calls": he["calls"]})
            return self._reply(200, {"ok": True, **totals, "by_model": rows,
                                     "daily": daily_rows, "hourly": hourly_rows})
        if u.path == "/api/reply-config":
            data = load_json_const(REPLY_CONFIG_PATH, {})
            try:
                _vp = min(1.0, max(0.0, float(data.get("vision_prob", 0.3))))
            except (TypeError, ValueError):
                _vp = 0.3
            return self._reply(200, {"ok": True,
                                     "reply_mode": data.get("reply_mode", "standard"),
                                     "keywords": data.get("keywords", []),
                                     "chat_probability": data.get("chat_probability", 0.15),
                                     "chat_interesting": data.get("chat_interesting", True),
                                     "chat_min_interval_sec": data.get("chat_min_interval_sec", 45),
                                     "vision_enabled": data.get("vision_enabled", False),
                                     "vision_mode": str(data.get("vision_mode") or "all")[:8],
                                     "vision_prob": _vp,
                                     "chat_anti_interrupt": data.get("chat_anti_interrupt", True),
                                     "web_search_enabled": data.get("web_search_enabled", False),
                                     "abuse_guard_enabled": data.get("abuse_guard_enabled", True),
                                     "task_bridge_enabled": data.get("task_bridge_enabled", True),
                                     "remote_control_enabled": data.get("remote_control_enabled", True),
                                     "draw_enabled": data.get("draw_enabled", True),
                                     "daily_review_enabled": data.get("daily_review_enabled", True),
                                     "econ_enabled": data.get("econ_enabled", False),
                                     "econ_manual": bool(data.get("econ_manual", False)),
                                     "econ_start": data.get("econ_start", "14:00"),
                                     "econ_end": data.get("econ_end", "18:00"),
                                     "econ_days": data.get("econ_days", [0, 1, 2, 3, 4]),
                                     "econ_active": C.econ_active(data),
                                     "proactive_groups_enabled": data.get("proactive_groups_enabled", False),
                                     "proactive_private_enabled": data.get("proactive_private_enabled", False),
                                     "proactive_groups_probability": data.get("proactive_groups_probability",
                                                                               data.get("proactive_probability", 0.1)),
                                     "proactive_private_probability": data.get("proactive_private_probability",
                                                                                data.get("proactive_probability", 0.1)),
                                     "proactive_groups_min_interval_sec": data.get("proactive_groups_min_interval_sec",
                                                                                   data.get("proactive_min_interval_sec", 1800)),
                                     "proactive_private_min_interval_sec": data.get("proactive_private_min_interval_sec",
                                                                                    data.get("proactive_min_interval_sec", 1800)),
                                     "proactive_groups_silent_start": data.get("proactive_groups_silent_start", "22:00"),
                                     "proactive_groups_silent_end": data.get("proactive_groups_silent_end", "08:00"),
                                     "proactive_hourly_cap": data.get("proactive_hourly_cap", 2),
                                     "nudge_enabled": bool(data.get("nudge_enabled", True)),
                                     "nudge_group_enabled": bool(data.get("nudge_group_enabled", False)),
                                     "nudge_delay_min_sec": data.get("nudge_delay_min_sec", 150),
                                     "nudge_delay_max_sec": data.get("nudge_delay_max_sec", 300),
                                     "nudge_max_strikes": data.get("nudge_max_strikes", 2),
                                     "env_pulse_enabled": bool(data.get("env_pulse_enabled", False)),
                                     "member_profiles_enabled": bool(data.get("member_profiles_enabled", False)),
                                     "bili_scroll_enabled": bool(data.get("bili_scroll_enabled", False)),
                                     "bili_share_enabled": bool(data.get("bili_share_enabled", False)),
                                     "bili_scroll_window_min": data.get("bili_scroll_window_min", 180),
                                     "bili_scroll_keywords": str(data.get("bili_scroll_keywords", "")),
                                     "lean_ctx_enabled": data.get("lean_ctx_enabled", True),
                                     "dyn_ctx_enabled": data.get("dyn_ctx_enabled", True),
                                     "wait_gate_enabled": data.get("wait_gate_enabled", True),
                                     "burst_merge_enabled": data.get("burst_merge_enabled", True),
                                     "botloop_guard_enabled": data.get("botloop_guard_enabled", True),
                                     "botloop_cooldown_min": data.get("botloop_cooldown_min", 60),
                                     "proactive_private_silent_start": data.get("proactive_private_silent_start", "22:00"),
                                     "proactive_private_silent_end": data.get("proactive_private_silent_end", "08:00"),
                                     "token_limit_enabled": data.get("token_limit_enabled", False),
                                     "token_limit_hours": data.get("token_limit_hours", 1),
                                     "token_limit_max": data.get("token_limit_max", 100000)})
        if u.path == "/api/token-limit":
            cfg = load_json_const(REPLY_CONFIG_PATH, {})
            enabled = bool(cfg.get("token_limit_enabled"))
            try:
                hours = max(1, min(24, int(cfg.get("token_limit_hours", 1))))
                cap = int(cfg.get("token_limit_max", 0))
            except (TypeError, ValueError):
                hours, cap = 1, 0
            cutoff = time.time() - hours * 3600
            used = 0
            try:
                with open(os.path.join(C.DATA_DIR, "usage_log.jsonl"),
                          "r", encoding="utf-8") as f:
                    for ln in f:
                        try:
                            r = json.loads(ln)
                        except ValueError:
                            continue
                        if r.get("ts", 0) >= cutoff:
                            used += int(r.get("total_tokens", 0) or 0)
            except OSError:
                pass
            return self._reply(200, {"ok": True, "enabled": enabled, "used": used,
                                     "cap": cap, "hours": hours,
                                     "hold": bool(enabled and cap > 0 and used >= cap)})
        if u.path == "/api/group-names":
            r = proxy_get("127.0.0.1", _daemon_port(),
                          "/group-list" + ("?refresh=1" if q.get("refresh") else ""),
                          read_const(TOKEN_PATH), timeout=15)
            return self._reply(200, r if isinstance(r, dict) else {"ok": False})
        if u.path == "/api/friend-names":
            r = proxy_get("127.0.0.1", _daemon_port(),
                          "/friend-list" + ("?refresh=1" if q.get("refresh") else ""),
                          read_const(TOKEN_PATH), timeout=15)
            return self._reply(200, r if isinstance(r, dict) else {"ok": False})
        if u.path == "/api/zcode-status":
            import subprocess as _sp
            running = False
            try:
                chk = _sp.run(["tasklist", "/FI", "IMAGENAME eq ZCode.exe", "/NH"],
                              capture_output=True, text=True, timeout=8,
                              creationflags=0x08000000)
                running = "zcode.exe" in (chk.stdout or "").lower()
            except (OSError, _sp.SubprocessError):
                pass
            cfg = C.load_app_config()
            return self._reply(200, {"ok": True,
                                     "installed": C.zcode_installed(),
                                     "running": running,
                                     "bridge": bool(cfg.get("zcode_bridge_enabled"))})
        if u.path == "/api/watchdog":
            cfg = C.load_app_config()
            with _wd_lock:
                snap = {"events": list(_wd_state["events"]),
                        "strikes": _wd_state["strikes"],
                        "last_check": _wd_state["last_check"],
                        "last_result": _wd_state["last_result"],
                        "busy": _wd_state["busy"]}
            return self._reply(200, {"ok": True,
                                     "enabled": bool(cfg.get("watchdog_enabled")),
                                     "mode": str(cfg.get("watchdog_mode") or "auto"),
                                     "interval": int(cfg.get("watchdog_interval") or 120),
                                     **snap})
        if u.path == "/api/backups":
            cfg = C.load_app_config()
            root = pathlib.Path(str(cfg.get("backup_dir") or "").strip()
                                or os.path.join(DATA_DIR, "backups"))
            items = []
            try:
                for sub in sorted(root.iterdir(), reverse=True):
                    if not sub.is_dir():
                        continue
                    n = sum(1 for f in C.backup_files_for(cfg)
                            if ((sub / os.path.basename(f)).is_file()
                                or (sub / os.path.basename(f)).is_dir()))
                    if n:
                        items.append({"name": sub.name, "files": n,
                                      "mtime": sub.stat().st_mtime})
            except OSError:
                pass
            return self._reply(200, {"ok": True, "backups": items,
                                     "root": str(root)})
        if u.path == "/api/devctrl-flow":
            import qq_bot_agent as _A
            return self._reply(200, {"ok": True, "flow": _A.devctrl_flow(),
                                     "defaults": _A._DEFAULT_FLOW})
        if u.path == "/api/devctrl-code":
            # 码的唯一有效来源在 agent 进程：UI 只做代理，绝不本地生成。
            r = proxy_get("127.0.0.1", _agent_port(), "/admin/devctrl-code")
            if not r.get("ok"):
                return self._reply(200, {"ok": False, "code": "", "expires_in": 0,
                                         "error": r.get("error", "agent 未运行")})
            return self._reply(200, r)
        return self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if not self._authed(u, q):
            return self._reply(401, {"ok": False, "error": "unauthorized"})
        body = self._body()
        # ---- MCP 服务桥（内置；来源=ZCode 已装服务 + 自定义 http）----
        if u.path == "/api/mcp/meta":
            import qq_mcp as _M
            cfg = _M.mcp_cfg()
            servers = []
            for s in _M.all_servers():
                spec = s["spec"]
                if s["type"] == "stdio":
                    detail = os.path.basename(str(spec.get("command") or ""))[:80]
                else:
                    detail = str(spec.get("url") or "")[:120]
                servers.append({
                    "name": s["name"], "source": s["source"], "type": s["type"],
                    "detail": detail,
                    "supported": s["type"] in ("stdio", "http", "sse"),
                    "on": _M.server_on(s["name"]) if s["type"] in ("stdio", "http", "sse") else False})
            zk_n = len([s for s in servers if s["source"] == "zcode"])
            return self._reply(200, {"ok": True, "enabled": bool(cfg.get("enabled")),
                                     "servers": servers,
                                     "zcode_installed": bool(C.zcode_installed()),
                                     "zcode_config_found": os.path.isfile(_M.ZCODE_CONFIG),
                                     "zcode_mcp_count": zk_n})
        if u.path == "/api/mcp/toggle":
            import qq_mcp as _M
            cfg = _M.mcp_cfg()
            if body.get("global"):
                cfg["enabled"] = bool(body.get("on"))
            else:
                name = str(body.get("name") or "")[:48]
                if not any(s["name"] == name for s in _M.all_servers()):
                    return self._reply(200, {"ok": False, "error": "服务器不存在"})
                ent = dict(cfg["servers"].get(name) or {})
                ent["on"] = bool(body.get("on"))
                cfg["servers"][name] = ent
                if not ent["on"]:
                    _M.reset_client(name)   # 关闭即杀常驻子进程
            _M.save_mcp_cfg(cfg)
            return self._reply(200, {"ok": True})
        if u.path == "/api/mcp/test":
            import qq_mcp as _M
            return self._reply(200, _M.test_server(str(body.get("name") or "")[:48]))
        if u.path == "/api/mcp/add-custom":
            # 表单模式：type/url/token/command/args + 高级字段（headers_lines
            # 每行 "Header: 值"、env_lines 每行 KEY=VALUE、timeout 秒、protocol）
            # JSON 模式：body.spec = 完整服务对象（ZCode 格式直接粘贴兼容：
            # {type,command,args,env,url,headers,timeout(ms 自动换算),protocol}）
            # stdio = 在本机运行用户指定的命令，UI 提示只添加信任来源。
            import re as _re
            import qq_mcp as _M

            def _norm_extras(ent):
                # timeout：秒；>1000 视为毫秒换算（ZCode 存 ms）；限 5-600
                if ent.get("timeout") is not None:
                    try:
                        t = float(ent["timeout"])
                        if t > 1000:
                            t /= 1000.0
                        t = max(5, min(600, int(t)))
                        ent["timeout"] = t
                    except (TypeError, ValueError):
                        ent.pop("timeout", None)
                # protocol：YYYY-MM-DD 形态才收（其余丢弃走默认）
                if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                                      str(ent.get("protocol") or "")):
                    ent.pop("protocol", None)
                return ent

            def _parse_lines_kv(txt, sep_re):
                d = {}
                for ln in str(txt or "").splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    m = _re.match(sep_re, ln)
                    if m:
                        d[m.group(1).strip()[:64]] = m.group(2).strip()[:300]
                return d

            def _build_ent(sp):
                """单个服务对象 → (ent, err)。type 缺省但有 url 按 http；
                streamable_http 等别名经 norm_type 归一。"""
                if not isinstance(sp, dict):
                    return None, "配置需要是对象"
                t = _M.norm_type(sp.get("type")) or (
                    "http" if str(sp.get("url") or "").strip() else "")
                if not t:
                    return None, ("不支持的传输类型 %r（支持 http（含 streamable_http）/ sse / stdio）"
                                  % str(sp.get("type") or "")[:40])
                ent = {"type": t}
                if t == "stdio":
                    cmd = str(sp.get("command") or "").strip()[:200]
                    if not cmd:
                        return None, "stdio 服务缺 command"
                    ent["command"] = cmd
                    a = sp.get("args")
                    if isinstance(a, list):
                        ent["args"] = [str(x)[:200] for x in a][:32]
                    elif isinstance(a, str) and a.strip():
                        ent["args"] = [p.strip('"') for p in _re.findall(
                            r'"[^"]*"|\S+', a.strip())][:32]
                    else:
                        ent["args"] = []
                    if isinstance(sp.get("env"), dict):
                        ent["env"] = {str(k)[:64]: str(v)[:300]
                                      for k, v in list(sp["env"].items())[:32]}
                else:
                    url = str(sp.get("url") or "").strip()[:300]
                    if not (url.startswith("http://") or url.startswith("https://")):
                        return None, "url 需以 http(s):// 开头"
                    ent["url"] = url
                    if isinstance(sp.get("headers"), dict):
                        ent["headers"] = {str(k)[:64]: str(v)[:300]
                                          for k, v in list(sp["headers"].items())[:24]}
                if sp.get("timeout") is not None:
                    ent["timeout"] = sp["timeout"]
                if sp.get("protocol"):
                    ent["protocol"] = sp["protocol"]
                return _norm_extras(ent), ""

            def _dup(nm):
                return any(s["name"] == nm for s in _M.all_servers())

            def _safe_name(k):
                """服务名必须 ASCII（进 [MCP] 动作行协议）；中文键名转安全名。"""
                s = "".join(ch for ch in str(k)
                            if ch.isascii() and (ch.isalnum() or ch in "-_.")) \
                    .strip("-_.")[:24]
                import hashlib as _hl
                base = s if _re.fullmatch(r"[A-Za-z0-9_\-.]{1,24}", s) \
                    else ("mcp" + _hl.md5(str(k).encode("utf-8")).hexdigest()[:6])
                nm, n = base, 2
                while _dup(nm):
                    nm = "%s-%d" % (base, n)
                    n += 1
                return nm

            cfg = _M.mcp_cfg()
            spec_obj = body.get("spec")
            inner = spec_obj.get("mcpServers") if isinstance(spec_obj, dict) else None
            if isinstance(inner, dict) and inner:
                # ---- mcpServers 包装（Claude Desktop 配置整段粘贴，可一次多个）----
                items = [(k, v) for k, v in inner.items()
                         if isinstance(v, dict)][:12]
                if not items:
                    return self._reply(200, {"ok": False,
                                             "error": "mcpServers 里没有有效的服务配置"})
                typed = str(body.get("name") or "").strip()[:32]
                added, skipped = [], []
                for k, v in items:
                    nm = typed if (len(items) == 1 and typed) else _safe_name(k)
                    if not _re.fullmatch(r"[A-Za-z0-9_\-.]{1,32}", nm):
                        skipped.append("%s：名称非法" % k); continue
                    if _dup(nm):
                        skipped.append("%s：与现有服务重名" % nm); continue
                    ent, err = _build_ent(v)
                    if err:
                        skipped.append("%s：%s" % (str(k)[:24], err)); continue
                    cfg["custom"][nm] = ent
                    added.append(nm)
                if not added:
                    return self._reply(200, {"ok": False,
                                             "error": ("；".join(skipped)[:300]
                                                       or "没有可添加的服务")})
                _M.save_mcp_cfg(cfg)
                return self._reply(200, {"ok": True, "added": added,
                                         "skipped": skipped[:6]})
            name = str(body.get("name") or "").strip()[:32]
            if not _re.fullmatch(r"[A-Za-z0-9_\-.]{1,32}", name):
                return self._reply(200, {"ok": False,
                                         "error": "名称仅限 1-32 位字母/数字/-_."})
            if _dup(name):
                return self._reply(200, {"ok": False, "error": "与现有服务重名"})
            if isinstance(spec_obj, dict):
                # ---- 单服务 JSON ----
                ent, err = _build_ent(spec_obj)
                if err:
                    return self._reply(200, {"ok": False, "error": err})
            else:
                # ---- 表单模式 ----
                stype = _M.norm_type(body.get("type") or "http")
                if not stype:
                    return self._reply(200, {"ok": False,
                                             "error": "类型仅支持 http（含 streamable_http）/ sse / stdio"})
                ent = {"type": stype}
                if stype == "stdio":
                    command = str(body.get("command") or "").strip()[:200]
                    if not command:
                        return self._reply(200, {"ok": False,
                                                 "error": "stdio 服务需要可执行文件路径"})
                    ent["command"] = command
                    ent["args"] = [p.strip('"') for p in _re.findall(
                        r'"[^"]*"|\S+', str(body.get("args") or "").strip()[:600])][:32]
                    env = _parse_lines_kv(body.get("env_lines"), r"([^=]+?)\s*=\s*(.*)")
                    if env:
                        ent["env"] = env
                else:
                    url = str(body.get("url") or "").strip()[:300]
                    if not (url.startswith("http://") or url.startswith("https://")):
                        return self._reply(200, {"ok": False,
                                                 "error": "url 需以 http(s):// 开头"})
                    ent["url"] = url
                    hdrs = _parse_lines_kv(body.get("headers_lines"),
                                           r"([^:]+?)\s*:\s*(.*)")
                    token = str(body.get("token") or "").strip()[:200]
                    if token:
                        hdrs["Authorization"] = "Bearer " + token
                    if hdrs:
                        ent["headers"] = hdrs
                if str(body.get("timeout") or "").strip():
                    ent["timeout"] = body.get("timeout")
                if body.get("protocol"):
                    ent["protocol"] = body.get("protocol")
                ent = _norm_extras(ent)
            cfg["custom"][name] = ent
            _M.save_mcp_cfg(cfg)
            return self._reply(200, {"ok": True})
        if u.path == "/api/mcp/del-custom":
            import qq_mcp as _M
            name = str(body.get("name") or "")[:48]
            cfg = _M.mcp_cfg()
            if name in (cfg.get("custom") or {}):
                cfg["custom"].pop(name, None)
                _M.reset_client(name)
                _M.save_mcp_cfg(cfg)
                return self._reply(200, {"ok": True})
            return self._reply(200, {"ok": False, "error": "只能删除自定义服务"})
        if u.path == "/api/shutdown":
            # 桌面应用退出时用：先回执再退出本进程（daemon/agent 是独立进程，不受影响）
            self._reply(200, {"ok": True, "bye": True})
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)),
                             daemon=True).start()
            return
        if u.path == "/api/pick-dir":
            _title = str(body.get("title") or "选择目录")[:40]
            def _pick():
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    d = filedialog.askdirectory(title=_title)
                    root.destroy()
                    return d or ""
                except Exception:
                    return ""
            import threading as _th
            box = {}
            t = _th.Thread(target=lambda: box.update(d=_pick()), daemon=True)
            t.start()
            t.join(timeout=120)
            return self._reply(200, {"ok": True, "dir": box.get("d", "")})
        if u.path == "/api/pick-zip":
            def _pickz():
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    f = filedialog.askopenfilename(
                        title="选择 NapCat Shell 压缩包",
                        filetypes=[("zip 压缩包", "*.zip")])
                    root.destroy()
                    return f or ""
                except Exception:
                    return ""
            import threading as _th
            boxz = {}
            tz = _th.Thread(target=lambda: boxz.update(f=_pickz()), daemon=True)
            tz.start()
            tz.join(timeout=120)
            return self._reply(200, {"ok": True, "file": boxz.get("f", "")})
        if u.path == "/api/update/run":
            import qq_update as _U
            cfg = C.load_app_config()
            if not bool(getattr(sys, "frozen", False)):
                return self._reply(200, {"ok": False, "error":
                    "源码模式不支持自动更新（请 git pull）；发布版才有此功能"})
            info = _U.check(cfg)
            if not info.get("ok"):
                return self._reply(200, {"ok": False, "error": info.get("error") or "检查失败"})
            if not info.get("newer"):
                return self._reply(200, {"ok": False, "error":
                    "已是最新版本 %s" % info.get("current")})
            threading.Thread(target=_U.run_update, args=(cfg,), daemon=True).start()
            return self._reply(200, {"ok": True, "latest": info.get("latest")})
        if u.path == "/api/napcat-open-login":
            cfg = C.load_app_config()
            if not S.port_open(6099):
                return self._reply(200, {"ok": False, "error":
                    "NapCat 还没启动（6099 端口未监听）：先点「启动机器人」，等它拉起 QQ + NapCat 后再试"})
            url = S.napcat_webui_url(cfg)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                return self._reply(200, {"ok": False, "error": "打开浏览器失败",
                                         "url": url})
            return self._reply(200, {"ok": True, "url": url,
                                     "tokenized": "?token=" in url})
        if u.path == "/api/napcat-install":
            zpath = str(body.get("zip") or "").strip()
            if not zpath or not os.path.isfile(zpath):
                return self._reply(400, {"ok": False, "error": "先选择 zip 文件"})
            ok, msg = S.install_napcat_zip(C.load_app_config(), zpath)
            return self._reply(200, {"ok": ok, "message": msg})
        if u.path == "/api/napcat-dl-start":
            import qq_napcat_dl as _ND
            return self._reply(200, _ND.start(
                C.load_app_config(), force=bool(body.get("force"))))
        if u.path == "/api/personas/pick-import":
            def _pickp():
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    f = filedialog.askopenfilename(
                        title="选择人设导出文件",
                        filetypes=[("人设文件", "*.json *.md *.txt"),
                                   ("所有文件", "*.*")])
                    root.destroy()
                    return f or ""
                except Exception:
                    return ""
            import threading as _th
            boxp = {}
            tp = _th.Thread(target=lambda: boxp.update(f=_pickp()), daemon=True)
            tp.start()
            tp.join(timeout=120)
            return self._reply(200, {"ok": True, "file": boxp.get("f", "")})
        if u.path == "/api/personas/import":
            import qq_persona_io as _PIO
            cnt, msg = _PIO.do_import(str(body.get("path") or ""))
            if not cnt:
                return self._reply(400, {"ok": False, "error": msg})
            return self._reply(200, {"ok": True, "count": cnt, "message": msg})
        if u.path == "/api/logs-export":
            # 导出诊断包：三源日志尾部 + 关键配置（脱敏）打包 zip；
            # 目录由前端先弹资源管理器目录选择框传入（未传则回落数据目录）
            import zipfile
            ddir = pathlib.Path(C.DATA_DIR)
            logs_dir = pathlib.Path(C.logs_dir())
            out_dir = pathlib.Path(str(body.get("dir") or "").strip() or str(ddir))
            if not out_dir.is_dir():
                return self._reply(400, {"ok": False,
                                         "error": "导出目录不存在：%s" % out_dir})
            out = out_dir / ("qqbot-diag-%s.zip" % time.strftime("%Y%m%d-%H%M%S"))
            out.resolve().relative_to(out_dir.resolve())  # 目标必须落在所选目录内
            def _mask(text):
                # 掩码所有疑似密钥/令牌的长 token，以及配置里明确是凭据的字段
                text = re.sub(r"[A-Za-z0-9]{24,}",
                              lambda m: m.group(0)[:6] + "…MASKED", text)
                return re.sub(
                    r'("(?:update_token|api_key|token|key|zhipu_api_key)"\s*:\s*")[^"]{6,}"',
                    r'\1MASKED"', text)
            with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
                for name in ("daemon.log", "agent.log", "ui.log"):
                    lp = logs_dir / name
                    if lp.is_file():
                        try:
                            tail = lp.read_text(encoding="utf-8",
                                                errors="replace").splitlines()[-800:]
                            z.writestr(name, _mask("\n".join(tail)))
                        except OSError:
                            pass
                cfgs = {"app_config.json": ddir / "app_config.json",
                        "reply_config.json": ddir / "reply_config.json",
                        "group_whitelist.json": ddir / "group_whitelist.json",
                        "model_chain.json": ddir / "model_chain.json"}
                for name, p in cfgs.items():
                    if p.is_file():
                        try:
                            z.writestr("config-" + name, _mask(p.read_text(
                                encoding="utf-8", errors="replace")))
                        except OSError:
                            pass
                z.writestr("说明.txt",
                           "诊断包由 ZCode QQ机器人 导出（日志各取末800行；配置中密钥已掩码）。\n"
                           "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
            try:  # 导出后直接打开资源管理器定位到文件（免得用户找不到）
                os.startfile(str(out_dir))  # noqa: Windows 仅 UI 进程使用
            except Exception:
                pass
            return self._reply(200, {"ok": True, "path": str(out)})
        if u.path == "/api/devctrl-code":
            # 刷新 = 让 agent 立刻作废旧码并换新（代理请求）
            r = proxy_get("127.0.0.1", _agent_port(), "/admin/devctrl-code?refresh=1")
            if not r.get("ok"):
                return self._reply(200, {"ok": False, "code": "", "expires_in": 0,
                                         "error": r.get("error", "agent 未运行")})
            return self._reply(200, r)
        if u.path == "/api/backup-now":
            import qq_bot_agent as _A
            _A._backup_data()
            return self._reply(200, {"ok": True})
        if u.path == "/api/qq-status":
            # 代理 daemon 的 QQ 在线状态设置（set_online_status NapCat 动作）
            kind = "away" if str(body.get("kind")) == "away" else "online"
            conn = http.client.HTTPConnection("127.0.0.1", _daemon_port(), timeout=12)
            try:
                payload = json.dumps({"kind": kind}).encode("utf-8")
                conn.request("POST", "/qq-status", body=payload, headers={
                    "Content-Type": "application/json",
                    "X-QQBot-Token": read_const(TOKEN_PATH)})
                r = json.loads(conn.getresponse().read().decode("utf-8"))
            finally:
                conn.close()
            return self._reply(200, r)
        if u.path == "/api/backup-restore":
            name = str(body.get("name") or "").strip()
            if not name or any(c in name for c in '\\/:*?"<>|') or name in (".", ".."):
                return self._reply(400, {"ok": False, "error": "备份名非法"})
            cfg = C.load_app_config()
            root = pathlib.Path(str(cfg.get("backup_dir") or "").strip()
                                or os.path.join(DATA_DIR, "backups"))
            src_dir = root / name
            if not src_dir.is_dir():
                return self._reply(400, {"ok": False, "error": "备份不存在: %s" % name})
            restored, missing = [], []
            for rel in C.backup_files_for(cfg):
                src = src_dir / os.path.basename(rel)
                dst = pathlib.Path(os.path.join(DATA_DIR, rel))
                if src.is_dir():
                    # 目录恢复（gsv_voices/、plugins_data/ 等）：整树覆盖回来
                    try:
                        if dst.is_dir():
                            shutil.rmtree(str(dst), ignore_errors=True)
                        shutil.copytree(str(src), str(dst))
                        restored.append(rel)
                    except OSError:
                        missing.append(rel)
                    continue
                if not src.is_file():
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    text = src.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    # 二进制/编码异常文件直接字节复制
                    try:
                        shutil.copyfile(str(src), str(dst))
                        restored.append(rel)
                    except OSError:
                        missing.append(rel)
                    continue
                tmp = dst.with_suffix(dst.suffix + ".restore.tmp")
                last = None
                for attempt in range(6):
                    try:
                        tmp.write_text(text, encoding="utf-8")
                        os.replace(str(tmp), str(dst))
                        last = None
                        break
                    except PermissionError as e:
                        last = e
                        time.sleep(0.08 * (attempt + 1))
                if last is not None:
                    try:
                        dst.write_text(text, encoding="utf-8")
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        missing.append(rel)
                        continue
                restored.append(rel)
            return self._reply(200, {"ok": True, "restored": restored,
                                     "failed": missing,
                                     "count": len(restored)})
        if u.path == "/api/config":
            cfg = load_json_const(MODEL_CHAIN_PATH, {})
            if "models" in body:
                models = body.get("models")
                def _entry_ok(x):
                    if isinstance(x, str):
                        return bool(x.strip())
                    return isinstance(x, dict) and bool(str(x.get("model", "")).strip())
                if not isinstance(models, list) or not models or \
                        not all(_entry_ok(m) for m in models[:8]):
                    return self._reply(400, {"ok": False, "error": "models 必须是非空数组(<=8)：模型名或{model,key}"})
                # 智谱模型名大小写敏感，统一转小写防止手滑（大写名会被平台报套餐失效）
                norm = []
                for m in models:
                    if isinstance(m, dict):
                        kid = str(m.get("key", "")).strip()[:40]
                        try:
                            ctx = max(4, min(128, int(m.get("ctx", 0))))
                        except (TypeError, ValueError):
                            ctx = 0
                        try:
                            mo = max(256, min(32768, int(m.get("max_out", 0))))
                        except (TypeError, ValueError):
                            mo = 0
                        norm.append({"model": str(m["model"]).strip().lower(),
                                     **({"key": kid} if kid else {}),
                                     **({"ctx": ctx} if ctx else {}),
                                     **({"max_out": mo} if mo else {})})
                    else:
                        norm.append(str(m).strip().lower())
                cfg["models"] = norm
            if "vision_model" in body:
                vm = str(body.get("vision_model", "")).strip()
                if len(vm) > 60:
                    return self._reply(400, {"ok": False, "error": "视觉模型名过长"})
                cfg["vision_model"] = vm
            if "thinking" in body:
                th = body.get("thinking")
                if not isinstance(th, dict) or len(th) > 20:
                    return self._reply(400, {"ok": False, "error": "thinking 结构非法"})
                clean = {}
                for k, v in th.items():
                    if k == "_auto":
                        clean["_auto"] = bool(v)
                    elif k == "_default" and isinstance(v, dict):
                        clean["_default"] = {"enabled": bool(v.get("enabled", True)),
                                             "level": str(v.get("level", "high"))[:10]}
                    elif isinstance(k, str) and len(k) <= 60 and isinstance(v, dict):
                        clean[k.lower()] = {"enabled": bool(v.get("enabled", True)),
                                            "level": str(v.get("level", "high"))[:10]}
                cfg["thinking"] = clean
            write_const(MODEL_CHAIN_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/groups":
            groups = body.get("groups")
            if not isinstance(groups, list) or len(groups) > 50 or \
                    not all(isinstance(g, str) and g.strip().isdigit() for g in groups):
                return self._reply(400, {"ok": False, "error": "groups 必须是群号字符串数组(<=50)"})
            cur = load_json_const(GROUPS_PATH, {})
            write_const(GROUPS_PATH, json.dumps(
                {"enabled": bool(body.get("enabled", cur.get("enabled", True))),
                 "allow_all": bool(body.get("allow_all", False)),
                 "groups": [g.strip() for g in groups]}, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/group-modes":
            gid = str(body.get("chat_id", "")).strip()
            if not gid.isdigit() or len(gid) > 12:
                return self._reply(400, {"ok": False, "error": "群号非法"})
            data = load_json_const(GROUP_MODES_PATH, {})
            if not isinstance(data, dict):
                data = {}
            ov = body.get("override")
            if ov is None:
                data.pop(gid, None)
                write_const(GROUP_MODES_PATH, json.dumps(
                    data, ensure_ascii=False, indent=2))
                return self._reply(200, {"ok": True, "cleared": True})
            if not isinstance(ov, dict):
                return self._reply(400, {"ok": False, "error": "override 必须是对象或 null"})
            # 字段级 merge：前端可只传改动的字段，未传字段保留该群已保存的覆盖
            clean = dict(data.get(gid, {})) if isinstance(data.get(gid), dict) else {}
            if "reply_mode" in ov:
                if ov["reply_mode"] not in ("standard", "keyword", "chat"):
                    return self._reply(400, {"ok": False, "error": "reply_mode 非法"})
                clean["reply_mode"] = ov["reply_mode"]
            if "keywords" in ov:
                kw = ov["keywords"]
                if not isinstance(kw, list) or len(kw) > 50 or \
                        not all(isinstance(k, str) and k.strip() for k in kw):
                    return self._reply(400, {"ok": False, "error": "keywords 非法"})
                clean["keywords"] = [k.strip() for k in kw][:50]
            if "chat_probability" in ov:
                try:
                    clean["chat_probability"] = min(1.0, max(
                        0.0, float(ov["chat_probability"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "chat_probability 非法"})
            if "chat_min_interval_sec" in ov:
                try:
                    clean["chat_min_interval_sec"] = max(0, min(
                        3600, int(ov["chat_min_interval_sec"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "interval 非法"})
            if "paused" in ov:
                clean["paused"] = bool(ov["paused"])
            if "proactive" in ov:
                # None=清除覆盖（跟随全局）；True/False=该群强制开/关主动发消息
                if ov["proactive"] is None:
                    clean.pop("proactive", None)
                    clean.pop("proactive_probability", None)
                    clean.pop("proactive_min_interval_sec", None)
                else:
                    clean["proactive"] = bool(ov["proactive"])
            if "proactive_probability" in ov:
                try:
                    clean["proactive_probability"] = min(1.0, max(0.0, float(ov["proactive_probability"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "概率非法"})
            if "proactive_min_interval_sec" in ov:
                try:
                    clean["proactive_min_interval_sec"] = max(300, min(86400, int(ov["proactive_min_interval_sec"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "间隔非法"})
            # 模式与暂停互斥（按本次请求判定）：请求设了模式即解除暂停，请求暂停即丢弃模式
            if "reply_mode" in ov:
                clean.pop("paused", None)
            if ov.get("paused"):
                clean.pop("reply_mode", None)
                clean.pop("proactive", None)
            if not clean:
                data.pop(gid, None)
            else:
                data[gid] = clean
            write_const(GROUP_MODES_PATH, json.dumps(
                data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True, "saved": clean})
        if u.path == "/api/private-config":
            # 合并式保存（曾整文件覆盖——typing 键被白名单保存抹掉的坑）
            cur = load_json_const(PRIVATE_CONFIG_PATH, {})
            wl = body.get("qq_whitelist")
            if "qq_whitelist" in body:
                if not isinstance(wl, list) or len(wl) > 100 or                     not all(isinstance(q, str) and q.strip().isdigit() for q in wl):
                    return self._reply(400, {"ok": False, "error": "qq_whitelist 必须是QQ号字符串数组(<=100)"})
                cur["qq_whitelist"] = [q.strip() for q in wl]
            if "enabled" in body:
                cur["enabled"] = bool(body.get("enabled"))
            if "allow_all" in body:
                cur["allow_all"] = bool(body.get("allow_all"))
            if "typing_enabled" in body:
                cur["typing_enabled"] = bool(body.get("typing_enabled"))
            if "typing_seconds" in body:
                try:
                    cur["typing_seconds"] = max(1, min(10, int(body.get("typing_seconds") or 2)))
                except (TypeError, ValueError):
                    cur["typing_seconds"] = 2
            write_const(PRIVATE_CONFIG_PATH, json.dumps(cur, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/reply-config":
            data = load_json_const(REPLY_CONFIG_PATH, {})
            if "reply_mode" in body:
                if body["reply_mode"] not in ("standard", "keyword", "chat"):
                    return self._reply(400, {"ok": False, "error": "reply_mode 非法"})
                data["reply_mode"] = body["reply_mode"]
            if "keywords" in body:
                kw = body["keywords"]
                if not isinstance(kw, list) or len(kw) > 50 or \
                        not all(isinstance(k, str) and k.strip() and len(k) <= 30 for k in kw):
                    return self._reply(400, {"ok": False, "error": "keywords 必须是非空字符串数组(<=50)"})
                data["keywords"] = [k.strip() for k in kw]
            if "chat_probability" in body:
                try:
                    data["chat_probability"] = min(1.0, max(0.0, float(body["chat_probability"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "chat_probability 必须是数字"})
            if "chat_interesting" in body:
                data["chat_interesting"] = bool(body["chat_interesting"])
            if "vision_enabled" in body:
                data["vision_enabled"] = bool(body["vision_enabled"])
            if "vision_mode" in body:
                data["vision_mode"] = str(body["vision_mode"] or "all")[:8]
            if "vision_prob" in body:
                try:
                    data["vision_prob"] = min(1.0, max(0.0, float(body["vision_prob"])))
                except (TypeError, ValueError):
                    pass
            if "web_search_enabled" in body:
                data["web_search_enabled"] = bool(body["web_search_enabled"])
            if "abuse_guard_enabled" in body:
                data["abuse_guard_enabled"] = bool(body["abuse_guard_enabled"])
            for _feat in ("task_bridge_enabled", "remote_control_enabled",
                          "draw_enabled", "daily_review_enabled"):
                if _feat in body:
                    data[_feat] = bool(body[_feat])
            if "chat_anti_interrupt" in body:
                data["chat_anti_interrupt"] = bool(body["chat_anti_interrupt"])
            if "chat_min_interval_sec" in body:
                try:
                    data["chat_min_interval_sec"] = max(0, min(3600, int(body["chat_min_interval_sec"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "chat_min_interval_sec 必须是整数"})
            if "econ_enabled" in body:
                data["econ_enabled"] = bool(body["econ_enabled"])
                if not data["econ_enabled"]:
                    data["econ_manual"] = False
            if "econ_manual" in body:
                data["econ_manual"] = bool(body["econ_manual"])
            for _ek in ("econ_start", "econ_end"):
                if _ek in body:
                    v = str(body[_ek]).strip()
                    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
                        return self._reply(400, {"ok": False, "error": "%s 应为 HH:MM" % _ek})
                    data[_ek] = v
            if "econ_days" in body:
                days = body["econ_days"]
                if not isinstance(days, list) or len(days) > 7 or \
                        not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
                    return self._reply(400, {"ok": False, "error": "econ_days 应为 0-6 的星期数组"})
                data["econ_days"] = sorted(set(days))
            # 新模型落库时清掉旧三态/仅工作日字段
            for _old in ("econ_workdays_only", "econ_mode_override"):
                data.pop(_old, None)
            if "token_limit_enabled" in body:
                data["token_limit_enabled"] = bool(body["token_limit_enabled"])
            if "token_limit_hours" in body:
                try:
                    data["token_limit_hours"] = max(1, min(24, int(body["token_limit_hours"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "窗口应为 1-24 小时"})
            if "token_limit_max" in body:
                try:
                    data["token_limit_max"] = max(1000, min(2000000000, int(body["token_limit_max"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "上限应为 tokens 整数（≥1000）"})
            if "proactive_groups_enabled" in body:
                data["proactive_groups_enabled"] = bool(body["proactive_groups_enabled"])
            if "lean_ctx_enabled" in body:
                data["lean_ctx_enabled"] = bool(body["lean_ctx_enabled"])
            if "dyn_ctx_enabled" in body:
                data["dyn_ctx_enabled"] = bool(body["dyn_ctx_enabled"])
            if "wait_gate_enabled" in body:
                data["wait_gate_enabled"] = bool(body["wait_gate_enabled"])
            if "burst_merge_enabled" in body:
                data["burst_merge_enabled"] = bool(body["burst_merge_enabled"])
            if "botloop_guard_enabled" in body:
                data["botloop_guard_enabled"] = bool(body["botloop_guard_enabled"])
            if "botloop_cooldown_min" in body:
                try:
                    data["botloop_cooldown_min"] = max(10, min(1440, int(body["botloop_cooldown_min"])))
                except (TypeError, ValueError):
                    pass
            if "proactive_private_enabled" in body:
                data["proactive_private_enabled"] = bool(body["proactive_private_enabled"])
            for _nk in ("nudge_delay_min_sec", "nudge_delay_max_sec"):
                if _nk in body:
                    try:
                        data[_nk] = max(30, min(1800, int(body[_nk])))
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": _nk + " 应为 30-1800 秒"})
            if "nudge_max_strikes" in body:
                try:
                    data["nudge_max_strikes"] = max(1, min(5, int(body["nudge_max_strikes"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "追问次数应为 1-5"})
            if "proactive_hourly_cap" in body:
                try:
                    data["proactive_hourly_cap"] = max(0, min(60, int(body["proactive_hourly_cap"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "总量上限应为 0-60 整数（0=不限）"})
            for _pk in ("env_pulse_enabled", "member_profiles_enabled",
                        "bili_scroll_enabled", "bili_share_enabled",
                        "nudge_enabled", "nudge_group_enabled"):
                if _pk in body:
                    data[_pk] = bool(body[_pk])
            if "bili_scroll_window_min" in body:
                try:
                    data["bili_scroll_window_min"] = max(30, min(1440, int(body["bili_scroll_window_min"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "周期应为 30-1440 分钟"})
            if "bili_scroll_keywords" in body:
                data["bili_scroll_keywords"] = str(body["bili_scroll_keywords"])[:120]
            for _pk in ("proactive_groups_probability", "proactive_private_probability"):
                if _pk in body:
                    try:
                        data[_pk] = min(1.0, max(0.0, float(body[_pk])))
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": "概率应为 0-1 数字"})
            for _pk in ("proactive_groups_min_interval_sec", "proactive_private_min_interval_sec"):
                if _pk in body:
                    try:
                        data[_pk] = max(300, min(86400, int(body[_pk])))
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": "间隔应为 300-86400 秒"})
            for _pk in ("proactive_groups_silent_start", "proactive_groups_silent_end",
                        "proactive_private_silent_start", "proactive_private_silent_end"):
                if _pk in body:
                    v = str(body[_pk]).strip()
                    if v and not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", v):
                        return self._reply(400, {"ok": False, "error": "%s 应为 HH:MM 或空" % _pk})
                    data[_pk] = v
            for _old in ("proactive_probability", "proactive_min_interval_sec"):
                data.pop(_old, None)
            write_const(REPLY_CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/personas/save":
            name = str(body.get("name", "")).strip()
            text = str(body.get("text", ""))[:20000]
            if not name or name.startswith("_") or len(name) > 40 or "\n" in name:
                return self._reply(400, {"ok": False, "error": "人设名非法（禁下划线开头，<=40字，不含换行）"})
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            data[name] = text
            write_const(PERSONA_COLLECTION_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/persona-routes/save":
            routes = body.get("routes")
            if not isinstance(routes, list) or len(routes) > 100:
                return self._reply(400, {"ok": False, "error": "routes 应为 <=100 条的数组"})
            coll = load_json_const(PERSONA_COLLECTION_PATH, {})
            cleaned = []
            seen = set()
            for r in routes:
                if not isinstance(r, dict):
                    continue
                key = str(r.get("key", "")).strip()
                persona = str(r.get("persona", "")).strip()
                if key not in ("group:", "private:"):
                    m = re.match(r"^(group|private):(\d{5,12})$", key)
                    if not m:
                        return self._reply(400, {"ok": False,
                                                 "error": "路由 key 非法: %s" % key[:40]})
                if key.startswith("_") or persona.startswith("_"):
                    return self._reply(400, {"ok": False, "error": "键/人设名非法"})
                if persona not in coll:
                    return self._reply(400, {"ok": False,
                                             "error": "人设不存在: %s" % persona[:40]})
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"key": key, "persona": persona})
            try:
                write_const(PERSONA_ROUTES_PATH, json.dumps(
                    {"routes": cleaned}, ensure_ascii=False, indent=2))
                return self._reply(200, {"ok": True, "count": len(cleaned)})
            except OSError as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/personas/rename":
            import re as _re
            _bad_seg = _re.compile(r'[/:*?"<>|\x00]')
            name = str(body.get("name", "")).strip()
            new_name = str(body.get("new_name", "")).strip()
            bad = _bad_seg.search(new_name) if new_name else True
            if (not name or not new_name or bad or new_name.startswith("_")
                    or len(new_name) > 40 or "\n" in new_name):
                return self._reply(400, {"ok": False,
                                         "error": "新人设名非法（禁下划线开头/特殊字符，<=40字）"})
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            if name not in data or name.startswith("_"):
                return self._reply(400, {"ok": False, "error": "人设不存在"})
            if new_name in data:
                return self._reply(400, {"ok": False, "error": "已存在同名人设"})
            new_coll = {}
            for k, v in data.items():
                if k == name:
                    new_coll[new_name] = v
                elif k == "_active":
                    new_coll[k] = new_name if v == name else v
                else:
                    new_coll[k] = v
            write_const(PERSONA_COLLECTION_PATH,
                        json.dumps(new_coll, ensure_ascii=False, indent=2))
            moved = []
            ev = load_json_const(EVOLVED_PATH, {})
            if isinstance(ev, dict) and name in ev:
                new_ev = {}
                for k, v in ev.items():
                    new_ev[new_name if k == name else k] = v
                write_const(EVOLVED_PATH, json.dumps(new_ev, ensure_ascii=False, indent=2))
                moved.append("自进化风格")
            st = load_json_const(STICKERS_PATH, {})
            if isinstance(st, dict) and isinstance(st.get(name), dict):
                new_st = {}
                for k, v in st.items():
                    new_st[new_name if k == name else k] = v
                entry = new_st[new_name]
                for it in entry.get("items", []):
                    f = str(it.get("file", ""))
                    if f.startswith(name + "/"):
                        it["file"] = new_name + f[len(name):]
                old_dir = STICKERS_ROOT / name
                if old_dir.is_dir():
                    try:
                        os.rename(str(old_dir), str(STICKERS_ROOT / new_name))
                    except OSError:
                        pass
                write_const(STICKERS_PATH, json.dumps(new_st, ensure_ascii=False, indent=2))
                moved.append("专属表情库")
            # 分对象人设路由同步改名
            rd = load_json_const(PERSONA_ROUTES_PATH, {})
            rr = rd.get("routes") if isinstance(rd, dict) else []
            hit = False
            for r in (rr or []):
                if isinstance(r, dict) and r.get("persona") == name:
                    r["persona"] = new_name
                    hit = True
            if hit:
                write_const(PERSONA_ROUTES_PATH, json.dumps(
                    {"routes": rr}, ensure_ascii=False, indent=2))
                moved.append("分对象人设路由")
            return self._reply(200, {"ok": True, "renamed": new_name,
                                     "moved": moved})
        if u.path == "/api/personas/switch":
            name = str(body.get("name", "")).strip()
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            if not name or name not in data:
                return self._reply(400, {"ok": False, "error": "人设不存在"})
            data["_active"] = name
            write_const(PERSONA_COLLECTION_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True, "active": name})
        if u.path == "/api/personas/delete":
            name = str(body.get("name", "")).strip()
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            if name.startswith("_"):
                return self._reply(400, {"ok": False, "error": "不能删除保留项"})
            if name not in data:
                return self._reply(400, {"ok": False, "error": "人设不存在"})
            if str(data.get("_active", "")) == name:
                return self._reply(400, {"ok": False, "error": "该人设正在使用，请先切换到其他设"})
            del data[name]
            write_const(PERSONA_COLLECTION_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            # 分对象人设清理：删掉指向该人设的路由，防悬空（这些对象自动回落全局激活人设）
            try:
                rd = load_json_const(PERSONA_ROUTES_PATH, {})
                routes = rd.get("routes") if isinstance(rd, dict) else []
                kept = [r for r in (routes or [])
                        if not (isinstance(r, dict) and r.get("persona") == name)]
                if len(kept) != len(routes or []):
                    write_const(PERSONA_ROUTES_PATH, json.dumps(
                        {"routes": kept}, ensure_ascii=False, indent=2))
            except OSError:
                pass
            return self._reply(200, {"ok": True})
        if u.path == "/api/app-config":
            cfg_in = body
            cleaned = {}
            if "bot_qq" in cfg_in:
                v = str(cfg_in["bot_qq"]).strip()
                if v and not v.isdigit():
                    return self._reply(400, {"ok": False, "error": "机器人 QQ 必须是数字"})
                cleaned["bot_qq"] = v
            for k in ("bot_name_enabled", "owner_name_enabled"):
                if k in cfg_in:
                    cleaned[k] = bool(cfg_in[k])
            for k in ("bot_name_value", "owner_name_value"):
                if k in cfg_in:
                    cleaned[k] = str(cfg_in[k]).strip()[:12]
            if "owner_qq" in cfg_in:
                ov = str(cfg_in["owner_qq"]).strip()
                if ov and not ov.isdigit():
                    return self._reply(400, {"ok": False, "error": "主人 QQ 必须是数字"})
                cleaned["owner_qq"] = ov
            if "qqnt_dir" in cfg_in:
                cleaned["qqnt_dir"] = str(cfg_in["qqnt_dir"]).strip()
            for k in ("napcat_ws_port", "daemon_port", "agent_admin_port", "ui_port"):
                if k in cfg_in:
                    try:
                        port = int(cfg_in[k])
                        if not (1024 <= port <= 65535):
                            raise ValueError
                        cleaned[k] = port
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": "%s 必须是 1024-65535" % k})
            if "device_ctrl_enabled" in cfg_in:
                cleaned["device_ctrl_enabled"] = bool(cfg_in["device_ctrl_enabled"])
            if "device_ctrl_2fa" in cfg_in:
                cleaned["device_ctrl_2fa"] = bool(cfg_in["device_ctrl_2fa"])
            if "device_ctrl_protected_dirs" in cfg_in:
                dirs = cfg_in["device_ctrl_protected_dirs"]
                if not isinstance(dirs, list) or len(dirs) > 20:
                    return self._reply(400, {"ok": False, "error": "保护目录列表非法"})
                cleaned["device_ctrl_protected_dirs"] = [
                    str(x).strip()[:260] for x in dirs if str(x).strip()]
            if "timezone" in cfg_in:
                cleaned["timezone"] = str(cfg_in["timezone"]).strip()[:50]
            if "owner_bilibili_uid" in cfg_in:
                uid = str(cfg_in["owner_bilibili_uid"]).strip()
                cleaned["owner_bilibili_uid"] = uid if uid.isdigit() and len(uid) <= 12 else ""
            for k in ("review_time", "backup_time"):
                if k in cfg_in:
                    v = str(cfg_in[k]).strip()
                    cleaned[k] = v if len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() else ""
            for k in ("backup_enabled", "sched_pause_enabled", "watchdog_enabled",
                      "qq_version_lock", "qq_status_auto", "zcode_bridge_enabled",
                      "zcode_handoff_enabled", "zcode_return_enabled",
                      "zcode_notify_enabled", "zcode_reports_enabled",
                      "zcode_sticker_enabled", "zcode_watch_enabled",
                      "zcode_cron_enabled", "zcode_context_enabled",
                      "device_ctrl_actions"):
                if k in cfg_in:
                    cleaned[k] = bool(cfg_in[k])
            if "backup_dir" in cfg_in:
                cleaned["backup_dir"] = str(cfg_in["backup_dir"]).strip()[:260]
            if "logs_dir" in cfg_in:
                cleaned["logs_dir"] = str(cfg_in["logs_dir"]).strip()[:260]
            if "logs_autoclean_enabled" in cfg_in:
                cleaned["logs_autoclean_enabled"] = bool(cfg_in["logs_autoclean_enabled"])
            if "logs_keep_days" in cfg_in:
                try:
                    cleaned["logs_keep_days"] = max(1, min(365, int(cfg_in["logs_keep_days"])))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "日志保留天数应为 1-365 整数"})
            if "api_proxy" in cfg_in:
                px = str(cfg_in["api_proxy"]).strip()[:120]
                if px:
                    if "//" not in px:
                        px = "http://" + px
                    up = urllib.parse.urlsplit(px)
                    if up.scheme not in ("http", "https") or not up.hostname:
                        return self._reply(400, {"ok": False, "error": "代理格式应为 host:port 或 http://host:port"})
                cleaned["api_proxy"] = px
            for k in ("backup_keep_days", "backup_keep_copies"):
                if k in cfg_in:
                    try:
                        cleaned[k] = max(1, min(365, int(cfg_in[k])))
                    except (TypeError, ValueError):
                        pass
            if "backup_excluded" in cfg_in:
                valid = {g["group"] for g in C.BACKUP_GROUPS}
                cleaned["backup_excluded"] = [str(x) for x in
                                              (cfg_in["backup_excluded"] or [])
                                              if str(x) in valid][:len(valid)]
            if "update_check" in cfg_in:
                cleaned["update_check"] = bool(cfg_in["update_check"])
            if "update_repo" in cfg_in:
                rp = str(cfg_in["update_repo"]).strip()[:120]
                if rp and (rp.count("/") != 1 or any(c in rp for c in " \t\\?#")):
                    return self._reply(400, {"ok": False, "error": "更新源格式应为 owner/name"})
                cleaned["update_repo"] = rp or C.DEFAULT_UPDATE_REPO
            if "update_token" in cfg_in:
                tv = str(cfg_in["update_token"]).strip()
                if len(tv) > 300 or any(c.isspace() for c in tv):
                    return self._reply(400, {"ok": False, "error": "GitHub Token 格式异常"})
                cleaned["update_token"] = tv
            if "update_skip_version" in cfg_in:
                cleaned["update_skip_version"] = str(
                    cfg_in["update_skip_version"]).strip()[:20]
            if "update_mirror" in cfg_in:
                mv = str(cfg_in["update_mirror"]).strip()[:160] or "auto"
                if mv not in ("auto", "off"):
                    if not mv.startswith("https://"):
                        return self._reply(400, {"ok": False,
                                                 "error": "加速镜像必须是 https:// 开头"})
                    if not mv.endswith("/"):
                        mv += "/"
                cleaned["update_mirror"] = mv
            if "user_blacklist" in cfg_in:
                bl = cfg_in["user_blacklist"]
                if not isinstance(bl, list) or len(bl) > 200:
                    return self._reply(400, {"ok": False, "error": "黑名单列表非法（最多 200 个）"})
                out_bl, seen = [], set()
                for x in bl:
                    v = str(x).strip()
                    if not v:
                        continue
                    if not v.isdigit() or not (5 <= len(v) <= 12):
                        return self._reply(400, {"ok": False, "error": "黑名单里的 %r 不是合法 QQ 号" % v[:14]})
                    if v not in seen:
                        seen.add(v)
                        out_bl.append(v)
                cleaned["user_blacklist"] = out_bl
            if "weather_city" in cfg_in:
                wc = re.sub(r"[\r\n\t]", "", str(cfg_in["weather_city"]).strip())[:30]
                cleaned["weather_city"] = wc
            for k in ("sched_pause_start", "sched_pause_end"):
                if k in cfg_in:
                    v = str(cfg_in[k]).strip()
                    cleaned[k] = v if len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() else ""
            if "watchdog_mode" in cfg_in:
                cleaned["watchdog_mode"] = "alert" if str(cfg_in["watchdog_mode"]) == "alert" else "auto"
            if "watchdog_interval" in cfg_in:
                try:
                    cleaned["watchdog_interval"] = max(60, min(600, int(cfg_in["watchdog_interval"])))
                except (TypeError, ValueError):
                    pass
            if "auto_power_on" in cfg_in:
                cleaned["auto_power_on"] = bool(cfg_in["auto_power_on"])
            if "close_action" in cfg_in:
                if cfg_in["close_action"] not in ("tray", "exit_app", "exit_all"):
                    return self._reply(400, {"ok": False, "error": "close_action 非法"})
                cleaned["close_action"] = cfg_in["close_action"]
            merged = C.save_app_config(cleaned)
            return self._reply(200, {"ok": True, **merged})
        if u.path == "/api/apikey":
            key = str(body.get("key", "")).strip()
            if not key or len(key) > 200 or any(c.isspace() for c in key):
                return self._reply(400, {"ok": False, "error": "key 格式异常"})
            (pathlib.Path(API_KEY_PATH)).write_text(key, encoding="utf-8")
            return self._reply(200, {"ok": True})
        if u.path == "/api/autostart":
            enable = bool(body.get("enable"))
            ok, msg = S.set_autostart(enable)
            return self._reply(200 if ok else 400, {"ok": ok, "message": msg,
                                                    "enabled": enable})
        if u.path == "/api/switch-account":
            new_qq = str(body.get("qq") or "").strip()
            if not new_qq.isdigit() or not (5 <= len(new_qq) <= 12):
                return self._reply(400, {"ok": False, "error": "QQ 号格式不对"})
            with _boot_lock:
                if _boot_state["running"]:
                    return self._reply(200, {"ok": False, "started": False,
                                             "error": "有启动/切换任务正在进行，稍后再试"})
            cfg = C.load_app_config()
            old_qq = str(cfg.get("bot_qq") or "").strip()
            if new_qq == old_qq:
                return self._reply(200, {"ok": False,
                                         "error": "和当前登录的机器人 QQ 相同，无需切换"})
            C.save_app_config({"bot_qq": new_qq})
            with _boot_lock:
                _boot_state.update({"running": True, "done": False, "failed": False,
                                    "started_at": time.time(), "steps": [],
                                    "final": ""})

            def _run_switch():
                try:
                    def on_note(step):
                        with _boot_lock:
                            _boot_state["steps"].append(
                                {"ts": time.time(), **{k: step.get(k) for k in
                                 ("component", "ok", "message", "progress")}})
                    on_note({"component": "switch", "ok": None,
                             "message": "已保存新机器人 QQ %s，正在停止旧账号 %s…"
                                        % (new_qq, old_qq or "?")})
                    for note in S.power_off(cfg, include_napcat=True):
                        on_note({"component": "napcat", "ok": None,
                                 "message": "停止：%s" % note.get("message", "")})
                    time.sleep(2)
                    on_note({"component": "switch", "ok": None,
                             "message": "正在用新账号 %s 启动（本机登录过则快速登录，否则弹二维码扫码）…" % new_qq})
                    S.power_on(C.load_app_config(), on_note=on_note)
                    with _boot_lock:
                        _boot_state["done"] = True
                        hard_fail = [s for s in _boot_state["steps"]
                                     if s.get("ok") is False and not s.get("progress")]
                        _boot_state["failed"] = bool(hard_fail)
                        _boot_state["final"] = ("账号切换完成（存在失败项，看下方日志）"
                                                if hard_fail else
                                                "账号切换完成：现在登录的是 %s" % new_qq)
                    on_note({"component": "switch", "ok": True,
                             "message": "新账号登录后 NapCat 会更换登录密钥——"
                                        "本页「NapCat 登录密钥」卡片自动读取实时值，"
                                        "无需手动处理"})
                except Exception as e:
                    with _boot_lock:
                        _boot_state["done"] = True
                        _boot_state["failed"] = True
                        _boot_state["final"] = "切换异常：%r" % e
                finally:
                    with _boot_lock:
                        _boot_state["running"] = False

            threading.Thread(target=_run_switch, daemon=True).start()
            return self._reply(200, {"ok": True, "started": True, "qq": new_qq})
        if u.path == "/api/uninstall":
            mode = str(body.get("mode") or "")
            if mode not in ("keep-data", "wipe"):
                return self._reply(400, {"ok": False, "error": "mode 非法"})
            if not getattr(sys, "frozen", False):
                return self._reply(200, {"ok": False, "error":
                    "源码模式没有可卸载的安装目录：删除仓库目录与 ~/.zcode-qq-bot 即可"})
            # 先摘掉开机自启（注册表操作在服务端做，比 bat 里删更稳）
            try:
                S.set_autostart(False)
            except Exception:
                pass
            import tempfile
            app_dir = os.path.dirname(os.path.abspath(sys.executable))
            bat = pathlib.Path(tempfile.gettempdir()) / "uninstall-qqbot.bat"
            lines = [
                "@echo off",
                "chcp 65001 >nul",
                "ping -n 4 127.0.0.1 >nul",
                "taskkill /IM ZCodeQQBot.exe /F >nul 2>&1",
                "ping -n 3 127.0.0.1 >nul",
                'rmdir /s /q "%s" >nul 2>&1' % app_dir,
            ]
            if mode == "wipe":
                lines.append('rmdir /s /q "%s" >nul 2>&1' % C.DATA_DIR)
            lines.append('del /q "%~f0"')
            bat.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
            subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(bat.parent),
                             creationflags=0x00000008 | 0x00000200,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
            return self._reply(200, {"ok": True, "mode": mode,
                                     "app_dir": app_dir,
                                     "data_dir": C.DATA_DIR if mode == "wipe" else ""})
        if u.path == "/api/napcat-runtime/install":
            import qq_napcat_runtime as _RT
            return self._reply(200, _RT.install())
        if u.path == "/api/napcat-runtime/delete":
            # 危险确认在浏览器端完成（与其他 danger 操作一致）
            import qq_napcat_runtime as _RT
            r = _RT.delete()
            if r.get("ok"):
                data = load_json_const(C.APP_CONFIG_PATH, {})
                data["napcat_mode"] = "qqnt"
                write_const(C.APP_CONFIG_PATH, json.dumps(
                    data, ensure_ascii=False, indent=2))
            return self._reply(200 if r.get("ok") else 400, r)
        if u.path == "/api/napcat-mode":
            mode = str(body.get("mode") or "").strip()
            if mode not in ("qqnt", "runtime"):
                return self._reply(400, {"ok": False, "error": "mode 非法"})
            if mode == "runtime":
                import qq_napcat_runtime as _RT
                if not _RT.is_installed():
                    return self._reply(200, {"ok": False, "need_install": True})
            data = load_json_const(C.APP_CONFIG_PATH, {})
            data["napcat_mode"] = mode
            write_const(C.APP_CONFIG_PATH, json.dumps(
                data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True, "mode": mode})
        if u.path == "/api/elevate-qq-lock":
            # 提权代理：写临时执行器 → Start-Process -Verb RunAs（用户 UAC 确认）
            # → 以管理员重跑本端点完成 hosts 修改
            enable = bool(body.get("enable"))
            import tempfile as _tf
            sf = pathlib.Path(_tf.gettempdir()) / "zcode_qqlock_elev.py"
            sf.write_text(
                "# -*- coding: utf-8 -*-\n"
                "import http.client, json, pathlib\n"
                "uit = pathlib.Path.home().joinpath('.zcode-qq-bot', 'ui_token')"
                ".read_text(encoding='utf-8').strip()\n"
                "port = %d\n"
                "c = http.client.HTTPConnection('127.0.0.1', port, timeout=15)\n"
                "c.request('POST', '/api/qq-version-lock', json.dumps("
                "{'enable': %s}).encode(), {'Content-Type': 'application/json',"
                " 'X-UI-Token': uit})\n"
                "c.getresponse().read(); c.close()\n"
                % (int(C.load_app_config().get("ui_port", DEFAULT_UI_PORT)),
                   "true" if enable else "false"),
                encoding="utf-8")
            try:
                import subprocess as _sp
                _sp.run(["powershell", "-NoProfile", "-Command",
                         "Start-Process -FilePath '%s' -Verb RunAs -WindowStyle Hidden"
                         % str(sys.executable).replace("'", "''")],
                        capture_output=True, timeout=10)
                return self._reply(200, {"ok": True})
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)[:120]})
        if u.path == "/api/qq-version-lock":
            enable = bool(body.get("enable"))
            # 同步意图到配置（预检 warn 判定读它）
            data = load_json_const(C.APP_CONFIG_PATH, {})
            data["qq_version_lock"] = enable
            write_const(C.APP_CONFIG_PATH, json.dumps(
                data, ensure_ascii=False, indent=2))
            ok, msg = S.set_qq_update_block(enable)
            if msg == "NEED_ADMIN":
                # 未提权：弹确认后由用户点 UAC，脚本自身以管理员重跑本端点
                return self._reply(200, {"ok": False, "need_admin": True,
                                         "target_state": enable})
            return self._reply(200, {"ok": ok, "message": msg,
                                     "lock_active": S.qq_update_block_active()})
        if u.path == "/api/power/on":
            with _boot_lock:
                if _boot_state["running"]:
                    return self._reply(200, {"ok": True, "started": False,
                                             "note": "启动任务已在进行中，见状态页启动日志"})
            # 防重复启动：NapCat/daemon/agent 全部在线时不再执行任何启动动作
            cfg = C.load_app_config()
            if (S.port_open(cfg["napcat_ws_port"]) and S.port_open(cfg["daemon_port"])
                    and S.port_open(cfg["agent_admin_port"])):
                return self._reply(200, {"ok": True, "started": False,
                                         "note": "机器人已在运行，无需重复启动"})
            with _boot_lock:
                _boot_state.update({"running": True, "done": False, "failed": False,
                                    "started_at": time.time(), "steps": [],
                                    "final": ""})
                state = dict(_boot_state)

            def _run():
                try:
                    def on_note(step):
                        with _boot_lock:
                            _boot_state["steps"].append(
                                {"ts": time.time(), **{k: step.get(k) for k in
                                 ("component", "ok", "message", "progress")}})
                    S.power_on(C.load_app_config(), on_note=on_note)
                    with _boot_lock:
                        _boot_state["done"] = True
                        hard_fail = [s for s in _boot_state["steps"]
                                     if s.get("ok") is False and not s.get("progress")]
                        _boot_state["failed"] = bool(hard_fail)
                        if hard_fail:
                            # 结论直接携带失败原因（取前两条，去重），不再只说「存在失败项」
                            seen, msgs = set(), []
                            for s in hard_fail:
                                m = str(s.get("message") or "").strip()
                                if m and m not in seen:
                                    seen.add(m)
                                    msgs.append(m)
                            summary = "；".join(m[:90] for m in msgs[:2])
                            if len(msgs) > 2:
                                summary += "（共 %d 项，详见下方日志）" % len(msgs)
                            _boot_state["final"] = "启动失败：" + summary
                        else:
                            _boot_state["final"] = "启动完成"
                        _boot_state_persist()
                except Exception as e:
                    with _boot_lock:
                        _boot_state["done"] = True
                        _boot_state["failed"] = True
                        _boot_state["final"] = "启动异常：%r" % e
                        _boot_state_persist()
                finally:
                    with _boot_lock:
                        _boot_state["running"] = False

            threading.Thread(target=_run, daemon=True).start()
            return self._reply(200, {"ok": True, "started": True})
        if u.path == "/api/power/off":
            cfg = C.load_app_config()
            notes = S.power_off(cfg, include_napcat=bool(body.get("include_napcat")))
            with _boot_lock:
                _boot_state["steps"].append({
                    "ts": time.time(), "component": "power", "ok": True,
                    "message": "手动停止：" + "；".join(
                        "%s %s" % (n.get("component", "?"), n.get("message", ""))
                        for n in notes)[:160]})
                _boot_state.update({"running": False, "done": True, "failed": False,
                                    "final": "已手动停止"})
            return self._reply(200, {"ok": True, "notes": notes})
        if u.path == "/api/stickers/upload":
            import base64
            import uuid as _uuid
            pool = str(body.get("pool", "_global")).strip()
            if pool != "_global" and (pool.startswith("_") or not pool or len(pool) > 40):
                return self._reply(400, {"ok": False, "error": "库名非法"})
            filename = str(body.get("filename", "img.jpg"))
            ext = os.path.splitext(filename)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                return self._reply(400, {"ok": False, "error": "仅支持图片格式"})
            try:
                raw = base64.b64decode(str(body.get("data_b64", "")), validate=False)
            except (ValueError, TypeError):
                return self._reply(400, {"ok": False, "error": "数据解码失败"})
            if not raw or len(raw) > 8 * 1024 * 1024:
                return self._reply(400, {"ok": False, "error": "图片为空或超过 8MB"})
            subdir = pathlib.Path(STICKERS_ROOT) if pool == "_global" \
                else pathlib.Path(STICKERS_ROOT) / pool
            subdir.mkdir(parents=True, exist_ok=True)
            name = "st_%s%s" % (_uuid.uuid4().hex[:12], ext)
            (subdir / name).write_bytes(raw)
            data = load_json_const(STICKERS_PATH, {})
            if not isinstance(data, dict):
                data = {}
            entry = data.get(pool)
            if not isinstance(entry, dict):
                entry = {"next_id": 1, "items": []}
                data[pool] = entry
            sid = int(entry.get("next_id", 1))
            entry["next_id"] = sid + 1
            entry["items"].append({
                "id": sid,
                "file": (name if pool == "_global" else pool + "/" + name),
                "pool": "" if pool == "_global" else pool,
                "tags": [t.strip() for t in str(body.get("tags", "")).split(",") if t.strip()][:10],
                "desc": str(body.get("desc", ""))[:100], "uses": 0,
                "added": int(time.time())})
            write_const(STICKERS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True, "id": sid})
        if u.path == "/api/stickers/fav-toggle":
            data = load_json_const(STICKERS_PATH, {})
            if not isinstance(data, dict):
                data = {}
            settings = data.get("_settings")
            if not isinstance(settings, dict):
                settings = {}
                data["_settings"] = settings
            settings["fav_enabled"] = bool(body.get("enabled", True))
            write_const(STICKERS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True,
                                     "fav_enabled": settings["fav_enabled"]})
        if u.path == "/api/stickers/sync-qq":
            pool = str(body.get("pool", "_global")).strip() or "_global"
            if pool != "_global" and (pool.startswith("_") or len(pool) > 40):
                return self._reply(400, {"ok": False, "error": "库名非法"})
            try:
                count = max(1, min(int(body.get("count", 100)), 300))
            except (TypeError, ValueError):
                count = 100
            try:
                added, failed, total = sync_qq_favorite_stickers(pool, count)
                return self._reply(200, {"ok": True, "added": added,
                                         "failed": failed, "fetched": total})
            except Exception as e:
                return self._reply(400, {"ok": False, "error": repr(e)})
        if u.path == "/api/stickers/update":
            pool = str(body.get("pool", "_global")).strip() or "_global"
            sid = body.get("id")
            data = load_json_const(STICKERS_PATH, {})
            entry = data.get(pool) if isinstance(data, dict) else None
            if not isinstance(entry, dict):
                return self._reply(400, {"ok": False, "error": "库不存在"})
            for it in entry.get("items", []):
                if it.get("id") == sid:
                    if "desc" in body:
                        it["desc"] = str(body.get("desc", ""))[:200]
                    if "tags" in body:
                        tg = body.get("tags")
                        if isinstance(tg, list):
                            it["tags"] = [str(t).strip() for t in tg if str(t).strip()][:15]
                    write_const(STICKERS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
                    return self._reply(200, {"ok": True})
            return self._reply(400, {"ok": False, "error": "条目不存在"})
        if u.path == "/api/stickers/delete":
            pool = str(body.get("pool", "_global")).strip() or "_global"
            sid = body.get("id")
            data = load_json_const(STICKERS_PATH, {})
            entry = data.get(pool) if isinstance(data, dict) else None
            if not isinstance(entry, dict):
                return self._reply(400, {"ok": False, "error": "库不存在"})
            for i, it in enumerate(entry.get("items", [])):
                if it.get("id") == sid:
                    entry["items"].pop(i)
                    try:
                        (pathlib.Path(STICKERS_ROOT) / str(it.get("file", "x"))).unlink()
                    except OSError:
                        pass
                    write_const(STICKERS_PATH, json.dumps(data, ensure_ascii=False, indent=2))
                    return self._reply(200, {"ok": True})
            return self._reply(400, {"ok": False, "error": "条目不存在"})
        if u.path == "/api/personas/global":
            data = load_json_const(PERSONA_COLLECTION_PATH, {})
            data["_global"] = str(body.get("text", ""))[:20000]
            write_const(PERSONA_COLLECTION_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/cache/clean":
            # 缓存清理（回收站）；keys 由 UI 二次确认后传入
            keys = body.get("keys") or []
            if not isinstance(keys, list) or len(keys) > 10:
                return self._reply(400, {"ok": False, "error": "参数非法"})
            return self._reply(200, S.cache_clean(keys))
        if u.path == "/api/voice/config":
            # 保存语音回复配置（reply_config.voice）
            import qq_tts as _T
            v = body.get("voice")
            if not isinstance(v, dict):
                return self._reply(400, {"ok": False, "error": "参数非法"})
            clean = {
                "enabled": bool(v.get("enabled")),
                "provider": str(v.get("provider") or "edge")[:16],
                "mode": str(v.get("mode") or "keyword")[:12],
                "group_enabled": bool(v.get("group_enabled")),
                "private_enabled": bool(v.get("private_enabled", True)),
                "with_text": bool(v.get("with_text")),
                "keywords": [str(x).strip()[:20] for x in (v.get("keywords") or [])
                             if str(x).strip()][:8],
                "prob": min(1.0, max(0.0, float(v.get("prob") or 0))),
                "edge_voice": str(v.get("edge_voice") or "zh-CN-XiaoxiaoNeural")[:48],
                "zhipu_voice": str(v.get("zhipu_voice") or "")[:48],
                "zhipu_speaker_id": str(v.get("zhipu_speaker_id") or "")[:64],
                "zhipu_key": str(v.get("zhipu_key") or "")[:128],
                "sf_key": str(v.get("sf_key") or "")[:128],
                "sf_model": str(v.get("sf_model") or "FunAudioLLM/cosyvoice2-0.5B")[:64],
                "sf_voice": str(v.get("sf_voice") or "anna")[:64],
                "custom_base": str(v.get("custom_base") or "")[:200],
                "custom_key": str(v.get("custom_key") or "")[:128],
                "custom_model": str(v.get("custom_model") or "tts-1")[:64],
                "custom_voice": str(v.get("custom_voice") or "alloy")[:64],
                # GPT-SoVITS 本地引擎
                "gsv_dir": str(v.get("gsv_dir") or "")[:200],
                "gsv_port": str(v.get("gsv_port") or "")[:8],
                "gsv_voice_id": str(v.get("gsv_voice_id") or "")[:24],
                "gsv_ref_wav": str(v.get("gsv_ref_wav") or "")[:300],
                "gsv_prompt_text": str(v.get("gsv_prompt_text") or "")[:300],
                "gsv_prompt_lang": str(v.get("gsv_prompt_lang") or "zh")[:8],
                "gsv_autostart": bool(v.get("gsv_autostart")),
                "gsv_ft_model": str(v.get("gsv_ft_model") or "")[:16],
                # 合成优化（引擎参数；均 clamp 到安全范围）
                "gsv_batch_size": max(1, min(16, (lambda x: int(x) if str(x or "").strip().isdigit() else 1)(v.get("gsv_batch_size")))),
                "gsv_split": (str(v.get("gsv_split") or "cut0") if str(v.get("gsv_split") or "") in ("cut0", "cut4", "cut5") else "cut0"),
                "gsv_keepalive": bool(v.get("gsv_keepalive", True)),
                "gsv_speed": 1.0,  # 下方统一 clamp（float 解析放 dict 外更清晰）
                # 智谱克隆音色列表（name + speaker_id）
                "zhipu_saved_voices": [
                    {"name": str(x.get("name") or "")[:40],
                     "speaker_id": str(x.get("speaker_id") or "")[:64]}
                    for x in (v.get("zhipu_saved_voices") or [])[:12]
                    if isinstance(x, dict) and str(x.get("speaker_id") or "").strip()][:12],
            }
            # 表单只采自己页面的字段：POST 未携带的既有键原样保留
            # （gsv_autostart / zhipu_saved_voices 由专用端点维护——曾因整体
            # 替换被「保存设置」抹回 False/空，自启动失效、克隆音色列表清空）
            _cur = _T.voice_cfg()
            try:
                clean["gsv_speed"] = min(2.5, max(0.5, float(clean.get("gsv_speed") or _cur.get("gsv_speed") or 1.0)))
            except (TypeError, ValueError):
                clean["gsv_speed"] = 1.0
            for _k, _val in _cur.items():
                if _k not in clean:
                    clean[_k] = _val
            if "gsv_autostart" not in v:
                clean["gsv_autostart"] = bool(_cur.get("gsv_autostart"))
            if "zhipu_saved_voices" not in v:
                clean["zhipu_saved_voices"] = _cur.get("zhipu_saved_voices") or []
            _T.save_voice_cfg(clean)
            return self._reply(200, {"ok": True, "config": _T.voice_cfg()})
        if u.path == "/api/voice/preview":
            # 试听：用当前表单配置合成一句，返回 data URI（<audio> 直接播）
            import base64
            import qq_tts as _T
            v = body.get("voice") or {}
            if not isinstance(v, dict):
                return self._reply(400, {"ok": False, "error": "参数非法"})
            text = str(body.get("text") or "你好呀，这是语音试听。")[:120]
            cfg = dict(_T.voice_cfg())
            cfg.update({k: v[k] for k in (
                "provider", "edge_voice", "zhipu_voice", "zhipu_speaker_id",
                "zhipu_key", "sf_key", "sf_model", "sf_voice",
                "custom_base", "custom_key", "custom_model", "custom_voice",
                "gsv_voice_id", "gsv_ref_wav", "gsv_prompt_text",
                "gsv_prompt_lang", "gsv_port", "gsv_dir") if k in v})
            # 试听显式指定了克隆音色/手动 ref → 挂起微调模型优先级
            # （否则 gsv_tts 的 ft 优先会压过所点 ref，放出微调模型的声音——
            # 预览必须「点谁听谁」；不带音色键的试听保持真实生效配置）
            if "gsv_voice_id" in v or "gsv_ref_wav" in v:
                cfg["gsv_ft_model"] = ""
            # 微调模型试听：gsv_ft_ref = 模型 id → 用该模型自带 ref.wav 作参考
            ft_ref = str(v.get("gsv_ft_ref") or "").strip()
            if ft_ref:
                import qq_gsv_ft as _F
                idx = _F.load_index().get(ft_ref)
                if not idx:
                    return self._reply(200, {"ok": False, "error": "微调模型不存在"})
                if not str(_T.voice_cfg().get("gsv_ft_model") or "") == ft_ref:
                    return self._reply(200, {"ok": False,
                        "error": "该模型未启用（引擎加载的才能合成）——先点「启用」"})
                ref = str(idx.get("ref") or "")
                if not ref or not os.path.isfile(ref):
                    return self._reply(200, {"ok": False, "error": "该模型没有参考音频"})
                cfg["gsv_ref_wav"] = ref
                cfg["gsv_voice_id"] = ""
                cfg["gsv_prompt_text"] = str(idx.get("prompt_text") or "")
                cfg["gsv_prompt_lang"] = str(idx.get("prompt_lang") or "zh")
            try:
                p = _T.synthesize(text, cfg)
                import pathlib as _pl
                data = _pl.Path(p).read_bytes()
                try:
                    p.unlink()
                except OSError:
                    pass
                # 检测格式返回正确 MIME（WAV vs MP3）
                is_wav = data[:4] == b"RIFF"
                mime = "audio/wav" if is_wav else "audio/mpeg"
                return self._reply(200, {"ok": True, "size": len(data),
                    "audio": "data:%s;base64," % mime + base64.b64encode(data).decode()})
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
        if u.path == "/api/voice/gsv/env":
            import qq_tts as _T
            return self._reply(200, {"ok": True, **_T.gsv_env_check()})
        if u.path == "/api/voice/gsv/ft-list":
            import qq_gsv_ft as _F
            import qq_tts as _T
            return self._reply(200, {"ok": True,
                                     **_F.ft_model_list(_T.voice_cfg()),
                                     **_F.ft_health_check(_T.voice_cfg())})
        if u.path == "/api/voice/gsv/ft-import":
            # 导入微调模型（src_dir 由前端 /api/pick-dir 选出，模型复制进
            # 引擎 ft_models 受控子树；ref 可显式指定或自动挑 3-10s 一条）
            import qq_gsv_ft as _F
            import qq_tts as _T
            r = _F.ft_model_import(
                _T.voice_cfg(),
                str(body.get("name") or ""),
                str(body.get("src_dir") or ""),
                ref_wav=str(body.get("ref_wav") or "") or None,
                prompt_text=str(body.get("prompt_text") or ""),
                prompt_lang=str(body.get("prompt_lang") or "zh"))
            return self._reply(200, r)
        if u.path == "/api/voice/gsv/ft-use":
            import qq_gsv_ft as _F
            import qq_tts as _T
            r = _F.ft_model_use(_T.voice_cfg(), str(body.get("id") or ""))
            return self._reply(200, r)
        if u.path == "/api/voice/gsv/ft-del":
            import qq_gsv_ft as _F
            import qq_tts as _T
            r = _F.ft_model_del(_T.voice_cfg(), str(body.get("id") or ""))
            return self._reply(200, r)
        if u.path == "/api/voice/gsv/start":
            import qq_tts as _T
            return self._reply(200, _T.gsv_start(_T.voice_cfg()))
        if u.path == "/api/voice/gsv/stop":
            import qq_tts as _T
            return self._reply(200, _T.gsv_stop())
        if u.path == "/api/voice/gsv/voice-save":
            # 本地音色库：保存（页面已截好 16k WAV，base64 传来）
            import base64 as _b64
            import qq_tts as _T
            raw = str(body.get("audio") or "")
            if not raw or len(raw) > 20 * 1024 * 1024:
                return self._reply(200, {"ok": False, "error": "音频为空或过大"})
            try:
                wav = _b64.b64decode(raw.split(",", 1)[-1])
            except Exception:
                return self._reply(200, {"ok": False, "error": "音频解码失败"})
            try:
                vid = _T.gsv_voice_save(
                    str(body.get("name") or ""), wav,
                    str(body.get("prompt_text") or "")[:300],
                    str(body.get("prompt_lang") or "zh")[:8])
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            return self._reply(200, {"ok": True, "voice_id": vid})
        if u.path == "/api/voice/gsv/voice-delete":
            import qq_tts as _T
            ok = _T.gsv_voice_delete(str(body.get("id") or ""))
            if ok:
                return self._reply(200, {"ok": True})
            return self._reply(200, {"ok": False, "error": "音色不存在"})
        if u.path == "/api/voice/gsv/voice-use":
            # 启用一个本地音色（写 voice cfg 的 gsv_voice_id）
            import qq_tts as _T
            vid = str(body.get("id") or "").strip()
            v = _T.gsv_voice_get(vid) if vid else None
            if not v:
                return self._reply(200, {"ok": False, "error": "音色不存在"})
            cfg = dict(_T.voice_cfg())
            cfg["gsv_voice_id"] = vid
            _T.save_voice_cfg(cfg)
            return self._reply(200, {"ok": True, "name": v.get("name")})
        if u.path == "/api/voice/gsv/autostart":
            import qq_tts as _T
            cfg = dict(_T.voice_cfg())
            cfg["gsv_autostart"] = bool(body.get("on"))
            _T.save_voice_cfg(cfg)
            return self._reply(200, {"ok": True, "gsv_autostart": bool(cfg["gsv_autostart"])})
        if u.path == "/api/voice/gsv/preview":
            # 克隆试听：用页面截好的临时参考音频合成（不进音色库，满意再保存）
            import base64 as _b64
            import qq_tts as _T
            raw = str(body.get("audio") or "")
            if not raw or len(raw) > 20 * 1024 * 1024:
                return self._reply(200, {"ok": False, "error": "音频为空或过大"})
            try:
                wav = _b64.b64decode(raw.split(",", 1)[-1])
            except Exception:
                return self._reply(200, {"ok": False, "error": "音频解码失败"})
            try:
                data = _T.gsv_preview_bytes(
                    str(body.get("text") or "你好呀，我是你的语音小助手，听听这个声音怎么样？"),
                    wav,
                    str(body.get("prompt_text") or "")[:300],
                    str(body.get("prompt_lang") or "zh")[:8],
                    _T.voice_cfg())
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            return self._reply(200, {"ok": True, "size": len(data),
                "audio": "data:audio/wav;base64," + _b64.b64encode(data).decode()})
        if u.path == "/api/voice/clone":
            # 克隆：设置页在浏览器里截好 3-30 秒 WAV（base64）传上来，
            # 上传智谱 → /voice/clone → 返回 speaker_id。统一克隆流程
            # （上传→克隆→试听→命名保存）里名字留到「保存」步再起——
            # voice_name 缺省时平台会用默认名，本地列表不入列（等 zvoice-save）
            import base64 as _b64
            import qq_tts as _T
            v = body.get("voice") if isinstance(body.get("voice"), dict) else {}
            name = str(body.get("voice_name") or "").strip()[:40] or "我的克隆音色"
            raw = str(body.get("audio") or "")
            if not raw or len(raw) > 20 * 1024 * 1024:
                return self._reply(200, {"ok": False, "error": "音频为空或过大（上限约 15MB）"})
            try:
                wav = _b64.b64decode(raw.split(",", 1)[-1])
            except Exception:
                return self._reply(200, {"ok": False, "error": "音频数据解码失败"})
            cfg = dict(_T.voice_cfg())
            if v.get("zhipu_key"):
                cfg["zhipu_key"] = str(v["zhipu_key"])[:128]
            try:
                vid, ofid, used = _T.clone_zhipu_voice(
                    _T.zhipu_keys(cfg), wav, name,
                    str(body.get("sample_text") or "")[:300],
                    str(body.get("preview_text") or "")[:120])
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            # 仅当调用方显式传了名字（旧流程兼容）才自动入列；新流程由
            # 「保存到音色列表」走 zvoice-save，列表条目用用户起的名字
            if str(body.get("voice_name") or "").strip():
                try:
                    cfg2 = dict(_T.voice_cfg())
                    lst = [x for x in (cfg2.get("zhipu_saved_voices") or [])
                           if str(x.get("speaker_id")) != vid]
                    lst.insert(0, {"name": used, "speaker_id": vid})
                    cfg2["zhipu_saved_voices"] = lst[:12]
                    _T.save_voice_cfg(cfg2)
                except Exception:
                    pass
            return self._reply(200, {"ok": True, "voice_id": vid,
                                     "voice_name_used": used,
                                     "output_file_id": ofid})
        if u.path == "/api/client-error":
            # 页面 JS 错误/闪屏卡住上报：只落服务日志（stdout→日志文件），
            # 让「卡加载/白屏」类问题有第一现场，不再靠猜
            print("[CLIENT_ERR] %s %s" % (str(body.get("kind") or "?")[:30],
                                          str(body.get("detail") or "")[:400]),
                  flush=True)
            return self._reply(200, {"ok": True})
        if u.path == "/api/voice/zvoice-save":
            # 保存克隆音色到列表（统一克隆流程第 ④ 步：试听满意后命名保存）
            import qq_tts as _T
            sid = str(body.get("speaker_id") or "").strip()[:64]
            name = str(body.get("name") or "").strip()[:40]
            if not sid:
                return self._reply(200, {"ok": False, "error": "缺少 speaker_id（先完成克隆）"})
            if not name:
                return self._reply(200, {"ok": False, "error": "先给这个音色起个名字"})
            cfg = dict(_T.voice_cfg())
            lst = [x for x in (cfg.get("zhipu_saved_voices") or [])
                   if str(x.get("speaker_id")) != sid]
            lst.insert(0, {"name": name, "speaker_id": sid})
            cfg["zhipu_saved_voices"] = lst[:12]
            _T.save_voice_cfg(cfg)
            return self._reply(200, {"ok": True, "name": name})
        if u.path == "/api/voice/zvoice-del":
            # 删除智谱克隆音色列表条目（仅本地记录，不动平台侧音色）
            import qq_tts as _T
            sid = str(body.get("speaker_id") or "").strip()
            cfg = dict(_T.voice_cfg())
            lst = [x for x in (cfg.get("zhipu_saved_voices") or [])
                   if str(x.get("speaker_id")) != sid]
            cfg["zhipu_saved_voices"] = lst
            _T.save_voice_cfg(cfg)
            return self._reply(200, {"ok": True})
        if u.path == "/api/voice/voices":
            # 复刻音色列表（管理/删除用）
            import qq_tts as _T
            cfg = dict(_T.voice_cfg())
            try:
                voices = _T.list_zhipu_voices(_T.zhipu_key(cfg))
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            return self._reply(200, {"ok": True, "voices": voices,
                "current": str(_T.voice_cfg().get("zhipu_speaker_id") or "")})
        if u.path == "/api/voice/voice-delete":
            # 删除复刻音色（danger：前端已二次确认）；删的是当前使用的则顺手清空配置
            import qq_tts as _T
            vid = str(body.get("voice_id") or "").strip()
            if not vid:
                return self._reply(200, {"ok": False, "error": "缺少音色 ID"})
            cfg = dict(_T.voice_cfg())
            try:
                did, _ts = _T.delete_zhipu_voice(_T.zhipu_key(cfg), vid)
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            if str(cfg.get("zhipu_speaker_id") or "").strip() == did:
                cfg["zhipu_speaker_id"] = ""
                _T.save_voice_cfg(cfg)
            return self._reply(200, {"ok": True, "voice": did,
                "cleared_current": True})
        if u.path == "/api/lark/config":
            # 保存飞书配置（app_secret 单独保存，不回显）
            import qq_lark as _L
            v = body.get("config")
            if not isinstance(v, dict):
                return self._reply(400, {"ok": False, "error": "参数非法"})
            old = _L.cfg()
            clean = {
                "enabled": bool(v.get("enabled")),
                "app_id": str(v.get("app_id") or "").strip()[:64],
                "bridges": [],
            }
            for b in (v.get("bridges") or [])[:10]:
                if not isinstance(b, dict):
                    continue
                qq_g = str(b.get("qq_group") or "").strip()[:24]
                lk = str(b.get("lark_chat_id") or "").strip()[:64]
                if qq_g and lk:
                    clean["bridges"].append({
                        "qq_group": qq_g, "lark_chat_id": lk,
                        "to_lark": bool(b.get("to_lark", True)),
                        "to_qq": bool(b.get("to_qq", False)),
                    })
            secret = str(v.get("app_secret") or "").strip()[:128]
            clean["app_secret"] = secret if secret else str(old.get("app_secret") or "")
            _L.save_cfg(clean)
            _L._TOKEN["value"] = ""
            return self._reply(200, {"ok": True})
        if u.path == "/api/lark/test":
            # 向指定飞书群发测试消息（验证配置）
            import qq_lark as _L
            try:
                _L.send_text(str(body.get("chat_id") or ""), "✅ ZCode QQ机器人·飞书互通测试成功")
                return self._reply(200, {"ok": True})
            except Exception as e:
                return self._reply(200, {"ok": False, "error": str(e)[:200]})
        if u.path == "/api/pause":
            paused = bool(body.get("paused"))
            cfg = load_json_const(MODEL_CHAIN_PATH, {})
            cfg["paused"] = paused
            write_const(MODEL_CHAIN_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
            try:
                conn = http.client.HTTPConnection("127.0.0.1", _agent_port(), timeout=5)
                payload = json.dumps({"paused": paused}).encode("utf-8")
                conn.request("POST", "/admin/pause", body=payload,
                             headers={"Content-Type": "application/json"})
                conn.getresponse().read()
                conn.close()
            except OSError:
                pass
            return self._reply(200, {"ok": True, "paused": paused})
        if u.path == "/api/napcat-token-sync":
            # daemon WS 401 自愈：读 NapCat 当前 token，与 ws_token 不一致则采纳
            ok, msg = S.ensure_napcat_config(C.load_app_config())
            return self._reply(200, {"ok": bool(ok), "msg": msg})
        if u.path == "/api/pin-model":
            # 转发 agent /admin/pin-model（立即使用/取消锁定），原样回传其响应
            try:
                conn = http.client.HTTPConnection("127.0.0.1", _agent_port(), timeout=5)
                payload = json.dumps(body).encode("utf-8")
                conn.request("POST", "/admin/pin-model", body=payload,
                             headers={"Content-Type": "application/json"})
                r = json.loads(conn.getresponse().read().decode("utf-8"))
                conn.close()
                return self._reply(200, r)
            except (OSError, ValueError) as e:
                return self._reply(200, {"ok": False,
                                         "error": "agent 不可达（未运行？）%r" % e})
        if u.path == "/api/evolved/toggle":
            name = str(body.get("name", "")).strip()
            data = load_json_const(EVOLVED_PATH, {})
            if not isinstance(data, dict):
                data = {}
            entry = data.get(name)
            if not isinstance(entry, dict):
                entry = {"enabled": True, "notes": []}
                data[name] = entry
            entry["enabled"] = bool(body.get("enabled", True))
            write_const(EVOLVED_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/evolved/clear":
            name = str(body.get("name", "")).strip()
            data = load_json_const(EVOLVED_PATH, {})
            if isinstance(data, dict) and isinstance(data.get(name), dict):
                data[name]["notes"] = []
                write_const(EVOLVED_PATH, json.dumps(data, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/file":
            kind = body.get("_kind") or urllib.parse.parse_qs(u.query).get("kind", [""])[0]
            text = str(body.get("text", ""))[:20000]
            if kind == "persona":
                write_const(PERSONA_PATH, text)
            elif kind == "style":
                # 0.5.10：风格卡按人设存储（target=人设名或 _global_）
                import qq_style_store as _SS
                _SS.set_style(str(body.get("target") or "_global_"), text)
            else:
                return self._reply(400, {"ok": False, "error": "kind 必须是 persona|style"})
            return self._reply(200, {"ok": True})
        if u.path == "/api/style/targets":
            # 风格目标列表 + 各目标当前文本（风格卡页用）
            # has_own = 有风格内容（专属风格卡 **或** 自进化观察）——用户视角
            # 这页统称"风格"，有 30 条自进化却无 ✓ 会被理解为"没风格"
            import qq_style_store as _SS
            coll = _SS._load_coll()
            ev_data = load_json_const(EVOLVED_PATH, {})
            targets = []
            for t, label in _SS.style_targets(coll):
                if t == "_global_":
                    txt = _SS.global_style()
                    has_ev = any(
                        isinstance(v, dict) and v.get("notes")
                        for v in (ev_data.values() if isinstance(ev_data, dict) else []))
                    ev_n = sum(len(v.get("notes") or []) for v in
                               (ev_data.values() if isinstance(ev_data, dict) else [])
                               if isinstance(v, dict))
                else:
                    v = _SS.persona_style(t)
                    txt = v if v is not None else ""
                    entry = ev_data.get(t) if isinstance(ev_data, dict) else None
                    has_ev = bool(isinstance(entry, dict) and entry.get("notes"))
                    ev_n = len(entry.get("notes") or []) if isinstance(entry, dict) else 0
                targets.append({"id": t, "label": label,
                                "has_own": bool(str(txt or "").strip()) or has_ev,
                                "has_card": bool(str(txt or "").strip()),
                                "evolved": has_ev, "ev_count": ev_n})
            return self._reply(200, {"ok": True, "targets": targets,
                                     "active": str(coll.get("_active", "") or "")})
        if u.path == "/api/style/read":
            import qq_style_store as _SS
            t = str(body.get("target") or "_global_")
            if t == "_global_":
                txt = _SS.global_style()
            else:
                v = _SS.persona_style(t)
                txt = v if v is not None else ""
            return self._reply(200, {"ok": True, "text": str(txt or "")})
        if u.path == "/api/style/dup-old":
            # 把旧全局风格文件初始化到某目标（老用户首次分目标保存的引导）
            import qq_style_store as _SS
            t = str(body.get("target") or "").strip()
            if not t:
                return self._reply(400, {"ok": False, "error": "缺少 target"})
            _SS.duplicate_style(t)
            return self._reply(200, {"ok": True})
        if u.path == "/api/personas/dup":
            # 创建人设副本（含其 per-persona 风格）——污染可快速切回
            import qq_style_store as _SS
            ok2, err = _SS.duplicate_persona(str(body.get("name") or ""),
                                             str(body.get("new_name") or ""))
            if not ok2:
                return self._reply(400, {"ok": False, "error": err})
            return self._reply(200, {"ok": True})
        if u.path in ("/api/keys/add", "/api/keys/del", "/api/keys/rename",
                      "/api/keys/primary"):
            data = load_json_const(KEYS_PATH, {})
            keys = data.get("keys") if isinstance(data.get("keys"), list) else []
            primary = str(data.get("primary", "")).strip()
            if u.path == "/api/keys/add":
                key = str(body.get("key", "")).strip()
                label = str(body.get("label", "")).strip()[:20]
                provider = str(body.get("provider") or "zhipu").strip().lower()
                base_url = str(body.get("base_url") or "").strip()[:300]
                if provider not in ("zhipu", "deepseek", "kimi", "openai",
                                    "grok", "gemini", "custom"):
                    return self._reply(400, {"ok": False, "error": "供应商非法"})
                if provider == "custom":
                    u2 = urllib.parse.urlsplit(base_url)
                    if u2.scheme not in ("http", "https") or not u2.hostname:
                        return self._reply(400, {"ok": False, "error": "自定义供应商需填 Base URL（http/https）"})
                    if not C.safe_api_host(u2.hostname):
                        return self._reply(400, {"ok": False, "error": "Base URL 主机被拒绝（禁止内网/环回地址）"})
                if len(key) < 20 or len(key) > 200 or any(c.isspace() for c in key):
                    return self._reply(400, {"ok": False, "error": "Key 格式非法（20-200字符，无空白）"})
                if len(keys) >= 10:
                    return self._reply(400, {"ok": False, "error": "Key 最多 10 个"})
                nid = "k%d" % (max([int(e["id"][1:]) for e in keys
                                    if isinstance(e, dict) and str(e.get("id", "")).startswith("k")
                                    and str(e["id"])[1:].isdigit()] or [0]) + 1)
                ent = {"id": nid, "label": label or ("Key " + nid[1:]), "key": key,
                       "provider": provider}
                if provider == "custom":
                    ent["base_url"] = base_url
                keys.append(ent)
                if not primary:
                    primary = nid  # 第一个 Key 自动成为主 Key
            elif u.path == "/api/keys/primary":
                kid = str(body.get("id", "")).strip()
                if not any(isinstance(e, dict) and e.get("id") == kid for e in keys):
                    return self._reply(400, {"ok": False, "error": "Key 不存在"})
                primary = kid
            else:
                kid = str(body.get("id", "")).strip()
                hit = [e for e in keys if isinstance(e, dict) and e.get("id") == kid]
                if not hit:
                    return self._reply(400, {"ok": False, "error": "Key 不存在"})
                if u.path == "/api/keys/del":
                    keys.remove(hit[0])
                    if primary == kid:
                        primary = keys[0]["id"] if keys and isinstance(keys[0], dict) else ""
                else:
                    nl = str(body.get("label", "")).strip()[:20]
                    if nl:
                        hit[0]["label"] = nl
                    if "provider" in body:
                        prov = str(body.get("provider") or "zhipu").strip().lower()
                        if prov not in ("zhipu", "deepseek", "kimi", "openai",
                                        "grok", "gemini", "custom"):
                            return self._reply(400, {"ok": False, "error": "供应商非法"})
                        if prov == "custom":
                            b = str(body.get("base_url") or "").strip()[:300]
                            u3 = urllib.parse.urlsplit(b)
                            if u3.scheme not in ("http", "https") or not u3.hostname \
                                    or not C.safe_api_host(u3.hostname):
                                return self._reply(400, {"ok": False, "error": "自定义供应商 Base URL 非法（需 http/https 公网主机）"})
                            hit[0]["base_url"] = b
                        else:
                            hit[0].pop("base_url", None)
                        hit[0]["provider"] = prov
            write_const(KEYS_PATH, json.dumps(
                {"keys": keys, "primary": primary}, ensure_ascii=False, indent=2))
            return self._reply(200, {"ok": True})
        if u.path == "/api/plugins/call":
            # 插件自定义端点统一入口：转发到 agent 进程里插件注册的
            # api.route() 处理器——第三方插件的能力闭环（UI 纯代理）
            name = str(body.get("name") or "").strip()
            method = str(body.get("method") or "").strip()
            if not name or not method:
                return self._reply(400, {"ok": False, "error": "缺少 name/method"})
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            res = proxy_post("127.0.0.1", _agent_port(), "/admin/plugin-call",
                             {"name": name, "method": method, "args": args},
                             token=read_const(TOKEN_PATH), timeout=60)
            return self._reply(200, res)
        if u.path == "/api/devctrl-flow":
            import qq_bot_agent as _A
            patch = {}
            if body.get("reset"):
                patch = dict(_A._DEFAULT_FLOW)
            else:
                if "trigger_mode" in body:
                    if body["trigger_mode"] not in ("auto", "keyword"):
                        return self._reply(400, {"ok": False, "error": "trigger_mode 非法"})
                    patch["trigger_mode"] = body["trigger_mode"]
                if "use_custom_text" in body:
                    patch["use_custom_text"] = bool(body["use_custom_text"])
                if "keywords" in body:
                    kw = body["keywords"]
                    if isinstance(kw, str):
                        kw = [x.strip() for x in kw.replace("，", ",").split(",") if x.strip()]
                    if not isinstance(kw, list) or len(kw) > 20:
                        return self._reply(400, {"ok": False, "error": "关键词列表非法"})
                    patch["keywords"] = [str(x).strip()[:20] for x in kw if str(x).strip()]
                for f in ("ask_text", "ok_text", "fail_text", "expired_text", "done_text"):
                    if f in body:
                        v = str(body[f])
                        if len(v) > 1000:
                            return self._reply(400, {"ok": False, "error": "%s 过长（<=1000字）" % f})
                        patch[f] = v
                if "auth_window_sec" in body:
                    try:
                        patch["auth_window_sec"] = max(60, min(86400, int(body["auth_window_sec"])))
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": "授权窗口应为 1-1440 分钟（60-86400 秒）"})
            saved = _A.save_devctrl_flow(patch)
            return self._reply(200, {"ok": True, "flow": saved})
        if u.path == "/api/memory/clear":
            write_const(MEMORY_STREAM_PATH, "")
            return self._reply(200, {"ok": True})
        if u.path == "/api/plugins/market-install":
            name = str(body.get("name", "")).strip()
            if not name:
                return self._reply(400, {"ok": False, "error": "缺少插件名"})

            def _job(tid):
                import qq_plugins as _qp

                def _prog(phase, pct, msg):
                    task_update(tid, phase=phase, pct=pct, msg=msg)
                return _qp.market_install(name, progress=_prog)
            return self._reply(200, {"ok": True,
                                     "task": task_start("安装插件", _job)})
        if u.path == "/api/plugins/install":
            # 文件对话框选 zip → 安全解压校验 → 装到用户插件目录（同名=更新覆盖）
            zipf = body.get("zip") if isinstance(body, dict) else None
            if not zipf:
                import threading as _th
                boxz = {}

                def _pick():
                    try:
                        import tkinter as tk
                        from tkinter import filedialog
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes("-topmost", True)
                        f = filedialog.askopenfilename(
                            title="选择插件 zip 包",
                            filetypes=[("zip 压缩包", "*.zip")])
                        root.destroy()
                        boxz["f"] = f or ""
                    except Exception:
                        boxz["f"] = ""
                _t0 = _th.Thread(target=_pick, daemon=True)
                _t0.start()
                _t0.join(timeout=180)
                zipf = boxz.get("f", "")
                if not zipf:
                    return self._reply(200, {"ok": False, "error": "未选择文件"})
            try:
                import qq_plugins as _qp
                res = _qp.install_from_zip(zipf)
                if res.get("ok"):
                    return self._reply(200, res)
                return self._reply(400, res)
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/plugins/uninstall":
            name = str(body.get("name", "")).strip()
            try:
                import qq_plugins as _qp
                res = _qp.uninstall(name)
                return self._reply(200 if res.get("ok") else 400, res)
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/chatimport/qqcli-export":
            # 用本机 qq-cli 技能导出指定会话并入库（job 化，前端轮询进度）
            import qq_chatimport as _CI
            chat = str(body.get("chat") or "")

            def _job(tid):
                def _prog(phase, pct, msg):
                    task_update(tid, phase=phase, pct=pct, msg=msg)
                return _CI.qq_cli_export(chat, on_progress=_prog)
            return self._reply(200, {"ok": True,
                                     "task": task_start("QQ 导出「%s」" % chat[:16], _job)})
        if u.path == "/api/chatimport/wxcli-export":
            # 用本机 wechat-cli 技能导出指定会话并入库（job 化）
            import qq_chatimport as _CI
            chat = str(body.get("chat") or "")

            def _job(tid):
                def _prog(phase, pct, msg):
                    task_update(tid, phase=phase, pct=pct, msg=msg)
                return _CI.wx_cli_export(chat, on_progress=_prog)
            return self._reply(200, {"ok": True,
                                     "task": task_start("微信导出「%s」" % chat[:16], _job)})
        if u.path == "/api/chatimport/pick":
            # 文件对话框选导出文件 → 只解析预览（不落盘）
            import threading as _th
            import qq_chatimport as _CI
            boxf = {}
            def _pickf():
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    f = filedialog.askopenfilename(
                        title="选择聊天记录导出文件",
                        filetypes=[("聊天记录", "*.md *.txt *.json"),
                                   ("所有文件", "*.*")])
                    root.destroy()
                    boxf["f"] = f or ""
                except Exception:
                    boxf["f"] = ""
            _t0 = _th.Thread(target=_pickf, daemon=True)
            _t0.start()
            _t0.join(timeout=180)
            fpath = str(boxf.get("f") or "")
            if not fpath:
                return self._reply(200, {"ok": False, "error": "未选择文件"})
            r = _CI.parse_file(fpath)
            r["path"] = fpath
            r["preview"] = [("%s %s: %s" % (time.strftime(
                "%m-%d %H:%M", time.localtime(m.get("ts") or 0)) if m.get("ts") else "?",
                m.get("sender", "?"), str(m.get("text", ""))[:60]))
                for m in (r.get("messages") or [])[:5]]
            r.pop("messages", None)
            return self._reply(200, r)
        if u.path == "/api/chatimport/store":
            # 解析并落隔离库（DATA_DIR/chat_import/<source>/<会话>/）
            import qq_chatimport as _CI
            src = str(body.get("source") or "qq")
            session = str(body.get("session") or "").strip()
            fpath = str(body.get("path") or "")
            if not session:
                session = pathlib.Path(fpath).stem if fpath else ""
            if not session:
                return self._reply(400, {"ok": False, "error": "缺少会话名"})
            r = _CI.store(src, session, fpath)
            if r.get("ok"):
                r["sessions"] = _CI.sessions(src)
            return self._reply(200 if r.get("ok") else 400, r)
        if u.path == "/api/chatimport/delete":
            import qq_chatimport as _CI
            r = _CI.delete(str(body.get("source") or "qq"),
                           str(body.get("session") or ""))
            if r.get("ok"):
                r["sessions"] = _CI.sessions(str(body.get("source") or "qq"))
            return self._reply(200 if r.get("ok") else 400, r)
        if u.path == "/api/chatimport/learn":
            # 代理 agent 学习（LLM 消化隔离库采样 → 人设草稿）
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", _agent_port(), timeout=180)
                conn.request("POST", "/admin/chatimport-learn",
                             json.dumps({"source": str(body.get("source") or "qq"),
                                         "session": str(body.get("session") or ""),
                                         "who": str(body.get("who") or "")}).encode("utf-8"),
                             {"Content-Type": "application/json"})
                raw = conn.getresponse().read().decode("utf-8", "replace")
                conn.close()
                return self._reply(200, json.loads(raw))
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/bili/login-start":
            try:
                import qq_bilibili as _qb
                return self._reply(200, _qb.login_start())
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/bili/login-poll":
            try:
                import qq_bilibili as _qb
                return self._reply(200, _qb.login_poll(str(body.get("key", ""))))
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/bili/logout":
            try:
                p = pathlib.Path(os.path.join(C.DATA_DIR, "bilibili_cookies.json"))
                if p.exists():
                    p.unlink()
                return self._reply(200, {"ok": True})
            except OSError as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/bili/run":
            try:
                conn = http.client.HTTPConnection("127.0.0.1", _agent_port(), timeout=8)
                conn.request("POST", "/admin/bili-run", body=b"{}",
                             headers={"Content-Type": "application/json"})
                r = conn.getresponse().read().decode("utf-8", "replace")
                conn.close()
                return self._reply(200, json.loads(r))
            except Exception as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path in ("/api/plugins/toggle", "/api/plugins/setting"):
            import qq_plugins as _qp
            name = str(body.get("name", "")).strip()
            if not name or name not in _qp.PluginHost().scan():
                return self._reply(400, {"ok": False, "error": "插件不存在"})
            st = _qp.PluginHost()._state()
            data = dict(st) if isinstance(st, dict) else {}
            if u.path == "/api/plugins/toggle":
                en = data.setdefault("enabled", {})
                if not isinstance(en, dict):
                    en = {}
                    data["enabled"] = en
                en[name] = bool(body.get("enabled"))
            else:
                sets = data.setdefault("settings", {})
                if not isinstance(sets, dict):
                    sets = {}
                    data["settings"] = sets
                vals = sets.setdefault(name, {})
                if not isinstance(vals, dict):
                    vals = {}
                    sets[name] = vals
                vals[str(body.get("key", ""))[:40]] = body.get("value")
            try:
                write_const(_qp.STATE_PATH, json.dumps(data, ensure_ascii=False, indent=2))
                return self._reply(200, {"ok": True})
            except OSError as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/member-profiles/save":
            qq = str(body.get("qq", "")).strip()
            name = str(body.get("name", "")).strip()[:40]
            note = str(body.get("note", "")).strip()[:300]
            if not qq.isdigit() or len(qq) < 5 or len(qq) > 12:
                return self._reply(400, {"ok": False, "error": "QQ 号需为 5-12 位数字"})
            try:
                d = load_json_const(MEMBER_PROFILES_PATH, {})
                members = d.get("members") if isinstance(d.get("members"), dict) else {}
                old = members.get(qq) or {}
                members[qq] = {"name": name or str(old.get("name") or ""),
                               "note": note, "groups": old.get("groups") or [],
                               "updated": int(time.time())}
                _mp_write(members)
                return self._reply(200, {"ok": True})
            except OSError as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path == "/api/member-profiles/del":
            qq = str(body.get("qq", "")).strip()
            try:
                d = load_json_const(MEMBER_PROFILES_PATH, {})
                members = d.get("members") if isinstance(d.get("members"), dict) else {}
                if qq in members:
                    members.pop(qq)
                    _mp_write(members)
                return self._reply(200, {"ok": True})
            except OSError as e:
                return self._reply(500, {"ok": False, "error": repr(e)})
        if u.path in ("/api/memory/update", "/api/memory/delete",
                      "/api/memory/add"):
            def _lines():
                try:
                    with open(MEMORY_STREAM_PATH, "r", encoding="utf-8") as f:
                        return f.readlines()
                except OSError:
                    return []

            def _flush(lines):
                tmp = pathlib.Path(MEMORY_STREAM_PATH + ".tmp")
                tmp.write_text("".join(lines), encoding="utf-8")
                last = None
                for _ in range(3):  # 与 agent 追加写的微竞态：短暂重试
                    try:
                        os.replace(str(tmp), MEMORY_STREAM_PATH)
                        return
                    except PermissionError as e:
                        last = e
                        time.sleep(0.2)
                raise last

            try:
                if u.path == "/api/memory/update":
                    idx = int(body.get("idx", -1))
                    text = str(body.get("text", "")).strip()
                    if idx < 0 or not text or len(text) > 300:
                        return self._reply(400, {"ok": False,
                                                 "error": "idx/text 非法（1-300字）"})
                    lines = _lines()
                    if idx >= len(lines):
                        return self._reply(400, {"ok": False, "error": "记忆行不存在"})
                    rec = json.loads(lines[idx])
                    rec["text"] = text
                    # 类型/对象可选一并修改（校验同新增）
                    if "scope" in body or "id" in body:
                        scope = body.get("scope", rec.get("scope"))
                        cid = str(body.get("id", rec.get("id", ""))).strip()
                        if scope not in ("group", "contact") or \
                                not cid.isdigit() or len(cid) > 12:
                            return self._reply(400, {"ok": False,
                                                     "error": "scope/id 非法"})
                        rec["scope"] = scope
                        rec["id"] = cid
                    lines[idx] = json.dumps(rec, ensure_ascii=False) + "\n"
                    _flush(lines)
                elif u.path == "/api/memory/delete":
                    idx = int(body.get("idx", -1))
                    lines = _lines()
                    if idx < 0 or idx >= len(lines):
                        return self._reply(400, {"ok": False, "error": "记忆行不存在"})
                    del lines[idx]
                    _flush(lines)
                else:  # add
                    scope = body.get("scope")
                    cid = str(body.get("id", "")).strip()
                    text = str(body.get("text", "")).strip()
                    if scope not in ("group", "contact") or \
                            not cid.isdigit() or len(cid) > 12 or \
                            not text or len(text) > 300:
                        return self._reply(400, {"ok": False,
                                                 "error": "scope/id/text 非法（1-300字）"})
                    rec = {"ts": int(time.time()), "scope": scope,
                           "id": cid, "text": text}
                    _lines_append = _lines()
                    _lines_append.append(json.dumps(rec, ensure_ascii=False) + "\n")
                    _flush(_lines_append)
                return self._reply(200, {"ok": True})
            except (ValueError, OSError) as e:
                return self._reply(400, {"ok": False, "error": repr(e)})
        return self._reply(404, {"ok": False, "error": "not found"})


def _watch_parent(ppid):
    """父进程（桌面窗口）消失时自动结束控制台，避免无窗口/无托盘的残留进程。

    只对「由 qq_app 启动并传入 --parent-pid」生效；浏览器/命令行单独启动不受影响。
    """
    if os.name != "nt" or not ppid:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x00100000, False, int(ppid))  # SYNCHRONIZE
        if not h:
            os._exit(0)  # 父进程已退出
        try:
            k32.WaitForSingleObject(h, 0xFFFFFFFF)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return
    os._exit(0)


# ---------------------------------------------------------------------------
# 长任务进度系统：导出/插件安装等耗时操作 job 化，前端轮询 /api/task/status
# ---------------------------------------------------------------------------
_TASKS = {}
_TASKS_LOCK = threading.Lock()


def task_start(label, fn):
    """后台跑 fn(tid)；fn 内用 task_update(tid, phase/pct/msg) 汇报进度，
    返回值作为最终 result（dict）。返回 task_id。"""
    tid = uuid.uuid4().hex[:12]
    with _TASKS_LOCK:
        _TASKS[tid] = {"label": str(label)[:60], "phase": "starting", "pct": 0,
                       "msg": "", "done": False, "ok": None, "result": None,
                       "ts": time.time()}
        if len(_TASKS) > 40:      # 只留最近 40 个任务记录
            for k in sorted(_TASKS, key=lambda x: _TASKS[x]["ts"])[:-40]:
                _TASKS.pop(k, None)

    def _run():
        try:
            res = fn(tid) or {}
            with _TASKS_LOCK:
                _TASKS[tid].update(done=True, ok=bool(res.get("ok", True)),
                                   result=res, phase="done", pct=100)
        except Exception as e:
            with _TASKS_LOCK:
                _TASKS[tid].update(done=True, ok=False,
                                   result={"ok": False, "error": str(e)[:300]},
                                   phase="error")
    threading.Thread(target=_run, daemon=True).start()
    return tid


def task_update(tid, phase=None, pct=None, msg=None):
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return
        if phase is not None:
            t["phase"] = str(phase)[:40]
        if pct is not None:
            t["pct"] = max(0, min(100, int(pct)))
        if msg is not None:
            t["msg"] = str(msg)[:160]


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="qq-bot admin UI")
    ap.add_argument("--port", type=int, default=DEFAULT_UI_PORT)
    ap.add_argument("--daemon-port", type=int, default=DEFAULT_DAEMON_PORT)
    ap.add_argument("--agent-port", type=int, default=DEFAULT_AGENT_PORT)
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="父进程 PID；父进程退出时本进程自动退出（桌面应用用）")
    args = ap.parse_args()
    PORTS["daemon"] = args.daemon_port
    PORTS["agent"] = args.agent_port

    class UiServer(ThreadingHTTPServer):
        daemon_threads = True
        # Windows 上 SO_REUSEADDR 是「强占」语义：两个进程（如升级残留的旧版
        # UI 孤儿 + 新版 UI）可同时 bind 同一端口，请求被随机路由到其中一个
        # ——页面从新进程加载、保存 POST 却打到旧进程，前端必现「网络错误」。
        # 独占绑定：第二个实例启动即失败，冲突在日志里立刻可见。
        allow_reuse_address = False

        def server_bind(self):
            if os.name == "nt":
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        srv = UiServer(("127.0.0.1", args.port), UiHandler)
    except OSError as e:
        print(json.dumps({"admin_ui_ready": False,
                          "error": "端口 %d 被占用（多半是旧版本残留进程仍在"
                                   "运行）：%r。请在任务管理器结束所有 "
                                   "ZCodeQQBot.exe 后重开。" % (args.port, e)},
                         ensure_ascii=False), flush=True)
        sys.exit(1)
    ui_token()  # 启动即生成令牌文件（不等首个请求；配合 qq_app 的 ensure_ui_token）
    if args.parent_pid:
        threading.Thread(target=_watch_parent, args=(args.parent_pid,),
                         daemon=True).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    threading.Thread(target=_auto_update_check, daemon=True).start()
    threading.Thread(target=_gsv_autostart, daemon=True).start()
    print(json.dumps({"admin_ui_ready": True, "url": "http://127.0.0.1:%d" % args.port,
                      "watchdog": True}, ensure_ascii=False), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
