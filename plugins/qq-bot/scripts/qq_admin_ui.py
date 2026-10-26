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
.chain{display:flex;flex-direction:column;gap:6px;max-width:560px}
.chain .row{display:flex;gap:8px;align-items:center;background:var(--tint);border:1px solid transparent;padding:7px 12px;border-radius:8px}
.chain .row .no{color:var(--dim);width:22px;font-size:12.5px}
.chain .row.cool{opacity:.45}
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
  <button data-t="settings">设置</button>
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
const TITLES={status:'状态',runlog:'运行日志',models:'模型管理',groups:'群消息',private:'私聊消息',persona:'人设',style:'风格卡',memory:'记忆',stickers:'表情包',devctrl:'设备操控',settings:'设置'};
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
function saveHint(text){return '<div class="hint-save">'+esc(text)+'</div>'}
const _SAVE_HINT='本页设置均为即时生效：开关/选项点击即保存；输入框改完按回车或点别处自动保存；带「保存」按钮的卡片只保存该卡片内容。';
function dirtyOn(el,btnId){el.classList.add('dirty');const b=document.getElementById(btnId);if(b)b.disabled=false}
function dirtyOff(btnId){const b=document.getElementById(btnId);if(b)b.disabled=true;document.querySelectorAll('textarea.dirty,input.dirty').forEach(x=>x.classList.remove('dirty'))}
async function dirtyGuard(){const d=document.querySelector('textarea.dirty,input.dirty');if(!d)return true;
 return await dlgConfirm({title:'有未保存的修改',body:'当前有未保存的修改（蓝色边框标记），离开将丢失。确定离开？',okText:'放弃修改',danger:true})}
async function load(){try{if(TAB==='status')await vStatus();else if(TAB==='runlog')await vRunlog();else if(TAB==='models')await vModels();else if(TAB==='groups')await vGroups();else if(TAB==='private')await vPrivate();else if(TAB==='persona')await vPersonas();else if(TAB==='style')await vFile('style');else if(TAB==='memory')await vMemory();else if(TAB==='stickers')await vStickers();else if(TAB==='devctrl')await vDevCtrl();else if(TAB==='settings')await vSettings();}catch(e){$('#view').innerHTML='<div class="card">加载失败: '+esc(e)+'</div>'}}
document.querySelectorAll('#nav button').forEach(b=>b.onclick=async()=>{if(!await dirtyGuard())return;document.querySelectorAll('#nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');TAB=b.dataset.t;if(TAB!=='runlog'&&_runlogTimer){clearInterval(_runlogTimer);_runlogTimer=null}skeleton();load()});
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
 const [d,a,b,c]=await Promise.all([
  api('/api/daemon-status').catch(()=>({ok:false})),
  api('/api/agent-status').catch(()=>({ok:false})),
  api('/api/power/status').catch(()=>({})),
  api('/api/app-config').catch(()=>({})),
 ]);
 manageBootTimer(b);
 const ws=d.ws_connected, login=d.login||{};
 const booting=!!(b&&b.running);
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
 await saveKV('/api/app-config',{sched_pause_enabled:on},on?'定时暂停已开启（时段内机器人完全停止）':'定时暂停已关闭');load()}
async function saveSchedPause(key,val){
 if(!val)return;
 await saveKV('/api/app-config',{[key]:val},'定时暂停时间已更新（即时生效）');load()}
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
  <b style="flex:1;min-width:120px;font-weight:500">${esc(e.model)}</b>
  <span class="k mono" style="flex:none">${esc(keyLabel(e))}${e.ctx?' · '+esc(e.ctx)+'k':''}${e.maxout?' · out '+esc(e.maxout):''} · ${thName[v]||'默认'}</span>
  <span class="mono">${cd?('冷却'+a.cooldowns[ck]+'s'):(a.current_model===e.model?'当前':'')}</span>
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
async function toggleDevCtrl(on){
 const box=document.getElementById('devon');
 if(on&&!await dlgConfirm({title:'开启设备操控',body:'⚠ 这是一项高危功能：开启后主人可以从 QQ 远程搜索、读取、发送你电脑上的文件。' + String.fromCharCode(10) + String.fromCharCode(10) + '已内置保护：仅主人可用、结果只发私聊、系统目录+你的保护目录不可触达、默认需动态授权码。' + String.fromCharCode(10) + String.fromCharCode(10) + '确定开启？',okText:'确认开启（我已了解风险）',danger:true})){
  if(box)box.checked=false;  // 取消：回滚勾选状态
  return;
 }
 const r=await saveKV('/api/app-config',{device_ctrl_enabled:on},on?'设备操控已开启':'设备操控已关闭');
 if(r.ok===false){if(box)box.checked=!on;return}  // 保存失败：回滚
 load()}

