// qq_status 插件设置页：上下线自动 / 定时状态 / 手动切换
// 顶层禁 let/const（宿主全局作用域共享）；'+...' 内联事件单引号 \' 转义
async function qqStatusPage(d){
 var PC=function(m,a){return api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'qq_status',method:m,args:a||{}})}).catch(function(){return{ok:false,error:'请求失败'}})};
 var st=await PC('load_cfg');
 if(!st||st.ok===false){return '<div class="card">加载失败：'+esc((st&&st.error)||'')+'</div>'}
 var cfg=st.cfg||{};
 var STS=st.statuses||[];
 var lbl={};STS.forEach(function(s){lbl[s.id]=s.label});
 var hostOk=st.host_ok!==false;

 var kindOpts=function(sel){return STS.map(function(s){return '<option value="'+esc(s.id)+'" '+(s.id===sel?'selected':'')+'> '+esc(s.label)+'</option>'}).join('')};
 var rules=(cfg.rules||[]);
 var ruleRows=rules.map(function(r,i){
  return '<div style="display:flex;gap:8px;align-items:center;margin:4px 0">'
   +'<input type="time" value="'+esc(r.t)+'" onchange="slRuleTime('+i+',this.value)">'
   +'<select onchange="slRuleKind('+i+',this.value)">'+kindOpts(r.kind)+'</select>'
   +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="slRuleDel('+i+')">删除</button></div>';
 }).join('');

 var manualBtns=STS.map(function(s){
  return '<button class="act ghost" style="margin:0;padding:5px 12px;font-size:12.5px" onclick="slNow(\''+esc(s.id)+'\')">'+esc(s.label)+'</button>';
 }).join('');

 var html='<div class="card"><div class="k" style="margin-bottom:6px"><b>这是什么</b>'
  +(hostOk?'':'<span class="badge" style="background:var(--err);color:#fff">需要 0.5.11+ 宿主</span>')
  +'</div><div class="k">管理机器人 QQ 的在线状态与个性状态（右上角自己头衔旁那串）。三个能力各自独立开关，互不影响。原内置「QQ 在线状态」设置由本插件接管（数据同源，不丢配置）。</div></div>'

  +'<div class="card"><label class="master"><input type="checkbox" id="qs-auto" '+(cfg.auto_enabled?'checked':'')+' onchange="slSave()"> <b>① 上下线自动</b><div class="k" style="margin-top:3px">机器人启动登录成功后自动设为「在线」；你在状态页手动停止机器人前自动切「离开」——让群里的人一眼看出它在不在岗</div></label></div>'

  +'<div class="card"><label class="master"><input type="checkbox" id="qs-sched" '+(cfg.schedule_enabled?'checked':'')+' onchange="slSave()"> <b>② 定时状态</b><div class="k" style="margin-top:3px">到点自动切换。适合：晚上 23:30「睡觉中」、上班 9:00「搬砖中」、睡前「追剧中」……每条规则每天最多触发一次（错过不补）</div></label>'
  +'<div id="qs-rules-row" class="dimmed" style="margin-top:8px">'+(ruleRows||'<div class="k" style="margin-bottom:4px">（还没有规则）</div>')
  +'<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="time" id="qs-new-t" value="23:30"><select id="qs-new-k">'+kindOpts('sleep')+'</select>'
  +'<button class="act ghost" style="margin:0" onclick="slRuleAdd()">添加规则</button></div>'
  +'<div class="k" style="margin-top:4px">改规则后记得点下方「保存设置」。</div></div></div>'

  +'<div class="card"><label class="master"><input type="checkbox" id="qs-manual" '+(cfg.manual_enabled!==false?'checked':'')+' onchange="slSave()"> <b>③ 手动切换</b><div class="k" style="margin-top:3px">在下方按钮上一点，立即切换到任意状态（手动设置会一直保持，直到下次自动/定时覆盖它）</div></label>'
  +'<div id="qs-manual-row" class="dimmed" style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">'+manualBtns+'</div>'
  +'<div class="k" id="qs-now" style="margin-top:6px"></div></div>'

  +'<div class="card"><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><button class="act" onclick="slSave()">保存设置</button><span class="k" id="qs-msg"></span></div>'
  +'<div class="k" style="margin-top:6px">状态全集：在线 / 离开 / 忙碌 / 请勿打扰 / Q我吧 / 隐身 + QQ 个性状态（睡觉中·学习中·搬砖中·摸鱼中·听歌中·追剧中·熬夜中·emo中·恋爱中·无聊中·元气满满·好运锦鲤）。机器人手动停止前自动切「离开」由①控制；定时与手动切的是状态显示，不影响消息回复。</div></div>';

 setTimeout(function(){
  slSchedToggle();slManualToggle();
 },0);
 return html;
}
function slSchedToggle(){
 var on=document.getElementById('qs-sched'),row=document.getElementById('qs-rules-row');
 if(on&&row)row.classList.toggle('dimmed',!on.checked);
}
function slManualToggle(){
 var on=document.getElementById('qs-manual'),row=document.getElementById('qs-manual-row');
 if(on&&row)row.classList.toggle('dimmed',!on.checked);
}
async function slSave(){
 var msg=document.getElementById('qs-msg');if(msg)msg.textContent='保存中…';
 var rules=[];
 // 从 DOM 收集规则（保持顺序）
 var box=document.getElementById('qs-rules-row');
 if(box){box.querySelectorAll('div').forEach(function(row){
  var t=row.querySelector('input[type="time"]'),k=row.querySelector('select');
  if(t&&k)rules.push({t:t.value,kind:k.value});
 });}
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'qq_status',method:'save_cfg',args:{
  auto_enabled:document.getElementById('qs-auto').checked,
  schedule_enabled:document.getElementById('qs-sched').checked,
  manual_enabled:document.getElementById('qs-manual').checked,
  rules:rules
 }})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(!r||r.ok===false){if(msg)msg.textContent='保存失败：'+((r&&r.error)||'');return}
 if(msg)msg.textContent='已保存 ✓（上下线自动已同步给机器人）';
}
function slRuleTime(i,v){/* 保存时统一收集 */}
function slRuleKind(i,v){/* 保存时统一收集 */}
function slRuleAdd(){
 var t=document.getElementById('qs-new-t').value||'23:30';
 var k=document.getElementById('qs-new-k').value||'sleep';
 var row=document.createElement('div');
 row.style.cssText='display:flex;gap:8px;align-items:center;margin:4px 0';
 row.innerHTML='<input type="time" value="'+t+'"><select>'+document.getElementById('qs-new-k').innerHTML.replace('"'+k+'"','"'+k+'" selected')+'</select>'
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="this.parentElement.remove()">删除</button>';
 document.getElementById('qs-rules-row').insertBefore(row,document.getElementById('qs-rules-row').querySelector('div:last-of-type'));
}
async function slNow(kind){
 var el=document.getElementById('qs-now');if(el)el.textContent='切换中…';
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'qq_status',method:'set_now',args:{kind:kind}})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(r&&r.ok){if(el)el.textContent='✓ 已切换（QQ 客户端即时可见）';}
 else{if(el)el.textContent='失败：'+((r&&r.error)||'');}
}
