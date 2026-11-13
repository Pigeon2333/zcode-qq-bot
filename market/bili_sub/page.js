// bili_sub 插件设置页 v1.1.0：订阅管理/群+私聊目标/推送历史重推（随插件包分发）
var __BS={subs:[],wl:{},friends:{},login:false,interval:5,hist:[],probeUid:'',probeName:''};
async function biliSubPage(d){
 var st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'load_state'})}).catch(()=>({ok:false}));
 if(st&&st.ok){__BS.subs=st.subs||[];__BS.wl=st.wl_groups||{};__BS.login=!!st.bili_login;__BS.interval=st.interval||5;__BS.hist=(st.history||[]).slice().reverse();}
 var fr=await api('/api/friend-names').catch(()=>({names:{}}));
 __BS.friends=(fr&&fr.names)||{};
 var loginHtml=__BS.login
  ?'<span class="k" style="color:var(--ok,#2e7d32)">✓ B站已登录（可拉动态/视频）</span>'
  :'<span class="k" style="color:#c62828">✗ B站未登录——动态/视频推送不可用（直播不受影响）。请到「B站刷视频」插件页扫码登录后回来。</span>';
 var gChips=Object.keys(__BS.wl).map(function(g){return __bsChip(g,__BS.wl[g]||g);}).join('');
 var fChips=Object.keys(__BS.friends).slice(0,40).map(function(u){return __bsChip('p:'+u,__BS.friends[u]||u);}).join('');
 var html='<div class="card"><div style="margin-bottom:6px"><b>B站订阅推送</b> <span class="k" style="font-size:12px">（订阅的UP有新内容时，机器人自动转发到指定群/私聊）</span></div>'+loginHtml+'</div>'
 +'<div class="card"><div style="margin-bottom:6px"><b>添加订阅</b></div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px">'
 +'<input id="bs-uid" placeholder="UP的UID或空间链接（space.bilibili.com/xxxx）" style="flex:1;min-width:240px">'
 +'<button class="act" style="margin:0" onclick="__bsProbe()">查询</button>'
 +'<span class="k" id="bs-probe"></span></div>'
 +'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:6px" class="k">'
 +'<label><input type="checkbox" id="bs-t-av" checked> 新视频</label>'
 +'<label><input type="checkbox" id="bs-t-draw" checked> 图文动态</label>'
 +'<label><input type="checkbox" id="bs-t-live" checked> 直播开播</label></div>'
 +'<div class="k" style="font-size:12px;margin-bottom:4px">推送到哪些群（点选）：</div>'
 +'<div id="bs-newgroups" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'+gChips+'</div>'
 +'<div class="k" style="font-size:12px;margin-bottom:4px">私聊推送给谁（点选好友或手动加QQ号）：</div>'
 +'<div id="bs-newpriv" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'+fChips+'</div>'
 +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
 +'<input id="bs-mtarget" placeholder="手动加群号或QQ号（回车添加）" style="width:210px" onkeydown="if(event.key===\'Enter\'){__bsAddManual(this.value)}">'
 +'<button class="act" style="margin:0" onclick="__bsAdd()">订阅</button>'
 +'<span class="k" id="bs-addmsg"></span></div></div>'
 +'<div class="card"><div style="margin-bottom:6px"><b>订阅列表</b> <span class="k" style="font-size:12px">（勾选与目标改动即时保存）</span></div>'
 +'<div id="bs-list"></div></div>'
 +'<div class="card"><div style="margin-bottom:6px"><b>推送历史</b> <span class="k" style="font-size:12px">（最近30条；绿=成功 红=失败——失败的或想补发的，点「重推」选目标再发一次）</span></div>'
 +'<div id="bs-hist"></div></div>'
 +'<div class="card"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>轮询间隔</b>'
 +'<input type="number" id="bs-interval" min="2" max="60" step="1" style="width:60px" value="'+__BS.interval+'"> <span class="k">分钟（2-60；默认5）</span>'
 +'<button class="act" style="margin:0" onclick="__bsSetInterval()">保存</button><span class="k" id="bs-intmsg"></span></div>'
 +'<div class="k" style="font-size:12px;margin-top:6px">新订阅首轮回询只记水位不推送（防历史轰炸）；单轮单UP最多推3条；动态请求间隔≥2秒。</div></div>';
 setTimeout(function(){__bsRenderList();__bsRenderHist();},0);
 return html;
}
function __bsChip(val,label){
 return '<span class="k bs-gchip" data-v="'+esc(String(val))+'" data-on="0" onclick="__bsToggleChip(this)" style="padding:2px 10px;border:1px solid var(--line);border-radius:14px;cursor:pointer;font-size:12.5px">'+esc(label)+'</span>';
}
function __bsToggleChip(el){el.dataset.on=el.dataset.on==='1'?'0':'1';el.style.borderColor=el.dataset.on==='1'?'var(--acc,#4f7cff)':'var(--line)';el.style.background=el.dataset.on==='1'?'rgba(79,124,255,.12)':'';}
function __bsPicked(cid){var out=[];document.querySelectorAll('#'+cid+' .bs-gchip').forEach(function(c){if(c.dataset.on==='1')out.push(c.dataset.v);});return out;}
function __bsSplitPicked(cid){var g=[],p=[];__bsPicked(cid).forEach(function(v){if(v.indexOf('p:')===0)p.push(v.slice(2));else g.push(v);});return {g:g,p:p};}
function __bsAddManual(v){v=(v||'').replace(/\D/g,'');if(!v)return;__bsAddManualTo('bs-newgroups',v);}
function __bsAddManualTo(cid,v){var host=document.getElementById(cid);if(!host)return;var chip=document.createElement('span');chip.className='k bs-gchip';chip.dataset.v=(cid==='bs-newpriv'?'p:':'')+v;chip.dataset.on='1';chip.textContent=v;chip.onclick=function(){__bsToggleChip(chip)};chip.style.cssText='padding:2px 10px;border:1px solid var(--acc,#4f7cff);border-radius:14px;cursor:pointer;font-size:12.5px;background:rgba(79,124,255,.12)';host.appendChild(chip);}
async function __bsProbe(){
 var uid=document.getElementById('bs-uid').value.trim();
 var msg=document.getElementById('bs-probe');msg.textContent='查询中…';
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'probe',args:{uid:uid}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.probeUid=r.uid;__BS.probeName=r.name;msg.textContent='→ '+r.name+'（UID:'+r.uid+'）';}
 else{msg.textContent='✗ '+(r&&r.error||'查询失败');__BS.probeUid='';}
}
async function __bsAdd(){
 var msg=document.getElementById('bs-addmsg');msg.textContent='';
 var sel=__bsSplitPicked('bs-newgroups');
 var selP=__bsSplitPicked('bs-newpriv');
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_add',args:{
  uid:__BS.probeUid||document.getElementById('bs-uid').value,
  name:__BS.probeName,
  av:document.getElementById('bs-t-av').checked?1:0,
  draw:document.getElementById('bs-t-draw').checked?1:0,
  live:document.getElementById('bs-t-live').checked?1:0,
  groups:sel.g,privates:selP.p}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.subs.push(r.sub);__BS.probeUid='';__BS.probeName='';document.getElementById('bs-uid').value='';document.getElementById('bs-probe').textContent='';document.querySelectorAll('#bs-newgroups .bs-gchip,#bs-newpriv .bs-gchip').forEach(function(c){if(c.dataset.on==='1')__bsToggleChip(c);});msg.textContent='✓ 已订阅（首轮只记水位，从下轮开始推送新内容）';__bsRenderList();}
 else{msg.textContent='✗ '+(r&&r.error||'添加失败');}
}
function __bsRenderList(){
 var host=document.getElementById('bs-list');if(!host)return;
 if(!__BS.subs.length){host.innerHTML='<div class="k">还没有订阅——上面添加第一个吧。</div>';return;}
 host.innerHTML=__BS.subs.map(function(s,idx){
  var chips=Object.keys(__BS.wl).map(function(g){return __bsSubChip(s,'g',g,__BS.wl[g]||g);}).join('');
  var ownG=(s.groups||[]).filter(function(g){return !__BS.wl[g];}).map(function(g){return __bsSubChip(s,'g',g,g);}).join('');
  var ownP=(s.privates||[]).map(function(u){return __bsSubChip(s,'p',u,__BS.friends[u]||u);}).join('');
  return '<div style="border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0">'
  +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(s.name||('UID:'+s.uid))+'</b><span class="k mono" style="font-size:12px">'+esc(String(s.uid))+'</span>'
  +'<button class="act ghost danger" style="margin:0 0 0 auto;padding:2px 10px;font-size:12px" onclick="__bsDel('+idx+')">退订</button>'
  +'<button class="act ghost" style="margin:0;padding:2px 10px;font-size:12px" onclick="__bsTest('+idx+')">测试推送</button></div>'
  +'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:6px 0" class="k">'
  +'<label><input type="checkbox" '+(s.av?'checked':'')+' onchange="__bsUpd('+idx+',\'av\',this.checked?1:0)"> 新视频</label>'
  +'<label><input type="checkbox" '+(s.draw?'checked':'')+' onchange="__bsUpd('+idx+',\'draw\',this.checked?1:0)"> 图文动态</label>'
  +'<label><input type="checkbox" '+(s.live?'checked':'')+' onchange="__bsUpd('+idx+',\'live\',this.checked?1:0)"> 直播开播</label></div>'
  +'<div class="k" style="font-size:12px;margin-bottom:3px">推送目标（群+私聊，点击增减）：</div>'
  +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px" id="bs-g-'+idx+'">'+chips+ownG+ownP+'</div>'
  +'<div style="display:flex;gap:6px;align-items:center"><input id="bs-mg-'+idx+'" placeholder="加群号/QQ号（回车）" style="width:150px;padding:3px 8px" onkeydown="if(event.key===\'Enter\'){__bsAddGroup('+idx+',this.value)}"></div></div>';
 }).join('');
}
function __bsSubChip(s,type,v,label){
 var list=type==='g'?(s.groups||[]):(s.privates||[]);
 var on=list.indexOf(String(v))>=0;
 return '<span class="k bs-sg" onclick="__bsToggleSub('+__BS.subs.indexOf(s)+',\''+type+'\',\''+esc(String(v))+'\')" style="padding:2px 10px;border:1px solid '+(on?'var(--acc,#4f7cff)':'var(--line)')+';border-radius:14px;cursor:pointer;font-size:12.5px;'+(on?'background:rgba(79,124,255,.12)':'')+'">'+esc(label)+'</span>';
}
async function __bsUpd(idx,key,val){__BS.subs[idx][key]=val;await __bsSaveSub(idx);}
async function __bsToggleSub(idx,type,v){
 var s=__BS.subs[idx];var list=type==='g'?(s.groups||[]):(s.privates||[]);
 var i=list.indexOf(String(v));
 if(i>=0)list.splice(i,1);else list.push(String(v));
 if(type==='g')s.groups=list;else s.privates=list;
 await __bsSaveSub(idx);
}
async function __bsAddGroup(idx,v){v=(v||'').replace(/\D/g,'');if(!v)return;var s=__BS.subs[idx];
 var isF=Object.keys(__BS.friends).indexOf(v)>=0;
 var type=(v.length>=5&&__BS.friends[v])||isF?'p':'g';
 var list=type==='p'?(s.privates||[]):(s.groups||[]);
 if(list.indexOf(v)<0)list.push(v);
 if(type==='p')s.privates=list;else s.groups=list;
 var inp=document.getElementById('bs-mg-'+idx);if(inp)inp.value='';
 await __bsSaveSub(idx);}
