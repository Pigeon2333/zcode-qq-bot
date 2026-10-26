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
import sys
import threading
import time
import urllib.parse
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
STICKERS_ROOT = pathlib.Path(os.path.join(DATA_DIR, "stickers"))
WS_TOKEN_PATH = os.path.join(DATA_DIR, "ws_token")

DEFAULT_UI_PORT = 39765
DEFAULT_DAEMON_PORT = 39763
DEFAULT_AGENT_PORT = 39764

PORTS = {"daemon": DEFAULT_DAEMON_PORT, "agent": DEFAULT_AGENT_PORT}

# 最近一次「启动机器人」任务的实时状态（内存态，UI 进程内）
_boot_lock = threading.Lock()
_boot_state = {"running": False, "done": False, "failed": False,
               "started_at": 0.0, "final": "", "steps": []}

# NapCat 活性看门狗（UI 进程内运行：能调 power_off/on 做整机恢复而不自杀）
_wd_lock = threading.Lock()
_wd_state = {"events": [], "strikes": 0, "last_check": 0.0, "busy": False,
             "last_result": ""}


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
        conn = http.client.HTTPConnection("127.0.0.1", PORTS["daemon"], timeout=8)
        body = json.dumps({"chat_type": "private", "chat_id": owner,
                           "texts": [text]}, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/send", body, {"Content-Type": "application/json",
                                             "X-QQBot-Token": token})
        conn.getresponse().read()
        conn.close()
    except Exception:
        pass


def _wd_recover(cfg):
    """自动恢复：整机电源周期（NapCat+daemon+agent）。仅在 UI 进程执行安全。"""
    with _wd_lock:
        if _wd_state["busy"]:
            return "上一次恢复仍在进行"
        _wd_state["busy"] = True
    try:
        S.power_off(cfg, include_napcat=True)
        time.sleep(2)
        notes = S.power_on(cfg)
        ok = any(n.get("component") == "napcat" and n.get("ok") for n in notes)
        result = "已自动重启 NapCat 与组件" + ("（成功）" if ok else "（WS 未就绪，登录可能需要扫码）")
        _wd_event("recover", result)
        _wd_notify_owner(cfg, "看门狗：检测到 NapCat 消息推送失联，" + result)
        return result
    finally:
        with _wd_lock:
            _wd_state["busy"] = False