async function vDevCtrl(){
 const [c,code,fl]=await Promise.all([
  api('/api/app-config').catch(()=>({})),
  api('/api/devctrl-code').catch(()=>({code:'',expires_in:0})),
  api('/api/devctrl-flow').catch(()=>({flow:{}})),
 ]);
 window._flow=(fl.flow||{});
 const on=!!c.device_ctrl_enabled;
 window._pdDirs=(c.device_ctrl_protected_dirs||[]).slice();
 $('#view').innerHTML=head('设备操控（高危功能：让机器人搜索/读取/发送你电脑上的文件）')+saveHint('仅限主人 QQ · 结果只发私聊 · 保护目录不可触达 · 授权码一次一换')+
 `<div class="card" style="border:1px solid ${on?'var(--err)':'var(--border)'}">
  <label class="master" style="margin-bottom:10px"><input type="checkbox" id="devon" ${on?'checked':''} onchange="toggleDevCtrl(this.checked)">
   <span><b>启用设备操控</b><div class="k" style="margin-top:3px">⚠ 开启后主人可从 QQ 远程搜索/读取/发送电脑文件。默认关闭，开启需二次确认。</div></span></label>

  <div style="margin-top:14px;padding:14px;border:2px solid ${on?'var(--err)':'var(--line)'};border-radius:10px;text-align:center">
   <div class="k" style="margin-bottom:6px">当前动态授权码（使用后自动换新）</div>
   <div id="devcode" style="font-size:36px;font-weight:700;letter-spacing:8px;font-family:monospace;color:${on?'var(--err)':'var(--dim)'}">${on?esc(code.code||'------'):'------'}</div>
   <div class="k" style="margin-top:6px">${on?'有效期剩 '+Math.max(0,code.expires_in||0)+' 秒 · 用完即换新 · 也可手动刷新':'功能未启用'}</div>
   ${on?'<button class="act ghost" style="margin-top:8px" onclick="refreshDevCode()">刷新授权码</button>':''}
  </div>

  <div class="k" style="margin-top:14px;margin-bottom:6px">使用流程</div>
  <div class="k" style="line-height:1.8">
   ① 主人在 QQ 里表达要操作电脑的意图（如「去电脑上找个文档」「设备操控」）<br>
   ② 机器人回复「协议一：连线主人」，等待授权码<br>
   ③ 主人把上方显示的 6 位授权码发给机器人<br>
   ④ 验证通过后 2 分钟内可直接发命令：找文件 / 读文件 / 发文件（结果只发私聊）
  </div>

  <div class="k" style="margin-top:14px;margin-bottom:6px">安全设置</div>
  <label class="master" style="margin-bottom:8px"><input type="checkbox" ${c.device_ctrl_2fa!==false?'checked':''} onchange="saveKV('/api/app-config',{device_ctrl_2fa:this.checked?1:0},'动态授权码已'+(this.checked?'开启（推荐）':'关闭——极危险'));load()">
   <span><b>动态授权码（2FA）</b><div class="k" style="margin-top:3px">每次操控需授权码验证，一次用完自动换新；关闭后只需主人 QQ 即可操控——极不推荐</div></span></label>

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
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">申请授权码文本（可用占位符 {owner} 主人昵称 / {bot} 机器人自称）</div>
  <textarea id="flask" style="min-height:80px">${esc(window._flow.ask_text||'')}</textarea></div>
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">验证成功文本（占位符 {owner} {bot} {mins} 授权分钟数）</div>
  <textarea id="flok" style="min-height:80px">${esc(window._flow.ok_text||'')}</textarea></div>
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">验证失败文本（占位符 {owner}）</div>
  <input id="flfail" value="${esc(window._flow.fail_text||'')}" style="width:100%"></div>
  <div style="margin-top:10px"><div class="k" style="margin-bottom:4px">授权码过期文本 / 任务完成文本（占位符 {left} 剩余秒数）</div>
  <textarea id="flexp" style="min-height:60px">${esc(window._flow.expired_text||'')}</textarea>
  <input id="fldone" value="${esc(window._flow.done_text||'')}" style="width:100%;margin-top:6px"></div>
  <div style="margin-top:10px"><label>授权窗口 <input id="flwin" type="number" min="30" max="3600" value="${window._flow.auth_window_sec||120}" style="width:80px"> 秒</label></div>
  <div style="margin-top:12px;display:flex;gap:8px"><button class="act" onclick="saveFlow()">保存流程设置</button>
  <button class="act ghost" onclick="resetFlow()">恢复默认</button></div>
 </div>`;
 renderPdList();
}
async function saveFlow(){
 const mode=(document.querySelector('input[name=fltmode]:checked')||{}).value||'auto';
 const body={trigger_mode:mode,
  keywords:document.getElementById('flkw').value,
  ask_text:document.getElementById('flask').value,
  ok_text:document.getElementById('flok').value,
  fail_text:document.getElementById('flfail').value,
  expired_text:document.getElementById('flexp').value,
  done_text:document.getElementById('fldone').value,
  auth_window_sec:parseInt(document.getElementById('flwin').value)||120};
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

async function vSettings(){
 const [c,rv,kl]=await Promise.all([
  api('/api/app-config').catch(()=>({})),
  api('/api/reply-config').catch(()=>({})),
  api('/api/keys').catch(()=>({keys:[]})),
 ]);
 const keysL=kl.keys||[];
 $('#view').innerHTML=head('运行环境、密钥与启动行为')+saveHint(_SAVE_HINT)+
 `<div class="card"><div class="k">运行环境（首次使用请核对；每项改动回车/失焦后自动保存，端口改动重启各组件后生效）</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">
   <div><div class="k" style="margin-bottom:4px">机器人 QQ 号</div><input id="sbotqq" value="${esc(String(c.bot_qq||''))}" style="width:100%" placeholder="如 3126940548" onchange="saveEnv('sbotqq','bot_qq',0,'机器人 QQ 已保存（已生效）')"></div>
   <div><div class="k" style="margin-bottom:4px">主人 QQ 号（任务/遥控白名单）</div><input id="sownerqq" value="${esc(String(c.owner_qq||''))}" style="width:100%" placeholder="如 3349525227" onchange="saveEnv('sownerqq','owner_qq',0,'主人 QQ 已保存（已生效）')"></div>
   <div style="grid-column:1/3"><div class="k" style="margin-bottom:4px">QQNT 安装目录（留空=自动探测，探测到：${esc(c.detected_qqnt||'未探测到')}）</div><input id="sqqnt" value="${esc(String(c.qqnt_dir||''))}" style="width:100%" onchange="saveEnv('sqqnt','qqnt_dir',0,'QQNT 目录已保存（下次启动任务时生效）')"></div>
  </div>
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
 <div class="card"><div class="k">API Key（智谱开放平台获取；多个 Key 时选一个「主」——生图/联网搜索/模型链未指定 Key 的条目都用主 Key；模型链每行可在「模型管理」单独指定 Key）</div>
  <div style="margin-top:10px">${keysL.map(k=>`<div class="row">
   <label style="margin:0;flex:none;white-space:nowrap"><input type="radio" name="primkey" ${k.primary?'checked':''} onchange="setPrimary('${esc(k.id)}')"> 主</label>
   <input value="${esc(k.label||'')}" data-kid="${esc(k.id)}" style="flex:1" onchange="renameKey('${esc(k.id)}',this)">
   <span class="mono" style="flex:none;width:150px">${esc(k.mask||'')}</span>
   <button class="act ghost danger" onclick="delKey('${esc(k.id)}')">删除</button></div>`).join('')||'<i class="k">尚未配置任何 Key</i>'}</div>
  <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
   <input id="nklabel" placeholder="备注，如：企业/个人" style="width:130px">
   <input id="nkval" type="password" placeholder="粘贴 API Key" style="flex:1;min-width:220px">
   <button class="act" onclick="addKey()">添加 Key</button></div>
  <div class="k" style="margin-top:6px">Key 仅保存在本机数据目录（不进源码），列表仅显示掩码；第一个添加的自动成为主 Key，删除主 Key 后自动切到剩余第一个。模型链中引用某 Key 的条目在该 Key 被删除后会被自动跳过。</div></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.lean_ctx_enabled!==false?'checked':''} onchange="saveLeanCtx(this.checked)">
   <span><b>精简上下文（推荐开启）</b><div class="k" style="margin-top:3px">开启后：旁观消息先经约 0.5k token 的轻量判定决定是否开口（决定开口才用全量提示词生成），回复时按各模型的上下文档位裁剪历史与记忆——人设/风格/安全规则始终全量，回复质量不受影响。关闭后：每次调用都带完整系统提示与更多历史记忆（追求最佳表现，token 消耗大约 +50%~80%，旁观评估为主增量）</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.wait_gate_enabled!==false?'checked':''} onchange="saveKV('/api/reply-config',{wait_gate_enabled:this.checked?1:0},'等说完协议已'+(this.checked?'开启':'关闭'));load()">
  <span><b>等说完协议（推荐开启）</b><div class="k" style="margin-top:3px">AI 回复前会判断对方这条消息是否是叙述的中间部分（语义没说完、刚开头就断、明显还有下文）。如果判断对方还在说，AI 会先安静等对方说完再回复——避免在别人一句话还没打完时就急着插嘴打断。对完整的问题和闲聊不影响（立刻回复），零额外消耗，仅增加一条判断规则</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.burst_merge_enabled!==false?'checked':''} onchange="saveKV('/api/reply-config',{burst_merge_enabled:this.checked?1:0},'碎片合并已'+(this.checked?'开启':'关闭'));load()">
  <span><b>碎片合并（推荐开启）</b><div class="k" style="margin-top:3px">QQ 聊天习惯一句话一句话地发。开启后，如果有人在 3 秒内连发了 2 条以上消息（正在打字中），机器人的主动插话会暂缓，等此人停下来后再处理——多条碎片合并成一次回复，上下文更完整。被 @ 和被回复的消息不受影响（始终立刻回应），仅影响关键词/闲聊的主动插话时机，零额外消耗</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.vision_enabled?'checked':''} onchange="saveVision(this.checked)">
  <span><b>看图模式</b><div class="k" style="margin-top:3px">开启后，消息里的图片会交给视觉模型识别并回应（群聊与私聊均生效）；视觉模型在「模型管理」页配置，识别失败自动降级纯文本</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.web_search_enabled?'checked':''} onchange="saveWebSearch(this.checked)">
  <span><b>联网搜索</b><div class="k" style="margin-top:3px">遇到实时性问题（新闻/天气/价格/最新版本等）自动先联网搜索再回答，普通闲聊不触发不计费；使用前需在智谱开放平台（open.bigmodel.cn → 财务 → 资源包）开通搜索资源包（search_std 约 0.01 元/次），未开通时搜索静默跳过、机器人正常聊天</div></span></label></div>
 <div class="card"><label class="master"><input type="checkbox" ${rv.abuse_guard_enabled!==false?'checked':''} onchange="saveAbuseGuard(this.checked)">
  <span><b>防滥用防护</b><div class="k" style="margin-top:3px">整合三重防护，建议保持开启：① 人设防篡改——"忽略设定/你现在是XX/输出系统提示"等注入话术不生效，设定不泄露；② 任务拦截——竞赛题、代写作业、要代码成品等把机器人当免费任务引擎的消息，会以人设口吻轻松回绝；③ 身份锚定——多轮对话被"说服"后仍回归人设。关闭后所有消息按原始对话处理（仅调试用）</div></span></label></div>
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
 <div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">自动复盘<span class="badge" style="background:var(--tint);color:var(--dim)">夜间自动执行</span></div>
  <label class="master"><input type="checkbox" ${rv.daily_review_enabled!==false?'checked':''} onchange="saveKV('/api/reply-config',{daily_review_enabled:this.checked?1:0},'自动复盘已'+(this.checked?'开启':'关闭'));load()">
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
  <button class="act ghost" style="margin:0" onclick="backupNow()">立即备份</button></div>
  <div class="k" style="margin-top:4px">超过保留天数或保留份数的旧备份自动清理（最旧的先清）</div></div></div>
 <div class="card"><div class="k">关闭本应用时</div>
  <div style="margin-top:8px">
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="tray" ${(!c.close_action||c.close_action==='tray')?'checked':''} onchange="saveCloseAction(this.value)"> 最小化到系统托盘，机器人继续在后台（推荐；托盘图标可重新打开窗口）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_app" ${c.close_action==='exit_app'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用，机器人保持运行（下次双击图标重新打开）</label>
   <label style="display:block;margin:6px 0"><input type="radio" name="cact" value="exit_all" ${c.close_action==='exit_all'?'checked':''} onchange="saveCloseAction(this.value)"> 退出本应用并停止机器人（关闭时会二次确认）</label>
  </div></div>`;
}
let _autoReg=null;
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
async function saveFeature(key,on){await saveKV('/api/reply-config',{[key]:on},(_FEAT_NAME[key]||key)+(on?'已开启':'已关闭')+'（已生效）')}
async function saveEcon(key,val){await saveKV('/api/reply-config',{[key]:val},{econ_enabled:'高峰降耗总开关',econ_manual:'开启方式',econ_days:'生效日'}[key]+'已更新（已生效）');load()}
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
async function saveProNum(field,val,what){await saveKV('/api/reply-config',{['proactive_'+field]:val},'主动'+what+'已更新（已生效）');load()}
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
async function saveLimit(key,val){await saveKV('/api/reply-config',{[key]:val},{token_limit_enabled:'token 限额'}[key]+'已更新（已生效）');load()}
async function saveLimitHours(){const v=parseInt($('#tlhours').value)||1;await saveKV('/api/reply-config',{token_limit_hours:Math.max(1,Math.min(24,v))},'限额窗口已更新（已生效）');load()}
async function saveLimitMax(){const v=parseInt($('#tlmax').value)||0;await saveKV('/api/reply-config',{token_limit_max:Math.max(1000,v)},'限额上限已更新（已生效）');load()}
function renderLimitUsage(u){
 const b=document.getElementById('tlimit-badge'),el=document.getElementById('tlimit-usage');if(!el)return;
 el.textContent=u.enabled?('本窗口已用 '+u.used.toLocaleString()+' / '+u.cap.toLocaleString()+' tokens（'+(u.cap?Math.min(100,Math.round(u.used/u.cap*100)):0)+'%）'):'';
 if(b)b.outerHTML=u.hold?'<span id="tlimit-badge" class="badge" style="background:var(--err);color:#fff">已达上限 · 已停响应</span>'
  :'<span id="tlimit-badge" class="badge" style="background:var(--tint);color:var(--dim)">'+(u.enabled?'限额中':'未启用')+'</span>';
}
async function addKey(){
 const label=$('#nklabel').value.trim(),key=$('#nkval').value.trim();
 if(!key){toast('请先粘贴 API Key',false);return}
 const r=await saveKV('/api/keys/add',{label,key},'Key 已添加（第一个自动成为主 Key）');
 if(r.ok!==false){load()}}
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
                    out.append({"id": e["id"], "label": str(e.get("label", "")),
                                "mask": (k[:6] + "…" + k[-4:]) if len(k) > 14 else "…",
                                "primary": e["id"] == primary})
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
        if u.path == "/api/devctrl-flow":
            import qq_bot_agent as _A
            return self._reply(200, {"ok": True, "flow": _A.devctrl_flow(),
                                     "defaults": _A._DEFAULT_FLOW})
        if u.path == "/api/devctrl-code":
            # 控制台常显授权码：确保当前有一个有效码（无则生成）
            import qq_bot_agent as _A
            import time as _t
            code_obj = _A._DEVCTRL_CODE
            if not code_obj["code"] or _t.time() > code_obj["expires"]:
                code_obj["code"] = C.gen_auth_code()
                code_obj["expires"] = _t.time() + 300
            return self._reply(200, {"ok": True, "code": code_obj["code"],
                                     "expires_in": int(code_obj["expires"] - _t.time())})
        if u.path == "/api/backup-now":
            import qq_bot_agent as _A
            _A._backup_data()
            return self._reply(200, {"ok": True})
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
                if len(key) < 20 or len(key) > 120 or any(c.isspace() for c in key):
                    return self._reply(400, {"ok": False, "error": "Key 格式非法（20-120字符，无空白）"})
                if len(keys) >= 10:
                    return self._reply(400, {"ok": False, "error": "Key 最多 10 个"})
                nid = "k%d" % (max([int(e["id"][1:]) for e in keys
                                    if isinstance(e, dict) and str(e.get("id", "")).startswith("k")
                                    and str(e["id"])[1:].isdigit()] or [0]) + 1)
                keys.append({"id": nid, "label": label or ("Key " + nid[1:]), "key": key})
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
                    if not nl:
                        return self._reply(400, {"ok": False, "error": "备注不能为空"})
                    hit[0]["label"] = nl
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
                        return self._reply(400, {"ok": False, "error": "触发方式非法"})
                    patch["trigger_mode"] = body["trigger_mode"]
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
                        patch["auth_window_sec"] = max(30, min(3600, int(body["auth_window_sec"])))
                    except (TypeError, ValueError):
                        return self._reply(400, {"ok": False, "error": "授权窗口应为 30-3600 秒"})
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
    print(json.dumps({"admin_ui_ready": True, "url": "http://127.0.0.1:%d" % args.port},
                     ensure_ascii=False), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