async function __bsSaveSub(idx){
 var s=__BS.subs[idx];
 await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_update',args:{uid:s.uid,av:s.av,draw:s.draw,live:s.live,groups:s.groups||[],privates:s.privates||[]}})}).catch(()=>null);
 __bsRenderList();
}
async function __bsDel(idx){
 var s=__BS.subs[idx];
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'sub_del',args:{uid:s.uid}})}).catch(()=>({ok:false}));
 if(r&&r.ok){__BS.subs.splice(idx,1);__bsRenderList();}
}
async function __bsTest(idx){
 var s=__BS.subs[idx];var g=(s.groups||[])[0];var p=(s.privates||[])[0];
 if(!g&&!p){toast('先给这个订阅选一个推送目标',false);return;}
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'test_push',args:g?{group:g}:{private:p}})}).catch(()=>({ok:false}));
 toast(r&&r.ok?('测试消息已发（'+(g?('群 '+g):('私聊 '+p))+'）'):('失败：'+(r&&r.error||'未知')),!!(r&&r.ok));
}
// ---------------- 推送历史 + 手动重推 ----------------
function __bsRenderHist(){
 var host=document.getElementById('bs-hist');if(!host)return;
 if(!__BS.hist.length){host.innerHTML='<div class="k">暂无推送记录——有新内容推送后这里可查、可重推。</div>';return;}
 host.innerHTML=__BS.hist.map(function(e){
  var t=new Date((e.ts||0)*1000);
  var badges=Object.keys(e.ok||{}).map(function(k){
   var good=e.ok[k];var m=k.match(/^(group|private):(\d+)$/);
   var label=m?(m[1]==='group'?'群':'QQ')+m[2]:k;
   return '<span class="k" style="font-size:11px;padding:1px 7px;border-radius:10px;border:1px solid '+(good?'var(--ok,#2e7d32)':'#c62828')+';color:'+(good?'var(--ok,#2e7d32)':'#c62828')+'">'+esc(label)+(good?'✓':'✗')+'</span>';
  }).join(' ');
  var kindTxt={av:'视频',draw:'图文',live:'直播'}[e.kind]||e.kind;
  return '<div style="border:1px solid var(--line);border-radius:8px;padding:7px;margin:6px 0">'
  +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
  +'<span class="k mono" style="font-size:11px">'+t.toLocaleString()+'</span>'
  +'<b style="font-size:12.5px">'+esc(e.name||e.uid)+'</b>'
  +'<span class="k" style="font-size:11px">['+kindTxt+(e.imgs&&e.imgs.length?('+'+e.imgs.length+'图'):'')+']</span>'
  +(e.repush?'<span class="k" style="font-size:11px">已重推'+e.repush+'次</span>':'')
  +'<button class="act" style="margin:0 0 0 auto;padding:2px 12px;font-size:12px" onclick="__bsRepushDlg('+e.hid+')">重推</button></div>'
  +'<div class="k" style="font-size:12px;margin-top:4px;white-space:pre-wrap;max-height:52px;overflow:hidden">'+esc((e.text||'').slice(0,150))+'</div>'
  +'<div style="margin-top:4px">'+badges+'</div></div>';
 }).join('');
}
function __bsRepushDlg(hid){
 var e=null;__BS.hist.forEach(function(x){if(x.hid===hid)e=x;});
 if(!e){toast('记录不存在',false);return;}
 var failed=Object.keys(e.ok||{}).filter(function(k){return !e.ok[k];});
 var gOpts=Object.keys(__BS.wl).map(function(g){var k='group:'+g;var sel=failed.indexOf(k)>=0?' checked':'';
  return '<label class="k" style="font-size:12.5px"><input type="checkbox" class="bs-rp" value="g:'+esc(g)+'"'+sel+'> '+esc(__BS.wl[g]||g)+'（群）</label>';}).join(' ');
 var pOpts=Object.keys(__BS.friends).slice(0,30).map(function(u){var k='private:'+u;var sel=failed.indexOf(k)>=0?' checked':'';
  return '<label class="k" style="font-size:12.5px"><input type="checkbox" class="bs-rp" value="p:'+esc(u)+'"'+sel+'> '+esc(__BS.friends[u]||u)+'（私聊）</label>';}).join(' ');
 var m=_dlgBuild('重新推送','选择重推目标（默认勾选上次失败的目标）：'
  +'<div id="bs-rp-list" style="max-height:220px;overflow:auto;margin-top:6px;display:flex;flex-direction:column;gap:4px">'+gOpts+pOpts+'</div>'
  +'<div style="display:flex;gap:6px;align-items:center;margin-top:6px"><input id="bs-rp-manual" placeholder="手动加群号/QQ号（回车添加）" style="flex:1;padding:3px 8px"><span class="k" style="font-size:11px">5-6位按群，≥7位且非白名单群按私聊</span></div>',
  false,'重推',false);
 // 手动接管按钮（不用 _dlgWire：它先关窗再回调，checkbox DOM 已移除读不到；
 // 且校验失败要保持弹窗——复杂表单手动接管是既定规矩）
 var okBtn=m.querySelector('.dlg-btns .act:not(.ghost)');
 m.querySelector('.dlg-btns .ghost').onclick=function(){m.remove()};
 okBtn.onclick=function(){
  var g=[],p=[];
  m.querySelectorAll('.bs-rp:checked').forEach(function(c){
   if(c.value.indexOf('g:')===0)g.push(c.value.slice(2));else p.push(c.value.slice(2));});
  if(!g.length&&!p.length){toast('至少选一个目标',false);return;}
  m.remove();__bsRepushDo(hid,g,p);};
 m.onkeydown=function(ev){if(ev.key==='Escape')m.remove()};
 m.onclick=function(ev){if(ev.target===m)m.remove()};
 var man=m.querySelector('#bs-rp-manual');
 man.onkeydown=function(ev){if(ev.key!=='Enter')return;
  var v=(man.value||'').replace(/\D/g,'');if(!v)return;
  var isP=v.length>=5&&v.length<=10&&!__BS.wl[v];
  var lab=document.createElement('label');lab.className='k';lab.style.cssText='font-size:12.5px';
  lab.innerHTML='<input type="checkbox" class="bs-rp" value="'+(isP?'p:':'g:')+esc(v)+'" checked> '+esc(v)+(isP?'（私聊）':'（群）');
  m.querySelector('#bs-rp-list').appendChild(lab);man.value='';};
 okBtn.focus();
}
async function __bsRepushDo(hid,g,p){
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'repush',args:{hid:hid,groups:g,privates:p}})}).catch(()=>({ok:false}));
 if(r&&r.ok){toast('已重推：'+Object.keys(r.results||{}).map(function(k){return k+(r.results[k]?'✓':'✗');}).join(' '),true);
  var st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'history'})}).catch(()=>null);
  if(st&&st.ok){__BS.hist=(st.history||[]).slice().reverse();__bsRenderHist();}
 } else toast('重推失败：'+(r&&r.error||'未知'),false);
}
async function __bsSetInterval(){
 var v=parseInt(document.getElementById('bs-interval').value,10);
 var msg=document.getElementById('bs-intmsg');
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'bili_sub',method:'set_interval',args:{interval:v}})}).catch(()=>({ok:false}));
 msg.textContent=r&&r.ok?'✓ 已保存':'✗ '+(r&&r.error||'失败');
}