def _wd_check_once():
    """单次健康检查。返回 (healthy, detail)。连续异常累计 strikes，达阈值动作。"""
    cfg = C.load_app_config()
    if not cfg.get("watchdog_enabled"):
        return True, "disabled"
    with _boot_lock:
        if _boot_state["running"]:
            return True, "boot-in-progress"
    daemon_up = S.port_open(PORTS["daemon"])
    napcat_up = S.port_open(6099)
    if not daemon_up and not napcat_up:
        with _wd_lock:  # 用户主动停止的全部组件：不算故障
            _wd_state["strikes"] = 0
        return True, "all-stopped"
    healthy, detail = True, ""
    if daemon_up:
        r = proxy_get("127.0.0.1", PORTS["daemon"], "/status",
                      read_const(TOKEN_PATH), timeout=4)
        if not r.get("ok"):
            healthy, detail = False, "daemon /status 异常"
        elif not r.get("ws_connected"):
            healthy, detail = False, "WS 未连接（重连中）"
        elif r.get("ws_alive") is False:  # 老版本 daemon 无此字段时不误报
            healthy, detail = False, "WS 半死（动作通道无响应）"
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
            result = _wd_recover(cfg)
            with _wd_lock:
                _wd_state["strikes"] = 0
                _wd_state["last_result"] = result
            return False, result
        _wd_notify_owner(cfg, "看门狗：检测到机器人推送异常（%s）。当前为仅告警模式，请到控制台手动重启。" % detail)
        with _wd_lock:
            _wd_state["last_result"] = "告警：%s" % detail
    return healthy, detail


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
.b-ok{background:var(--ok-bg);color:var(--ok)}.b-bad{background:var(--err-bg);color:var(--err)}
textarea{width:100%;min-height:260px;background:var(--input);color:var(--fg);border:1px solid var(--line2);border-radius:8px;padding:10px;font-family:Consolas,monospace;font-size:13px;resize:vertical}
textarea:focus,input:focus,select:focus{outline:2px solid var(--line2);border-color:var(--fg-strong)}
input,select{background:var(--input);color:var(--fg);border:1px solid var(--line2);border-radius:7px;padding:6px 10px;font-family:inherit;font-size:13.5px}
input:focus{background:var(--input-focused)}
input[type=checkbox],input[type=radio]{accent-color:var(--btn);width:15px;height:15px;vertical-align:-2px}
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
table.memtbl td:nth-child(4){word-break:break-all;white-space:pre-wrap}
table.memtbl td:nth-child(5){white-space:nowrap}
#toast{position:fixed;right:20px;bottom:20px;background:var(--card);border:1px solid var(--ok);color:var(--fg);padding:11px 18px;border-radius:10px;display:none;box-shadow:var(--shadow);font-size:13.5px;z-index:99}
.mono{font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
.tabular-nums{font-variant-numeric:tabular-nums}
.rmgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}
.rm{border:1px solid var(--line2)!important;border-radius:10px!important;padding:13px 15px!important;cursor:pointer;transition:border-color .15s,background .15s;margin-bottom:0!important}
.rm:hover{background:var(--tint)!important}
.rm.on{border-color:var(--fg-strong)!important;background:var(--tint)!important}
.rm b{font-size:13.5px}
.rm.on b{color:var(--fg-strong)}
.master{display:flex;align-items:flex-start;gap:10px}
.master input[type=checkbox]{width:17px;height:17px;margin-top:2px}
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
  <button data-t="persona">人设</button>
  <button data-t="style">风格卡</button>
  <button data-t="memory">记忆</button>
  <button data-t="stickers">表情包</button>
 <button data-t="devctrl">设备操控</button>
  <button data-t="zcode">ZCode 联动</button>
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
<div id="splash"><div class="mark">Z/</div><div class="nm">ZCode QQ机器人</div><div class="bar"><i></i></div></div>
<script>
const $=s=>document.querySelector(s);
let TAB='status';
const TITLES={status:'状态',runlog:'运行日志',models:'模型管理',groups:'群消息',private:'私聊消息',persona:'人设',style:'风格卡',memory:'记忆',stickers:'表情包',devctrl:'设备操控',zcode:'ZCode 联动',settings:'设置',about:'关于'};
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function head(sub){return '<h2>'+TITLES[TAB]+'</h2>'+(sub?'<div class="sub">'+sub+'</div>':'')}
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
 m.querySelector('.dlg-body').textContent=body||'';
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
async function api(p,opts){const r=await fetch(p,opts);return r.json()}
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
async function load(){try{if(TAB==='status')await vStatus();else if(TAB==='runlog')await vRunlog();else if(TAB==='models')await vModels();else if(TAB==='groups')await vGroups();else if(TAB==='private')await vPrivate();else if(TAB==='persona')await vPersonas();else if(TAB==='style')await vFile('style');else if(TAB==='memory')await vMemory();else if(TAB==='stickers')await vStickers();else if(TAB==='devctrl')await vDevCtrl();else if(TAB==='zcode')await vZcode();else if(TAB==='settings')await vSettings();else if(TAB==='about')await vAbout();}catch(e){$('#view').innerHTML='<div class="card">加载失败: '+esc(e)+'</div>'}}
document.querySelectorAll('#nav button').forEach(b=>b.onclick=async()=>{if(!await dirtyGuard())return;document.querySelectorAll('#nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');TAB=b.dataset.t;if(TAB!=='runlog'&&_runlogTimer){clearInterval(_runlogTimer);_runlogTimer=null}if(TAB!=='devctrl'&&_devCodeTimer){clearInterval(_devCodeTimer);_devCodeTimer=null}skeleton();load()});
const _urlTab=new URLSearchParams(location.search).get('tab');
if(_urlTab&&TITLES[_urlTab]){TAB=_urlTab;document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on',x.dataset.t===_urlTab));}
/* 启动闪屏：每次新窗口首播，UI 自愈刷新（reload）不重播 */
(function(){
 const s=document.getElementById('splash');if(!s)return;
 if(sessionStorage.getItem('splashed')){s.remove();return}
 sessionStorage.setItem('splashed','1');
 setTimeout(()=>{s.classList.add('off');setTimeout(()=>s.remove(),380)},980)
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
 return '<div class="card" id="bootcard"><div class="k" style="display:flex;gap:10px;align-items:center;margin-bottom:6px">'
  +'启动任务 '+badge+(b.final?'<span class="k">'+esc(b.final)+'</span>':'')
  +(b.running?'':' <button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="$(\'#bootcard\').remove()">收起</button>')
  +'</div>'+(rows||'<div class="k">等待第一步…</div>')+'</div>';
}
async function refreshBootCard(){
 try{
  const b=await api('/api/power/status');
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
 const [d,a,b,c,wd]=await Promise.all([
  api('/api/daemon-status').catch(()=>({ok:false})),
  api('/api/agent-status').catch(()=>({ok:false})),
  api('/api/power/status').catch(()=>({})),
  api('/api/app-config').catch(()=>({})),
  api('/api/watchdog').catch(()=>({enabled:false})),
 ]);
 manageBootTimer(b);
 const ws=d.ws_connected, login=d.login||{};
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
 $('#view').innerHTML=head('组件运行状态与机器人开关')+
 ` <div class="card"><div class="kv">
  <div><span class="k">消息管道</span> <span class="badge ${d.ok?'b-ok':'b-bad'}">${d.ok?'运行':'停止'}</span></div>
  <div><span class="k">NapCat WS</span> <span class="badge ${ws?'b-ok':'b-bad'}">${ws?'已连接':'断开'}</span></div>
  <div><span class="k">自治大脑</span> <span class="badge ${a&&a.ok?'b-ok':'b-bad'}">${a&&a.ok?'运行':'停止'}</span></div>
  <div><span class="k">机器人</span> ${esc(login.nickname||'?')} <span class="mono">(${esc(String(login.user_id||'?'))})</span></div>
 </div>
 <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
  <button class="act" onclick="powerOn()" ${booting?'disabled style="opacity:.55"':''}>${booting?'启动中…（进度见下方日志）':'启动机器人（幂等防重复）'}</button>
  <button class="act ghost" onclick="powerOff(false)">停止大脑与管道（保留登录）</button>
  <button class="act ghost danger" onclick="powerOff(true)">全部停止（含 NapCat）</button>
 </div></div>
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
  <span style="margin-left:10px">
   <input type="time" value="${esc(c.sched_pause_start||'23:00')}" onchange="saveSchedPause('sched_pause_start',this.value)"> –
   <input type="time" value="${esc(c.sched_pause_end||'07:00')}" onchange="saveSchedPause('sched_pause_end',this.value)">
  </span>
  <div class="k" style="margin-top:4px">时段内机器人完全停止一切活动（零消耗，同手动暂停），时段结束自动恢复。支持跨午夜（如 23:00–07:00）。恢复后不补发暂停期间消息。与安静模式/分群暂停/仅私聊模式不同：这是全局一刀切。</div>
 </div></div>
 ${wdCard}
 <div class="card"><div class="k">收发统计</div><div class="kv" style="margin-top:8px">
  <div>收到 ${d.stats?.received??0}</div><div>触发 ${d.stats?.triggered??0}</div>
  <div>已发 ${d.stats?.sent??0}</div><div>限流丢弃 ${d.stats?.dropped_rate??0}</div>
 </div></div>`;
 $('#headline').textContent=(d.ok&&a&&a.ok)?'运行中':'已停止';
}
async function powerOn(){
 const r=await api('/api/power/on',{method:'POST'}).catch(()=>({started:false,note:'请求失败'}));
 if(r.started===false){toast(r.note||'启动任务已在进行中，见状态页启动日志')}
 else{toast('启动任务已开始：每一步进度在状态页实时显示')}
 load();
}
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
const _srcName={daemon:'管道',agent:'大脑',boot:'启动'};
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
 $('#view').innerHTML=head('机器人运行全日志：消息管道（daemon）、自治大脑（agent）、启动任务实时汇聚')+
 '<div class="card"><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px">'
 +chip('all','全部')+chip('daemon','消息管道')+chip('agent','自治大脑')+chip('boot','启动任务')
 +'<span class="k" style="margin-left:auto">3 秒自动刷新·跟随滚动</span></div>'
 +'<div style="max-height:calc(100vh - 210px);overflow-y:auto" id="logbox">'+(runlogRows(d)||'<div class="k" style="padding:20px 0">暂无日志</div>')+'</div></div>';
 const box=$('#logbox');if(box)box.scrollTop=box.scrollHeight;
 if(!_runlogTimer)_runlogTimer=setInterval(refreshRunlog,3000);
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
  const cd=a.cooldowns&&a.cooldowns[ck]>0;
  return `<div class="row ${cd?'cool':''}"><span class="no">${i+1}.</span>
  <b style="flex:0 0 auto;max-width:46%;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.model)}</b>
  <span class="k mono" style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right">${esc(keyLabel(e))}${e.ctx?' · '+esc(e.ctx)+'k':''}${e.maxout?' · out '+esc(e.maxout):''} · ${thName[v]||'默认'}</span>
  <span class="mono" style="flex:0 0 auto">${cd?('冷却'+a.cooldowns[ck]+'s'):(a.current_model===e.model?'当前':'')}</span>
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
 <div class="card"><div class="k">模型降级链（自上而下尝试；429/失效自动冷却切下一个；点「编辑」修改模型名 / API Key / 上下文档位 / 思考模式）</div>
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
function chainOf(entries){return entries.map(e=>(e.key||e.ctx||e.maxout)?{model:e.model,...(e.key?{key:e.key}:{}),...(e.ctx?{ctx:e.ctx}:{}),...(e.maxout?{max_out:e.maxout}:{})}:e.model)}
async function editModel(i){
 const creating=i<0;const e=creating?{model:'',key:'',ctx:'',think:'',maxout:''}:Object.assign({},window._mEntries[i]);
 const keysL=window._mKeys||[];
 const keyOpts='<option value=""'+(!e.key?' selected':'')+'>主 Key（智谱）</option>'
  +keysL.map(k=>`<option value="${esc(k.id)}"${e.key===k.id?' selected':''}>${k.primary?'★ ':''}${esc(k.label||k.id)}（${esc(k.provider_label||'智谱')}）</option>`).join('');
 const thOpts=['','off','high','low'].map(v=>'<option value="'+v+'"'+((e.think||'')===v?' selected':'')+'>'+({'' :'跟随默认','off':'关闭思考','high':'思考·高','low':'思考·低'})[v]+'</option>').join('');
 const row=(label,inner,hint)=>'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px">'+label+'</div>'+inner+(hint?'<div class="k" style="margin-top:3px">'+hint+'</div>':'')+'</div>';
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:440px;max-height:86vh;overflow-y:auto"><h3>'+(creating?'添加模型':'编辑模型')+'</h3>'
  +row('模型名','<input id="em_model" value="'+esc(e.model)+'" placeholder="如 glm-5.3-flash" style="width:100%">')
  +row('API Key','<select id="em_key" class="mini" style="width:100%">'+keyOpts+'</select>','该条目调用时使用的智谱账号')
  +row('上下文档位（千 token）','<input id="em_ctx" type="number" min="4" max="128" value="'+(e.ctx||'')+'" placeholder="按模型默认（flash 系 128）" style="width:100%">','控制注入的对话历史与记忆规模；人设/风格/安全规则永不裁剪')
  +row('思考模式','<select id="em_th" class="mini" style="width:100%">'+thOpts+'</select>')
  +row('最大输出（tokens）','<input id="em_maxout" type="number" min="256" max="32768" step="256" value="'+(e.maxout||'')+'" placeholder="默认（按思考档 600–4096）" style="width:100%">','单次回复的输出上限；开思考时会自动放大以留出思考空间')
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
 const on=g.enabled!==false, allOn=!!g.allow_all, m=c.reply_mode||'standard';
 const rows=(g.groups||[]).map((x,i)=>{
  const ov=_gmModes[x]||{};
  const cur=ov.paused?'paused':(ov.reply_mode||'');
  const opt=(v,t)=>`<option value="${v}"${cur===v?' selected':''}>${t}</option>`;
  const pcur=ov.paused?'x':(ov.proactive===true?'on':(ov.proactive===false?'off':''));
  const opt2=(v,t)=>`<option value="${v}"${pcur===v?' selected':''}>${t}</option>`;
  let params='';
  if(ov.reply_mode==='keyword'){
   params=`<div class="gm-sub">该群关键词（逗号分隔，覆盖全局；回车保存）：<input value="${esc((ov.keywords||_gmGlobal.keywords||[]).join(', '))}" style="width:230px" onchange="saveGmFieldFor('${esc(x)}','keywords',this)"></div>`;
  }else if(ov.reply_mode==='chat'){
   params=`<div class="gm-sub">插话概率 <input type="number" min="0" max="100" style="width:60px" value="${Math.round((ov.chat_probability??_gmGlobal.chat_probability??0.5)*100)}" onchange="saveGmFieldFor('${esc(x)}','chat_probability',this)">%
   　最小间隔 <input type="number" min="0" max="3600" style="width:68px" value="${ov.chat_min_interval_sec??_gmGlobal.chat_min_interval_sec??45}" onchange="saveGmFieldFor('${esc(x)}','chat_min_interval_sec',this)"> 秒（仅该群生效）</div>`;
  }
  if(ov.proactive===true){
   params+=`<div class="gm-sub">该群主动：概率 <input type="number" min="0" max="100" style="width:56px" value="${Math.round((ov.proactive_probability??rv.proactive_groups_probability??0.1)*100)}" onchange="saveGmProNum('${esc(x)}','proactive_probability',(parseInt(this.value)||0)/100)">%
   　频率 每 <input type="number" min="5" max="1440" style="width:60px" value="${Math.round((ov.proactive_min_interval_sec??rv.proactive_groups_min_interval_sec??1800)/60)}" onchange="saveGmProNum('${esc(x)}','proactive_min_interval_sec',(parseInt(this.value)||30)*60)"> 分钟（仅该群；未修改时跟随全局）</div>`;
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
  ${m==='keyword'?`<div style="margin-top:12px"><div class="k">关键词（逗号分隔，包含即触发；改完按回车或点别处自动保存）</div>
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
  <div style="margin-top:10px;display:flex;gap:8px"><input id="newg" placeholder="群号，如 661206749" onkeydown="if(event.key==='Enter')addg()"><button class="act" onclick="addg()">添加</button></div></div>
 </div>`;
}
async function vPrivate(){
 const [c,rv]=await Promise.all([
  api('/api/private-config').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
 ]);
 const on=c.enabled, all=c.allow_all;
 rvState=rv;
 const rows=(c.qq_whitelist||[]).map((x,i)=>`<div class="row"><span class="no">${i+1}.</span>
  <input value="${esc(x)}" data-i="${i}" style="flex:1" placeholder="QQ号" onchange="savePrivate()">
  <button class="act ghost" onclick="delpq(${i})">删除</button></div>`).join('');
 $('#view').innerHTML=head('私聊回应开关与私信白名单（私聊始终全部回复，无需回复模式）')+saveHint(_SAVE_HINT)+
 `<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">主动发消息<span class="badge" style="background:var(--tint);color:var(--dim)">全局通用</span>（对所有可私聊对象生效；与群聊设置互不影响）</div>
  <label class="master"><input type="checkbox" ${rv.proactive_private_enabled?'checked':''} onchange="savePro('proactive_private_enabled',this.checked)">
   <span><b>允许机器人主动私聊</b><div class="k" style="margin-top:3px">只发给私信白名单内、最近 6 小时聊过的人（与「回应私信」开关互不影响，回复所有人的私信开启时也不会主动打扰陌生人）；安静模式/暂停/限额时都不发</div></span></label>
  ${proCard('private')}
 </div> <div class="card">
  <label class="master" style="margin-bottom:8px"><input type="checkbox" id="penabled" ${on?'checked':''} onchange="savePrivate()">
   <span><b>回应私信</b><div class="k" style="margin-top:3px">默认关闭；开启后机器人会回复私聊消息</div></span></label>
  <label class="master"><input type="checkbox" id="pallowall" ${all?'checked':''} onchange="savePrivate()">
   <span>回复所有人的私信<div class="k" style="margin-top:3px">关闭后仅回复下方白名单中的 QQ 号</div></span></label>
 </div>
 <div class="card" style="${all?'opacity:.45':''}"><div class="k">私信白名单（仅回复列表内 QQ 号的私信，改动即时生效${all?'；当前被上面的"回复所有人"覆盖，列表保留不丢':''}）</div>
  <div class="chain" style="margin-top:10px">${rows||'<i class="k">空（未配置任何 QQ，将无人可私聊触发）</i>'}</div>
  <div style="margin-top:10px;display:flex;gap:8px"><input id="newpq" placeholder="QQ号，如 3349525227" onkeydown="if(event.key==='Enter')addpq()"><button class="act" onclick="addpq()">添加</button></div></div>`;
}
function getPQ(){return [...document.querySelectorAll('.chain input')].map(x=>x.value.trim()).filter(x=>/^\d{5,12}$/.test(x))}
async function savePrivate(){
 const body={enabled:$('#penabled').checked,allow_all:$('#pallowall').checked,qq_whitelist:getPQ()};
 const r=await api('/api/private-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 toast(r.ok?'私聊白名单已生效（'+getPQ().length+' 个 QQ）':'保存失败: '+(r.error||''),r.ok!==false)
}
async function delpq(i){const c=getPQ();const q=c[i];c.splice(i,1);
 if(!await dlgConfirm({title:'移除私聊白名单',body:'移除 QQ '+q+'？机器人将不再回复该账号的私信（可随时重新添加）。',okText:'移除',danger:true}))return;
 postPQ(c)}
async function postPQ(c){const a=await api('/api/private-config');await api('/api/private-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:a.enabled,allow_all:a.allow_all,qq_whitelist:c})});load()}
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
 const d=await api('/api/personas');
 const opts=(d.names||[]).map(n=>`<option value="${esc(n)}" ${n===d.active?'selected':''}>${esc(n)}</option>`).join('');
 $('#view').innerHTML=head('多人设管理与全局通用设定')+saveHint('长文本需点所在卡片的「保存」按钮生效（按钮只保存该卡片）；未保存的修改以蓝色边框标记，切换人设或离开本页会提示。')+
 `<div class="card"><div class="k">全局通用设定（所有人设共用：主人是谁、通用禁忌等，跨人设生效）</div>
  <textarea id="gedit" style="min-height:150px;margin-top:8px" oninput="dirtyOn(this,'bsaveg')">${esc(d.global||'')}</textarea>
  <button class="act" id="bsaveg" disabled onclick="saveGlobal()">保存全局设定</button></div>
 <div class="card"><div class="k">人设列表（当前激活：<b style="color:var(--fg-strong)">${esc(d.active||'无')}</b>，切换即时生效）</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <select id="psel">${opts}</select>
   <button class="act ghost" onclick="switchPersona()">切换到此设</button>
   <button class="act ghost" onclick="renamePersona()">重命名</button>
   <button class="act ghost" onclick="newPersona()">新建</button>
   <button class="act ghost danger" onclick="delPersona()">删除</button>
  </div>
  <div class="k" style="margin-top:10px">人设内容（<span id="pname">${esc(d.active||'')}</span>）</div>
  <textarea id="pedit" style="margin-top:6px" oninput="dirtyOn(this,'bsavep')">${esc(d.text||'')}</textarea>
  <button class="act" id="bsavep" disabled onclick="savePersona()">保存当前编辑的人设</button></div>`;
 const sel=$('#psel');_pSelPrev=sel.value;
 sel.onchange=async()=>{
  if($('#pedit').classList.contains('dirty')&&!await dlgConfirm({title:'未保存的修改',body:'当前人设有未保存的修改，放弃并切换？',okText:'放弃并切换',danger:true})){sel.value=_pSelPrev;return}
  _pSelPrev=sel.value;
  const r=await api('/api/personas/detail?name='+encodeURIComponent(sel.value));
  $('#pedit').value=r.text||'';$('#pname').textContent=sel.value;
  $('#pedit').classList.remove('dirty');$('#bsavep').disabled=true;
 };
}
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
async function vFile(kind){
 const d=await api('/api/file?kind='+kind);
 const ev=await api('/api/evolved');
 let evHtml='';
 if(kind==='style'){
  const rows=(ev.personas||[]).map(p=>{
   const notesHtml=p.notes&&p.notes.length
    ?'<div class="k" style="margin-top:8px">'+p.notes.map(n=>'- '+esc(n)).join('<br>')+'</div>'
    :'<div class="k" style="margin-top:6px">（尚无积累：机器人每 6 次回复会自动从群聊中提炼 1-2 条风格观察）</div>';
   const clearBtn=p.notes&&p.notes.length
    ?'<button class="act ghost" style="color:var(--err)" data-pname="'+esc(p.name)+'" onclick="clearEvolved(this.dataset.pname)">清空</button>'
    :'';
   return '<div class="card">'
    +'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
    +'<b>'+esc(p.name)+'</b>'+(p.active?'<span class="badge b-ok">当前人设</span>':'')
    +'<label><input type="checkbox" '+(p.enabled?'checked':'')+' data-pname="'+esc(p.name)+'" onchange="toggleEvolved(this.dataset.pname,this.checked)"> 启用自进化</label>'
    +'<span class="k mono">'+(p.notes?p.notes.length:0)+' 条观察</span>'+clearBtn
    +'</div>'+notesHtml+'</div>';
  }).join('');
  evHtml='<div class="card"><div class="k">自进化风格（绑定人设：机器人聊天中自动积累表达经验，注入到对应人设的提示词；可随时开关/清空，与人设卡叠加生效）</div>'
   +'<div style="margin-top:10px">'+(rows||'<i class="k">暂无人设</i>')+'</div></div>';
 }
 $('#view').innerHTML=head('说话风格卡（模仿目标语言风格，留空=纯人设；与自进化叠加生效）')+saveHint('风格卡内容需点「保存」按钮生效（只保存本卡片）；未保存的修改以蓝色边框标记。')+evHtml+
 `<div class="card"><div class="k">${kind==='style'?'风格卡内容':'文件内容'}</div>
  <textarea id="fedit" style="margin-top:10px" oninput="dirtyOn(this,'bsavef')">${esc(d.text||'')}</textarea>
  <div><button class="act" id="bsavef" data-kind="${kind}" disabled onclick="saveFile(this.dataset.kind)">保存${kind==='style'?'风格卡':'文件内容'}</button></div></div>`;
}
async function toggleEvolved(name,on){await api('/api/evolved/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,enabled:on})});toast('「'+name+'」自进化已'+(on?'启用':'停用')+'（已生效）');load()}
async function clearEvolved(name){if(!await dlgConfirm({title:'清空风格观察',body:'清空「'+name+'」的全部风格观察？',okText:'清空',danger:true}))return;await api('/api/evolved/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});toast('已清空');load()}
async function saveFile(kind){const t=$('#fedit').value;const r=await saveKV('/api/file?kind='+kind,{text:t},'已保存（agent 下一轮生效）');if(r.ok!==false)dirtyOff('bsavef')}
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
  <div style="margin-top:8px"><div class="k" style="margin-bottom:4px">触发关键词（逗号分隔；两种模式下都会先匹配）</div>
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
   <div><div class="k" style="margin-bottom:4px">机器人 QQ 号</div><input id="sbotqq" value="${esc(String(c.bot_qq||''))}" style="width:100%" placeholder="如 3126940548" onchange="saveEnv('sbotqq','bot_qq',0,'机器人 QQ 已保存（已生效）')"></div>
   <div><div class="k" style="margin-bottom:4px">主人 QQ 号（任务/遥控白名单）</div><input id="sownerqq" value="${esc(String(c.owner_qq||''))}" style="width:100%" placeholder="如 3349525227" onchange="saveEnv('sownerqq','owner_qq',0,'主人 QQ 已保存（已生效）')"></div>
  </div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
   <label class="master" style="margin-bottom:8px"><input type="checkbox" ${c.bot_name_enabled?'checked':''} onchange="saveKVLoad('/api/app-config',{bot_name_enabled:this.checked?1:0},this.checked?'已开启固定自称':'已关闭：跟随人设卡名字')">
    <span><b>固定自称</b><div class="k" style="margin-top:3px">开启后机器人一律用下面的名字自称，优先级最高（人设卡的名字仅作管理备注）；关闭则跟随当前人设卡</div></span></label>
   <div style="display:flex;gap:8px;align-items:center"><span class="k" style="flex:none">如何称呼自己</span>
    <input id="sbotname" value="${esc(String(c.bot_name_value||'智谱娘'))}" style="width:160px" onchange="saveKV('/api/app-config',{bot_name_value:this.value.trim()||'智谱娘'},'自称已保存（即时生效）')"></div>
  </div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
   <label class="master" style="margin-bottom:8px"><input type="checkbox" ${c.owner_name_enabled?'checked':''} onchange="saveKVLoad('/api/app-config',{owner_name_enabled:this.checked?1:0},this.checked?'已开启固定称呼':'已关闭：按人设/默认叫法')">
    <span><b>固定对你的称呼</b><div class="k" style="margin-top:3px">开启后机器人一律用下面的称呼叫你（所有场景，含设备操控话术）；关闭则按当前人设的叫法/消息昵称</div></span></label>
   <div style="display:flex;gap:8px;align-items:center"><span class="k" style="flex:none">如何称呼你</span>
    <input id="sownername" value="${esc(String(c.owner_name_value||'主人'))}" style="width:160px" onchange="saveKV('/api/app-config',{owner_name_value:this.value.trim()||'主人'},'称呼已保存（即时生效）')"></div>
  </div></div>
 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">② 运行环境</div>
 <div class="card"><div class="k">安装位置与网络（端口一般无需改动，改动重启各组件后生效）</div>
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">QQNT 安装目录（留空=自动探测，探测到：${esc(c.detected_qqnt||'未探测到')}）</div>
  <input id="sqqnt" value="${esc(String(c.qqnt_dir||''))}" style="width:100%" onchange="saveEnv('sqqnt','qqnt_dir',0,'QQNT 目录已保存（下次启动任务时生效）')"></div>
  <div class="k" style="margin-top:12px">端口（一般无需改动；改动重启各组件后生效）</div>
  <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;align-items:center">
   <label>NapCat WS <input id="pnap" type="number" value="${c.napcat_ws_port}" style="width:76px" onchange="saveEnv('pnap','napcat_ws_port',1)"></label>
   <label>daemon <input id="pdae" type="number" value="${c.daemon_port}" style="width:76px" onchange="saveEnv('pdae','daemon_port',1)"></label>
   <label>agent <input id="pagn" type="number" value="${c.agent_admin_port}" style="width:76px" onchange="saveEnv('pagn','agent_admin_port',1)"></label>
   <label>管理台 <input id="pui" type="number" value="${c.ui_port}" style="width:76px" onchange="saveEnv('pui','ui_port',1)"></label>
  </div>
  <div style="display:flex;gap:10px;margin-top:8px;flex-wrap:wrap;align-items:center">
   <label>时区 <input id="ptz" value="${esc(c.timezone||'Asia/Shanghai')}" style="width:180px" placeholder="如 Asia/Shanghai" onchange="saveEnv('ptz','timezone',0,'时区已保存（机器人感知的当前时间随此变化）')">（IANA 时区名，影响 AI 对时间的感知）</label>
  </div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">③ 接口与模型消耗</div>
 <div class="card"><div class="k">API Key（智谱开放平台获取；多个 Key 时选一个「主」——生图/联网搜索/模型链未指定 Key 的条目都用主 Key；模型链每行可在「模型管理」单独指定 Key）</div>
  <div style="margin-top:10px">${keysL.map(k=>`<div class="row">
   <label style="margin:0;flex:none;white-space:nowrap"><input type="radio" name="primkey" ${k.primary?'checked':''} onchange="setPrimary('${esc(k.id)}')"> 主</label>
   <span class="badge" style="flex:none;background:var(--tint);color:var(--dim)">${esc(k.provider_label||'智谱')}</span>
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
  <div class="k" style="margin-top:6px">预制供应商（OpenAI 兼容）：智谱、DeepSeek（deepseek-chat / deepseek-reasoner）、Kimi（moonshot-v1-8k…）、OpenAI（gpt-4o 等）、Grok（grok-2 等）、Gemini（gemini-2.0-flash 等）、自定义（填 Base URL，禁止内网地址）。智谱/DeepSeek/Kimi 国内直连；OpenAI/Grok/Gemini 需在下方配置 API 代理后使用。主 Key 与生图/联网搜索固定用智谱 Key；模型链每行可绑定任意 Key。Key 仅存本机数据目录，列表仅显示掩码。</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <span class="k" style="flex:none">API 代理（可选）</span>
   <input id="apiproxy" value="${esc(String(c.api_proxy||''))}" style="width:240px" placeholder="http://127.0.0.1:7890（Clash 等本地代理）" onchange="saveKV('/api/app-config',{api_proxy:this.value.trim()},'API 代理已保存（即时生效；留空=直连）')">
   <span class="k">供 OpenAI / Grok / Gemini 等境外供应商出站用；国内供应商建议留空直连</span></div></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.lean_ctx_enabled!==false?'checked':''} onchange="saveLeanCtx(this.checked)">
   <span><b>精简上下文（推荐开启）</b><div class="k" style="margin-top:3px">开启后：旁观消息先经约 0.5k token 的轻量判定决定是否开口（决定开口才用全量提示词生成），回复时按各模型的上下文档位裁剪历史与记忆——人设/风格/安全规则始终全量，回复质量不受影响。关闭后：每次调用都带完整系统提示与更多历史记忆（追求最佳表现，token 消耗大约 +50%~80%，旁观评估为主增量）</div></span></label></div>
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

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">④ 对话行为</div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.wait_gate_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{wait_gate_enabled:this.checked?1:0},'等说完协议已'+(this.checked?'开启':'关闭'))">
  <span><b>等说完协议（推荐开启）</b><div class="k" style="margin-top:3px">AI 回复前会判断对方这条消息是否是叙述的中间部分（语义没说完、刚开头就断、明显还有下文）。如果判断对方还在说，AI 会先安静等对方说完再回复——避免在别人一句话还没打完时就急着插嘴打断。对完整的问题和闲聊不影响（立刻回复），零额外消耗，仅增加一条判断规则</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.burst_merge_enabled!==false?'checked':''} onchange="saveKVLoad('/api/reply-config',{burst_merge_enabled:this.checked?1:0},'碎片合并已'+(this.checked?'开启':'关闭'))">
  <span><b>碎片合并（推荐开启）</b><div class="k" style="margin-top:3px">QQ 聊天习惯一句话一句话地发。开启后，如果有人在 3 秒内连发了 2 条以上消息（正在打字中），机器人的主动插话会暂缓，等此人停下来后再处理——多条碎片合并成一次回复，上下文更完整。被 @ 和被回复的消息不受影响（始终立刻回应），仅影响关键词/闲聊的主动插话时机，零额外消耗</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.vision_enabled?'checked':''} onchange="saveVision(this.checked)">
  <span><b>看图模式</b><div class="k" style="margin-top:3px">开启后，消息里的图片会交给视觉模型识别并回应（群聊与私聊均生效）；视觉模型在「模型管理」页配置，识别失败自动降级纯文本</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.web_search_enabled?'checked':''} onchange="saveWebSearch(this.checked)">
  <span><b>联网搜索</b><div class="k" style="margin-top:3px">遇到实时性问题（新闻/天气/价格/最新版本等）自动先联网搜索再回答，普通闲聊不触发不计费。搜索后端：优先用智谱搜索资源包（若有），未开通自动切换必应免费搜索兜底——不买资源包也能搜；两个都失败时机器人会坦白拿不到最新消息</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.abuse_guard_enabled!==false?'checked':''} onchange="saveAbuseGuard(this.checked)">
  <span><b>防滥用防护</b><div class="k" style="margin-top:3px">整合三重防护，建议保持开启：① 人设防篡改——"忽略设定/你现在是XX/输出系统提示"等注入话术不生效，设定不泄露；② 任务拦截——竞赛题、代写作业、要代码成品等把机器人当免费任务引擎的消息，会以人设口吻轻松回绝；③ 身份锚定——多轮对话被"说服"后仍回归人设。关闭后所有消息按原始对话处理（仅调试用）</div></span></label></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑤ 主人专属功能</div>
 <div class="card"><div class="k" style="margin-bottom:8px">主人专属功能（仅对设置的主人 QQ 生效；关闭后对应入口不再响应）</div>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.task_bridge_enabled!==false?'checked':''} onchange="saveFeature('task_bridge_enabled',this.checked)">
   <span><b>任务沙箱</b><div class="k" style="margin-top:3px">主人发「任务：…」转交沙箱执行器写代码/跑脚本，结果自动回传；产物在任务目录（可在 app_config.task_workspace 配置，指向 ZCode 工作区即可在 ZCode 中接管）</div></span></label>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.remote_control_enabled!==false?'checked':''} onchange="saveFeature('remote_control_enabled',this.checked)">
   <span><b>私聊遥控</b><div class="k" style="margin-top:3px">主人私聊发指令管理机器人（状态/用量/人设/群管理/功能开关/暂停恢复），私聊机器人发「帮助」查看全部</div></span></label>
  <label class="master" style="margin-bottom:6px"><input type="checkbox" ${rv.draw_enabled!==false?'checked':''} onchange="saveFeature('draw_enabled',this.checked)">
   <span><b>AI 生图</b><div class="k" style="margin-top:3px">主人发「画：描述」或自然说法（帮我画一张/给我画个/来一张…，私聊或群里@）→ CogView-3-flash 免费生成并发图</div></span></label>
  <label class="master"><input type="checkbox" ${rv.daily_review_enabled!==false?'checked':''} onchange="saveFeature('daily_review_enabled',this.checked)">
   <span><b>夜间自动复盘</b><div class="k" style="margin-top:3px">每日 03:30 自动统计当日表现、提炼风格观察、日报发主人私聊</div></span></label>
 </div>
 <div class="card"><div class="k">开机自启（登录 Windows 后自动打开控制台窗口${c.autostart_with_windows?'，已开启':'，未开启'}）</div>
  <div style="margin-top:8px"><label><input type="checkbox" id="sauto" ${c.autostart_with_windows||S_autostartRegistry()?'checked':''} onchange="saveAuto(this.checked)"> 开机自动启动本应用</label></div></div>
 <div class="card"><div class="k">启动行为</div>
  <div style="margin-top:8px"><label class="master"><input type="checkbox" id="spoweron" ${c.auto_power_on?'checked':''} onchange="savePowerOn(this.checked)">
   <span>打开本应用时自动启动机器人<div class="k" style="margin-top:2px">与上一项同时勾选时，开机会先启动应用、紧接着自动启动机器人</div></span></label></div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑥ 自动化与维护</div>
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
  <div class="k" style="margin-top:4px">备份范围：人设合集、风格卡、自进化观察、长期记忆、回复/分群/私聊/白名单配置、模型链、表情包索引（含视觉描述缓存）、设备操控流程、定时任务、主动消息状态、用量流水、本页应用设置。密钥类文件（API Key/token）出于安全不备份，恢复后如缺失需重新填写。超过保留天数或保留份数的旧备份自动清理（最旧的先清）</div></div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">QQ 在线状态</div>
  <label class="master"><input type="checkbox" ${c.qq_status_auto?'checked':''} onchange="saveKVLoad('/api/app-config',{qq_status_auto:this.checked?1:0},'QQ 在线状态自动设置已'+(this.checked?'开启':'关闭'))">
   <span><b>自动设置在线状态</b><div class="k" style="margin-top:3px">机器人上线登录成功后自动把 QQ 状态设为「在线」；手动停止机器人（状态页停止/全部停止）前自动设为「离开」，让群里的人一眼看出机器人是否在岗。依赖 NapCat 扩展能力，若你的 NapCat 版本不支持会记录失败到运行日志、不影响其他功能</div></span></label>
  <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <span class="k">手动测试：</span>
   <button class="act ghost" style="margin:0" onclick="setQQStatus('online')">设为在线</button>
   <button class="act ghost" style="margin:0" onclick="setQQStatus('away')">设为离开</button>
   <span class="k">（不管开关是否开启都可手动设置；QQ 客户端上即时可见）</span></div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">NapCat 活性看门狗<span class="badge" style="background:var(--tint);color:var(--dim)">防消息推送静默失联</span></div>
  <label class="master"><input type="checkbox" ${c.watchdog_enabled!==false?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_enabled:this.checked?1:0},'看门狗已'+(this.checked?'开启':'关闭'))">
   <span><b>启用看门狗</b><div class="k" style="margin-top:3px">定期探测 NapCat 消息通道是否真的活着（TCP 连着但对端不响应/不推事件的「半死」状态，控制台显示在线但机器人实际收不到消息）；探测到失联会在「状态」页告警并私聊通知主人</div></span></label>
  <div class="k" style="margin-top:10px">探测到失联后：</div>
  <label style="display:block;margin:4px 0"><input type="radio" name="wdmode" value="auto" ${(c.watchdog_mode||'auto')==='auto'?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_mode:'auto'},'看门狗模式：自动恢复')"> 自动恢复<span class="k">：先由 daemon 自动重连；重连无效则自动重启 NapCat 与组件（需要重新登录时会打开扫码页）</span></label>
  <label style="display:block;margin:4px 0"><input type="radio" name="wdmode" value="alert" ${c.watchdog_mode==='alert'?'checked':''} onchange="saveKVLoad('/api/app-config',{watchdog_mode:'alert'},'看门狗模式：仅告警')"> 仅告警<span class="k">：只通知，不做任何重启，等主人到控制台手动处理</span></label>
  <div style="margin-top:8px"><label>探测间隔 <input type="number" min="60" max="600" step="30" value="${c.watchdog_interval||120}" style="width:70px" onchange="saveKVLoad('/api/app-config',{watchdog_interval:Math.max(60,Math.min(600,parseInt(this.value)||120))},'探测间隔已保存')"> 秒（连续 2 次异常才触发动作）</label></div>
  <div class="k" style="margin-top:6px">当前状态可在「状态」页查看（含最近事件）</div></div>

 <div class="k" style="margin:16px 0 -6px;font-weight:600;letter-spacing:1px">⑦ 应用行为</div>
 <div class="card"><div class="k">关闭本应用时</div>
  <div style="margin-top:8px">
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="tray" ${(!c.close_action||c.close_action==='tray')?'checked':''} onchange="saveCloseAction(this.value)"> 最小化到系统托盘，机器人继续在后台（推荐；托盘图标可重新打开窗口）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_app" ${c.close_action==='exit_app'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用，机器人保持运行（下次双击图标重新打开）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_all" ${c.close_action==='exit_all'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用并停止机器人（关闭时会二次确认）</label>
  </div></div>`;
}
let _autoReg=null;
async function vAbout(){
 $('#view').innerHTML=head('关于本项目')+
 `<div class="card" style="text-align:center;padding:26px 18px">
   <div class="brand-mark" style="width:44px;height:44px;font-size:22px;margin:0 auto 10px;display:flex;align-items:center;justify-content:center;border-radius:10px"></div>
   <div style="font-size:18px;font-weight:600">ZCode QQ机器人</div>
   <div class="k" style="margin-top:6px">一只住在你电脑里、跑在 ZCode 体系上的 QQ 机器人：多人设聊天、长期记忆、表情包、看图生图、任务沙箱、设备操控、主动消息，样样都行。</div>
   <div class="k mono" style="margin-top:10px">版本 0.1.0 · Windows · Python 3</div>
  </div>
 <div class="card"><div class="k" style="margin-bottom:8px">项目信息</div>
  <div class="kv">
   <div><span class="k">GitHub 仓库</span><br><a href="https://github.com/Pigeon2333/zcode-qq-bot" target="_blank" style="word-break:break-all">github.com/Pigeon2333/zcode-qq-bot</a><div class="k">源码、文档与问题反馈都在这里（私有仓库，公开后可提交 Issue）</div></div>
   <div><span class="k">作者</span><br><b>从不咕咕的鸽纸</b><div class="k">QQ 3349525227 · 邮箱 <a href="mailto:ed@sodayo.net">ed@sodayo.net</a></div></div>
   <div><span class="k">B站主页</span><br><a href="https://space.bilibili.com/280664902" target="_blank">space.bilibili.com/280664902</a></div>
   <div><span class="k">GitHub 主页</span><br><a href="https://github.com/Pigeon2333" target="_blank">github.com/Pigeon2333</a></div>
  </div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">贡献者</div>
  <div class="k">暂无——欢迎通过 Pull Request 参与，你的名字会出现在这里。</div></div>
 <div class="card"><div class="k" style="margin-bottom:8px">更新</div>
  <div class="k">当前版本 0.1.0。自动检查更新功能开发中；发布新版本后这里会提供「检查更新」按钮与更新日志。</div>
  <button class="act ghost" style="margin-top:8px;opacity:.55;cursor:default" disabled>检查更新（即将上线）</button></div>
 <div class="card"><div class="k">致谢</div>
  <div class="k" style="margin-top:6px">基于智谱 GLM 系列模型与 NapCat（OneBot 11）构建；感谢所有测试群里陪她聊天、帮她变得更好的朋友们。</div></div>`;
}
async function S_autostartRegistry(){if(_autoReg===null){try{const r=await api('/api/autostart');_autoReg=!!r.enabled;}catch(e){_autoReg=false;}}return _autoReg}
async function saveEnv(id,key,port,msg){
 const v=$('#'+id).value.trim();
 await saveKV('/api/app-config',{[key]:port?+v:v},msg||(port?'端口已保存（重启各组件后生效）':null))}
async function saveAuto(on){const r=await api('/api/autostart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable:on})});_autoReg=on;toast(r.message||'已更新')}
async function savePowerOn(on){await saveKV('/api/app-config',{auto_power_on:on},on?'已开启：打开应用即自动启动机器人（已生效）':'已关闭：需在「状态」页手动启动（已生效）')}
async function saveCloseAction(v){await saveKV('/api/app-config',{close_action:v},'关闭行为已保存（已生效）')}
const _MODE_NAME={standard:'标准',keyword:'关键词',chat:'闲聊'};
function pickMode(v){api('/api/reply-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reply_mode:v})}).then(()=>{toast('已切换到'+_MODE_NAME[v]+'模式（已生效）');load()})}
async function saveKw(){const kw=$('#kwedit').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);await saveKV('/api/reply-config',{keywords:kw},'关键词已生效（共 '+kw.length+' 个）')}
async function saveProb(){await saveKV('/api/reply-config',{chat_probability:(parseInt($('#pslide').value)||0)/100},'插话概率已生效：'+$('#pslide').value+'%')}
async function saveIntv(){await saveKV('/api/reply-config',{chat_min_interval_sec:parseInt($('#mintv').value)||0},'最小间隔已生效：'+($('#mintv').value||0)+' 秒')}
async function saveCk(key,on,name){await saveKV('/api/reply-config',{[key]:on},name+(on?'已开启':'已关闭')+'（已生效）')}
async function saveVision(on){await saveKV('/api/reply-config',{vision_enabled:on},on?'看图模式已开启（已生效）':'看图模式已关闭（已生效）')}
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
  <label>频率：每 <input type="number" min="5" max="1440" value="${iv}" style="width:60px" onchange="saveProNum('${kind}_min_interval_sec',(parseInt(this.value)||30)*60,'分钟')"> 分钟评估一次（5–1440 分钟）</label>
  <label>命中概率 <input type="range" min="0" max="100" value="${prob}" style="width:150px" oninput="this.nextElementSibling.textContent=this.value+'%'" onchange="saveProNum('${kind}_probability',(parseInt(this.value)||0)/100,'概率')"> <b>${prob}%</b></label>
  <label><input type="checkbox" ${sOn?'checked':''} onchange="toggleProSilent('${kind}',this.checked)"> 禁用时段</label>
  ${sOn?`<label><input type="time" value="${esc(rvState['proactive_'+kind+'_silent_start']||'22:00')}" onchange="saveProTime('${kind}_silent_start',this.value)"> – <input type="time" value="${esc(rvState['proactive_'+kind+'_silent_end']||'08:00')}" onchange="saveProTime('${kind}_silent_end',this.value)">（此段内不主动发消息，支持跨午夜）</label>`:''}
 </div>`}
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
async function setQQStatus(kind){
 const r=await api('/api/qq-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast('设置失败：'+(r.detail||r.error||''),false);return}
 toast('QQ 状态已设为'+(kind==='online'?'在线':'离开'))}
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
async function vMemory(){
 const d=await api('/api/memory');
 const rows=(d.items||[]).map(r=>`<tr><td class="mono">${esc(r.time)}</td><td>${r.scope==='group'?'群':'人'}</td><td class="mono">${esc(String(r.id))}</td><td>${esc(r.text)}</td>
  <td><button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="editMemo(${r.idx})">编辑</button>
  <button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="delMemo(${r.idx})">删除</button></td></tr>`).join('');
 $('#view').innerHTML=head('长期记忆流水（自治大脑自动写入；改动即时生效，机器人下一轮对话即用新记忆）')+saveHint('每条记忆可单独编辑/删除（删除需确认）；也可手动新增一条（选择群或联系人）。'+`当前展示最近 100 条。`)+
 `<div class="card">
 <table class="memtbl"><tr><th>时间</th><th>类型</th><th>对象</th><th>内容</th><th>操作</th></tr>${rows||'<tr><td colspan=5 class="k">空</td></tr>'}</table>
 <div style="display:flex;gap:8px"><button class="act" onclick="addMemo()">手动新增一条记忆</button>
 <button class="act ghost danger" onclick="clearMem()">清空全部记忆流水</button></div></div>`;
}
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
  +'<div class="dlg-body">给指定群或联系人写入一条记忆，机器人与其对话时会记得。例如：群 661206749 →「周三有小组作业讨论」。</div>'
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
  <input id="sttags" placeholder="标签，逗号分隔" style="width:180px">
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
 const tagsStr=await dlgPrompt({title:'编辑表情 · 标签',body:'标签（逗号分隔，点取消=保持原标签不动）：\n例如：开心,猫猫',value:(it.tags||[]).join(','),okText:'保存'});
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


def collect_runlog(src="all", tail=250):
    """聚合 daemon/agent 日志与启动任务步骤 → 展示记录列表（正序）。"""
    items = []
    if src in ("all", "daemon"):
        items += [_parse_daemon_line(ln) for ln in
                  _tail_lines(os.path.join(C.DATA_DIR, "logs", "daemon.log"), tail)]
    if src in ("all", "agent"):
        items += _parse_agent_lines(
            _tail_lines(os.path.join(C.DATA_DIR, "logs", "agent.log"), tail))
    if src in ("all", "boot"):
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
    """写模块级常量路径文件（本文件所有写入路径均为常量）。"""
    tmp = pathlib.Path(path + ".ui.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), path)


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

    def _html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            return self._html()
        if u.path == "/api/daemon-status":
            return self._reply(200, proxy_get("127.0.0.1", PORTS["daemon"], "/status",
                                              read_const(TOKEN_PATH)))
        if u.path == "/api/agent-status":
            r = proxy_get("127.0.0.1", PORTS["agent"], "/admin/status")
            return self._reply(200, r if r.get("ok") else {"ok": False})
        if u.path == "/api/power/status":
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
        if u.path == "/api/runlog":
            src = q.get("src", "all")
            if src not in ("all", "daemon", "agent", "boot"):
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
                                     "qq_whitelist": data.get("qq_whitelist", [])})
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
            return self._reply(200, {"ok": True, "personas": rows})
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
            return self._reply(200, {"ok": True, **cfg})
        if u.path == "/api/apikey-status":
            key = read_const(API_KEY_PATH)
            mask = (key[:6] + "..." + key[-4:]) if len(key) >= 12 else ("已设置" if key else "")
            return self._reply(200, {"ok": True, "set": bool(key), "mask": mask})
        if u.path == "/api/autostart":
            return self._reply(200, {"ok": True, "enabled": S.autostart_enabled()})
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
            return self._reply(200, {"ok": True,
                                     "reply_mode": data.get("reply_mode", "standard"),
                                     "keywords": data.get("keywords", []),
                                     "chat_probability": data.get("chat_probability", 0.15),
                                     "chat_interesting": data.get("chat_interesting", True),
                                     "chat_min_interval_sec": data.get("chat_min_interval_sec", 45),
                                     "vision_enabled": data.get("vision_enabled", False),
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
                                     "lean_ctx_enabled": data.get("lean_ctx_enabled", True),
                                     "wait_gate_enabled": data.get("wait_gate_enabled", True),
                                     "burst_merge_enabled": data.get("burst_merge_enabled", True),
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
            r = proxy_get("127.0.0.1", PORTS["daemon"],
                          "/group-list" + ("?refresh=1" if q.get("refresh") else ""),
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
                    n = sum(1 for f in C.BACKUP_FILES
                            if (sub / os.path.basename(f)).is_file())
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
            r = proxy_get("127.0.0.1", PORTS["agent"], "/admin/devctrl-code")
            if not r.get("ok"):
                return self._reply(200, {"ok": False, "code": "", "expires_in": 0,
                                         "error": r.get("error", "agent 未运行")})
            return self._reply(200, r)
        return self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        body = self._body()
        if u.path == "/api/pick-dir":
            def _pick():
                try:
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    d = filedialog.askdirectory(title="选择备份目录")
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
        if u.path == "/api/devctrl-code":
            # 刷新 = 让 agent 立刻作废旧码并换新（代理请求）
            r = proxy_get("127.0.0.1", PORTS["agent"], "/admin/devctrl-code?refresh=1")
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
            conn = http.client.HTTPConnection("127.0.0.1", PORTS["daemon"], timeout=12)
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
            for rel in C.BACKUP_FILES:
                src = src_dir / os.path.basename(rel)
                if not src.is_file():
                    continue
                dst = pathlib.Path(os.path.join(DATA_DIR, rel))
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
            wl = body.get("qq_whitelist")
            if not isinstance(wl, list) or len(wl) > 100 or                     not all(isinstance(q, str) and q.strip().isdigit() for q in wl):
                return self._reply(400, {"ok": False, "error": "qq_whitelist 必须是QQ号字符串数组(<=100)"})
            write_const(PRIVATE_CONFIG_PATH, json.dumps(
                {"enabled": bool(body.get("enabled", False)),
                 "allow_all": bool(body.get("allow_all", True)),
                 "qq_whitelist": [q.strip() for q in wl]}, ensure_ascii=False, indent=2))
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
            if "wait_gate_enabled" in body:
                data["wait_gate_enabled"] = bool(body["wait_gate_enabled"])
            if "burst_merge_enabled" in body:
                data["burst_merge_enabled"] = bool(body["burst_merge_enabled"])
            if "proactive_private_enabled" in body:
                data["proactive_private_enabled"] = bool(body["proactive_private_enabled"])
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
            for k in ("review_time", "backup_time"):
                if k in cfg_in:
                    v = str(cfg_in[k]).strip()
                    cleaned[k] = v if len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() else ""
            for k in ("backup_enabled", "sched_pause_enabled", "watchdog_enabled",
                      "qq_status_auto", "zcode_bridge_enabled",
                      "zcode_handoff_enabled", "zcode_return_enabled",
                      "zcode_notify_enabled", "zcode_reports_enabled",
                      "zcode_sticker_enabled", "zcode_watch_enabled",
                      "zcode_cron_enabled", "zcode_context_enabled",
                      "device_ctrl_actions"):
                if k in cfg_in:
                    cleaned[k] = bool(cfg_in[k])
            if "backup_dir" in cfg_in:
                cleaned["backup_dir"] = str(cfg_in["backup_dir"]).strip()[:260]
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
                        _boot_state["final"] = ("存在失败项" if hard_fail else "启动完成")
                except Exception as e:
                    with _boot_lock:
                        _boot_state["done"] = True
                        _boot_state["failed"] = True
                        _boot_state["final"] = "启动异常：%r" % e
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
        if u.path == "/api/pause":
            paused = bool(body.get("paused"))
            cfg = load_json_const(MODEL_CHAIN_PATH, {})
            cfg["paused"] = paused
            write_const(MODEL_CHAIN_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
            try:
                conn = http.client.HTTPConnection("127.0.0.1", PORTS["agent"], timeout=5)
                payload = json.dumps({"paused": paused}).encode("utf-8")
                conn.request("POST", "/admin/pause", body=payload,
                             headers={"Content-Type": "application/json"})
                conn.getresponse().read()
                conn.close()
            except OSError:
                pass
            return self._reply(200, {"ok": True, "paused": paused})
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
                write_const(STYLE_PATH, text)
            else:
                return self._reply(400, {"ok": False, "error": "kind 必须是 persona|style"})
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


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="qq-bot admin UI")
    ap.add_argument("--port", type=int, default=DEFAULT_UI_PORT)
    ap.add_argument("--daemon-port", type=int, default=DEFAULT_DAEMON_PORT)
    ap.add_argument("--agent-port", type=int, default=DEFAULT_AGENT_PORT)
    args = ap.parse_args()
    PORTS["daemon"] = args.daemon_port
    PORTS["agent"] = args.agent_port
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), UiHandler)
    srv.daemon_threads = True
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    print(json.dumps({"admin_ui_ready": True, "url": "http://127.0.0.1:%d" % args.port,
                      "watchdog": True}, ensure_ascii=False), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
