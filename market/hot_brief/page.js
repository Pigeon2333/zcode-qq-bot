// hot_brief 插件设置页：手动总结/定时开关/历史记录（随插件包分发）
async function hotBriefPage(d){
 // 自定义端点拉状态
 const hist=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'history'})}).catch(()=>({ok:false}));
 const cur=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'get_day',args:{date:new Date().toISOString().slice(0,10)}})}).catch(()=>({ok:false}));
 const autoHint='（定时开关保存后下次插件加载生效；当前会话内手动总结随时可用）';
 const days=(hist.ok?(hist.days||[]):[]);
 const rows=days.map(x=>'<div style="display:flex;gap:8px;align-items:center;margin:5px 0;padding:7px;border:1px solid var(--line);border-radius:6px">'
  +'<b class="mono" style="min-width:86px">'+esc(x.date)+'</b>'
  +'<span class="k" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(x.preview||'')+'</span>'
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="hbView(\''+esc(x.date)+'\')">查看</button>'
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="hbDel(\''+esc(x.date)+'\')">删</button></div>').join('');
 const curText=(cur.ok&&cur.record)?(cur.record.text||''):'';
 const html='<div class="card"><div class="k" style="margin-bottom:6px"><b>今日速览</b></div>'
 +(curText?'<div class="k" style="white-space:pre-wrap;font-size:12.5px">'+esc(curText.slice(0,600))+'</div>':'<div class="k">今天还没总结过——点下面按钮立刻来一份。</div>')
 +'<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<button class="act" style="margin:0" onclick="hbRefresh()">立刻总结当前热点</button>'
 +'<label class="k"><input type="checkbox" id="hb-auto"> 每天定时自动总结</label>'
 +'<label class="k">时间 <input type="time" id="hb-time" value="03:00" onchange="hbSetTime(this.value)"></label>'
 +'<span class="k" id="hb-msg"></span></div>'
 +'<div class="k" style="margin-top:6px;font-size:12px">'+autoHint+'</div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>热点来源</b>（搜什么由你定）</div>'
 +'<div class="k" style="margin-bottom:6px">搜索关键词，最多 3 组（中英文逗号、顿号、空格、换行分隔均可）（如：今日热点新闻、微博热搜榜、科技新闻）。总结时先按这些关键词搜索，再由 AI 提炼成真正的热点事件清单（去掉聚合站导航/广告条目）。</div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="hb-query" placeholder="留空 = 默认（今日热点新闻 头条）" style="flex:1;min-width:260px">'
 +'<button class="act" style="margin:0" onclick="hbSetQuery()">保存来源</button>'
 +'<span class="k" id="hb-qmsg"></span></div></div>'
 +'<div class="card"><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><b>历史记录</b><span class="k">（分天存档，可回看/删除）</span>'
 +'<span style="margin-left:auto;display:flex;gap:6px;align-items:center" class="k">自动清理 <input type="number" id="hb-clean" min="0" max="3650" step="1" style="width:58px" title="每天自动删除 N 天前的旧记录；0=不清理"> 天前的记录（0=不清理）</span></div>'
 +(rows||'<div class="k">暂无历史记录</div>')+'</div>';
 // 必须在 return 之前调度（写在 return 后是死代码——回显已存配置全靠它）
 setTimeout(async()=>{
  const st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'load_auto'})}).catch(()=>null);
  const el=document.getElementById('hb-auto');
  if(el&&st&&st.ok)el.checked=st.auto_enabled!==false;
  el&&(el.onchange=()=>hbSetAuto(el.checked));
  const tm=document.getElementById('hb-time');
  if(tm&&st&&st.ok)tm.value=String(st.hh).padStart(2,'0')+':'+String(st.mm).padStart(2,'0');
  const cl=document.getElementById('hb-clean');
  if(cl&&st&&st.ok)cl.value=st.clean_days||0;
  cl&&(cl.onchange=()=>hbSetClean(cl.value));
  const q=document.getElementById('hb-query');
  if(q&&st&&st.ok)q.value=st.query||'';
 },0);
 return html;
}
async function hbSetClean(v){
 const n=parseInt(v)||0;
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'set_clean_days',args:{days:n}})}).catch(()=>({ok:false}));
 if(!r.ok){toast('保存失败：'+(r.error||''),false);return}
 toast(r.removed>0?('已保存：自动清理 '+r.clean_days+' 天前记录（本次清了 '+r.removed+' 条旧记录）'):('已保存：自动清理 '+r.clean_days+' 天前记录'));
 plPage('hot_brief');
}
async function hbRefresh(){
 const m=document.getElementById('hb-msg');m&&(m.textContent='总结中…（搜索+提炼约 10-30 秒）');
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'refresh_now'})}).catch(()=>({ok:false}));
 if(m)m.textContent=r.ok?'✓ 已更新今日速览':'失败：'+(r.error||'');
 if(r.ok)plPage('hot_brief');
}
async function hbSetAuto(on){
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'set_auto',args:{enabled:on}})});
 toast('定时总结已'+(on?'开启':'关闭'));
}
async function hbSetTime(v){
 if(!v)return;
 const parts=v.split(':');
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'set_auto_time',args:{hh:parseInt(parts[0])||0,mm:parseInt(parts[1])||0}})}).catch(()=>({ok:false}));
 toast(r.ok?('自动总结时间已设为 '+r.hh+':'+String(r.mm).padStart(2,'0')):'保存失败');
}
async function hbView(date){
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'get_day',args:{date}})}).catch(()=>({ok:false}));
 const t=(r.ok&&r.record)?(r.record.text||''):'（无记录）';
 dlgConfirm({title:date+' 热点速览',body:'<div class="k" style="white-space:pre-wrap;font-size:12.5px">'+esc(t.slice(0,800))+'</div>',okText:'关闭'});
}
async function hbDel(date){
 if(!await dlgConfirm({title:'删除 '+date+' 的记录？',body:'删除后无法恢复（当日速览不受影响，明天还会生成新的）',danger:true,okText:'删除'}))return;
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'delete_day',args:{date}})});
 toast('已删除');plPage('hot_brief');
}
async function hbSetQuery(){
 const q=(document.getElementById('hb-query').value||'').trim();
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'set_query',args:{query:q}})}).catch(()=>({ok:false}));
 if(!r||r.ok===false){toast('保存失败：'+((r&&r.error)||''),false);return}
 toast(q?('热点来源已保存：'+q):'已恢复默认来源关键词（今日热点新闻 头条）');
}
