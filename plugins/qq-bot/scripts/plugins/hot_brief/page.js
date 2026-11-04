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
 return '<div class="card"><div class="k" style="margin-bottom:6px"><b>今日速览</b></div>'
 +(curText?'<div class="k" style="white-space:pre-wrap;font-size:12.5px">'+esc(curText.slice(0,600))+'</div>':'<div class="k">今天还没总结过——点下面按钮立刻来一份。</div>')
 +'<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<button class="act" style="margin:0" onclick="hbRefresh()">立刻总结当前热点</button>'
 +'<label class="k"><input type="checkbox" id="hb-auto"> 每 6 小时自动总结</label>'
 +'<span class="k" id="hb-msg"></span></div>'
 +'<div class="k" style="margin-top:6px;font-size:12px">'+autoHint+'</div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>历史记录</b>（分天存档，可回看/删除）</div>'
 +(rows||'<div class="k">暂无历史记录</div>')+'</div>';
 setTimeout(async()=>{
  const st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'load_auto'})}).catch(()=>null);
  const el=document.getElementById('hb-auto');
  if(el&&st&&st.ok)el.checked=st.auto_enabled!==false;
  el&&(el.onchange=()=>hbSetAuto(el.checked));
 },0);
}
async function hbRefresh(){
 const m=document.getElementById('hb-msg');m&&(m.textContent='总结中…（搜索约几秒）');
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'refresh_now'})}).catch(()=>({ok:false}));
 if(m)m.textContent=r.ok?'✓ 已更新今日速览':'失败：'+(r.error||'');
 if(r.ok)load();
}
async function hbSetAuto(on){
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'set_auto',args:{enabled:on}})});
 toast('定时总结已'+(on?'开启':'关闭'));
}
async function hbView(date){
 const r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'get_day',args:{date}})}).catch(()=>({ok:false}));
 const t=(r.ok&&r.record)?(r.record.text||''):'（无记录）';
 dlgConfirm({title:date+' 热点速览',body:'<div class="k" style="white-space:pre-wrap;font-size:12.5px">'+esc(t.slice(0,800))+'</div>',okText:'关闭'});
}
async function hbDel(date){
 if(!await dlgConfirm({title:'删除 '+date+' 的记录？',body:'删除后无法恢复（当日速览不受影响，明天还会生成新的）',danger:true,okText:'删除'}))return;
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'hot_brief',method:'delete_day',args:{date}})});
 toast('已删除');load();
}
