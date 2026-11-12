// bili_sub 插件设置页：订阅管理/目标群选择/内容类型勾选（随插件包分发）
var __BS={subs:[],wl:{},login:false,interval:5,probeUid:'',probeName:''};
async function biliSubPage(d){
 var st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'load_state'})}).catch(()=>({ok:false}));
 if(st&&st.ok){__BS.subs=st.subs||[];__BS.wl=st.wl_groups||{};__BS.login=!!st.bili_login;__BS.interval=st.interval||5;}
 var loginHtml=__BS.login
  ?'<span class="k" style="color:var(--ok,#2e7d32)">✓ B站已登录（可拉动态/视频）</span>'
  :'<span class="k" style="color:#c62828">✗ B站未登录——动态/视频推送不可用（直播不受影响）。请到「B站刷视频」插件页扫码登录后回来。</span>';
 var wlChips=Object.keys(__BS.wl).map(function(g){return __bsGroupChip(g,__BS.wl[g]||g);}).join('');
 var html='<div class="card"><div style="margin-bottom:6px"><b>B站订阅推送</b> <span class="k" style="font-size:12px">（订阅的UP有新内容时，机器人自动转发到指定群）</span></div>'+loginHtml+'</div>'
 +'<div class="card"><div style="margin-bottom:6px"><b>添加订阅</b></div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px">'
 +'<input id="bs-uid" placeholder="UP的UID或空间链接（space.bilibili.com/xxxx）" style="flex:1;min-width:240px">'
 +'<button class="act" style="margin:0" onclick="__bsProbe()">查询</button>'
 +'<span class="k" id="bs-probe"></span></div>'
 +'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:6px" class="k">'
 +'<label><input type="checkbox" id="bs-t-av" checked> 新视频</label>'
 +'<label><input type="checkbox" id="bs-t-draw" checked> 图文动态</label>'
 +'<label><input type="checkbox" id="bs-t-live" checked> 直播开播</label></div>'
 +'<div class="k" style="font-size:12px;margin-bottom:4px">转发到哪些群（点选白名单群，或手动加群号）：</div>'
 +'<div id="bs-newgroups" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'+wlChips+'</div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="bs-mgroup" placeholder="手动加群号（回车添加）" style="width:190px" onkeydown="if(event.key===\'Enter\'){__bsAddManual(\'bs-newgroups\',\'bs-mgroup\')}">'
 +'<button class="act" style="margin:0" onclick="__bsAdd()">订阅</button>'
 +'<span class="k" id="bs-addmsg"></span></div></div>'
 +'<div class="card"><div style="margin-bottom:6px"><b>订阅列表</b> <span class="k" style="font-size:12px">（勾选与目标群改动即时保存）</span></div>'
 +'<div id="bs-list"></div></div>'
 +'<div class="card"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>轮询间隔</b>'
 +'<input type="number" id="bs-interval" min="2" max="60" step="1" style="width:60px" value="'+__BS.interval+'"> <span class="k">分钟（2-60；默认5）</span>'
 +'<button class="act" style="margin:0" onclick="__bsSetInterval()">保存</button><span class="k" id="bs-intmsg"></span></div>'
 +'<div class="k" style="font-size:12px;margin-top:6px">新订阅首轮回询只记水位不推送（防历史轰炸）；单轮单UP最多推3条；动态请求间隔≥2秒。</div></div>';
 setTimeout(function(){__bsRenderList();},0);
 return html;
}
function __bsGroupChip(g,label){
 var on=String(g)+':1';
 return '<span class="k bs-gchip" data-g="'+esc(String(g))+'" data-on="0" onclick="__bsToggleChip(this)" style="padding:2px 10px;border:1px solid var(--line);border-radius:14px;cursor:pointer;font-size:12.5px">'+esc(label||g)+'</span>';
}
function __bsToggleChip(el){el.dataset.on=el.dataset.on==='1'?'0':'1';el.style.borderColor=el.dataset.on==='1'?'var(--acc,#4f7cff)':'var(--line)';el.style.background=el.dataset.on==='1'?'rgba(79,124,255,.12)':'';}
function __bsPickedGroups(cid){var out=[];document.querySelectorAll('#'+cid+' .bs-gchip').forEach(function(c){if(c.dataset.on==='1')out.push(c.dataset.g);});return out;}
function __bsAddManual(cid,iid){var inp=document.getElementById(iid);var v=(inp.value||'').replace(/\D/g,'');if(!v)return;var host=document.getElementById(cid);var chip=document.createElement('span');chip.className='k bs-gchip';chip.dataset.g=v;chip.dataset.on='1';chip.textContent=v;chip.onclick=function(){__bsToggleChip(chip)};chip.style.cssText='padding:2px 10px;border:1px solid var(--acc,#4f7cff);border-radius:14px;cursor:pointer;font-size:12.5px;background:rgba(79,124,255,.12)';host.appendChild(chip);inp.value='';}
async function __bsProbe(){
 var uid=document.getElementById('bs-uid').value.trim();
 var msg=document.getElementById('bs-probe');msg.textContent='查询中…';
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'probe',args:{uid:uid}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.probeUid=r.uid;__BS.probeName=r.name;msg.textContent='→ '+r.name+'（UID:'+r.uid+'）';}
 else{msg.textContent='✗ '+(r&&r.error||'查询失败');__BS.probeUid='';}
}
async function __bsAdd(){
 var msg=document.getElementById('bs-addmsg');msg.textContent='';
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_add',args:{
  uid:__BS.probeUid||document.getElementById('bs-uid').value,
  name:__BS.probeName,
  av:document.getElementById('bs-t-av').checked?1:0,
  draw:document.getElementById('bs-t-draw').checked?1:0,
  live:document.getElementById('bs-t-live').checked?1:0,
  groups:__bsPickedGroups('bs-newgroups')}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.subs.push(r.sub);__BS.probeUid='';__BS.probeName='';document.getElementById('bs-uid').value='';document.getElementById('bs-probe').textContent='';document.querySelectorAll('#bs-newgroups .bs-gchip').forEach(function(c){__bsToggleChip(c);if(c.dataset.on==='1')__bsToggleChip(c);});msg.textContent='✓ 已订阅（首轮只记水位，从下轮开始推送新内容）';__bsRenderList();}
 else{msg.textContent='✗ '+(r&&r.error||'添加失败');}
}
function __bsRenderList(){
 var host=document.getElementById('bs-list');if(!host)return;
 if(!__BS.subs.length){host.innerHTML='<div class="k">还没有订阅——上面添加第一个吧。</div>';return;}
 host.innerHTML=__BS.subs.map(function(s,idx){
  var chips=Object.keys(__BS.wl).map(function(g){return __bsSubChip(s,g,__BS.wl[g]||g);}).join('');
  var own=(s.groups||[]).filter(function(g){return !__BS.wl[g];}).map(function(g){return __bsSubChip(s,g,g);}).join('');
  return '<div style="border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0">'
  +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(s.name||('UID:'+s.uid))+'</b><span class="k mono" style="font-size:12px">'+esc(String(s.uid))+'</span>'
  +'<button class="act ghost danger" style="margin:0 0 0 auto;padding:2px 10px;font-size:12px" onclick="__bsDel('+idx+')">退订</button>'
  +'<button class="act ghost" style="margin:0;padding:2px 10px;font-size:12px" onclick="__bsTest('+idx+')">测试推送</button></div>'
  +'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:6px 0" class="k">'
  +'<label><input type="checkbox" '+(s.av?'checked':'')+' onchange="__bsUpd('+idx+',\'av\',this.checked?1:0)"> 新视频</label>'
  +'<label><input type="checkbox" '+(s.draw?'checked':'')+' onchange="__bsUpd('+idx+',\'draw\',this.checked?1:0)"> 图文动态</label>'
  +'<label><input type="checkbox" '+(s.live?'checked':'')+' onchange="__bsUpd('+idx+',\'live\',this.checked?1:0)"> 直播开播</label></div>'
  +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px" id="bs-g-'+idx+'">'+chips+own+'</div>'
  +'<div style="display:flex;gap:6px;align-items:center"><input id="bs-mg-'+idx+'" placeholder="加群号（回车）" style="width:150px;padding:3px 8px" onkeydown="if(event.key===\'Enter\'){__bsAddGroup('+idx+')}"><span class="k" style="font-size:12px">点击群名=加入/移出推送目标</span></div></div>';
 }).join('');
}
function __bsSubChip(s,g,label){
 var on=(s.groups||[]).indexOf(String(g))>=0;
 return '<span class="k bs-sg" data-g="'+esc(String(g))+'" onclick="__bsToggleSub('+__BS.subs.indexOf(s)+',\''+esc(String(g))+'\')" style="padding:2px 10px;border:1px solid '+(on?'var(--acc,#4f7cff)':'var(--line)')+';border-radius:14px;cursor:pointer;font-size:12.5px;'+(on?'background:rgba(79,124,255,.12)':'')+'">'+esc(label)+'</span>';
}
async function __bsUpd(idx,key,val){__BS.subs[idx][key]=val;await __bsSaveSub(idx);}
async function __bsToggleSub(idx,g){
 var gs=__BS.subs[idx].groups||[];var i=gs.indexOf(String(g));
 if(i>=0)gs.splice(i,1);else gs.push(String(g));
 await __bsSaveSub(idx);
}
async function __bsAddGroup(idx){var inp=document.getElementById('bs-mg-'+idx);var v=(inp.value||'').replace(/\D/g,'');if(!v)return;var gs=__BS.subs[idx].groups||[];if(gs.indexOf(v)<0)gs.push(v);inp.value='';await __bsSaveSub(idx);}
async function __bsSaveSub(idx){
 var s=__BS.subs[idx];
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_update',args:{uid:s.uid,av:s.av,draw:s.draw,live:s.live,groups:s.groups}})}).catch(()=>null);
 __bsRenderList();
}
async function __bsDel(idx){
 var s=__BS.subs[idx];
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_del',args:{uid:s.uid}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.subs.splice(idx,1);__bsRenderList();}
}
async function __bsTest(idx){
 var s=__BS.subs[idx];var g=(s.groups||[])[0];
 if(!g){alert('先给这个订阅选一个目标群');return;}
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'test_push',args:{group:g}})}).catch(()=>({ok:false}));
 alert(r&&r.ok?('测试消息已发往群 '+g):('失败：'+(r&&r.error||'未知')));
}
async function __bsSetInterval(){
 var v=parseInt(document.getElementById('bs-interval').value,10);
 var msg=document.getElementById('bs-intmsg');
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'set_interval',args:{interval:v}})}).catch(()=>({ok:false}));
 msg.textContent=r&&r.ok?'✓ 已保存':'✗ '+(r&&r.error||'失败');
}
