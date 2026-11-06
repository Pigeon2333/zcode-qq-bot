// style_learn 插件设置页：学习设置 / 风格库（手动 CRUD）/ 消耗估算
// 注意：宿主规则——顶层禁 let/const（用 var/function）；'+...' 字符串里
// 内联事件的单引号必须 \' 转义（整页 JS 语法错误的教训）。
async function styleLearnPage(d){
 var PC=function(m,a){return api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:m,args:a||{}})}).catch(function(){return{ok:false,error:'请求失败'}})};
 var st=await PC('load_cfg');
 var cfg=(st&&st.ok)?(st.cfg||{}):{};
 var est=(st&&st.ok)?(st.estimate||{}):{};
 var stat=await PC('status');
 var groups=((stat&&stat.ok)?(stat.groups||[]):[]);
 var recs=(await PC('records')).records||[];
 var pct=(est.pct||0);
 var html='<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
  +'<b>这是什么</b><span class="badge" style="background:var(--tint);color:var(--dim)">目的：活人感 · 不是模仿任何人</span>'
  +(pct?'<span class="badge" style="background:#fff4e0;color:#b25e09">预计消耗 +'+pct+'%</span>':'')
  +'</div>'
  +'<div class="k">机器人在你指定的群里<b>旁观</b>群友聊天（只看不说话，未触发/非白名单群的消息也能学），攒够样本后由 AI 蒸馏出这群人的「活人感特征」（句长/语气词/标点/表情梗/接话方式等共性），注入她的上下文。她学到的是<b>真人怎么说话的质感</b>，不是某个人的语气——样本全程去标识（不存昵称），蒸馏明确禁止提取个人特征。</div>'
  +'<div class="k" style="margin-top:6px">⚠ 涉及分析群成员消息内容，默认关闭；确认无隐私顾虑再开。每次蒸馏约 1.0-1.5k token（每群至多 3 小时一次）；注入块当前约 '+(est.inject_tokens||0)+' token/条回复（按典型上下文 4500 估算 = +'+pct+'%）。</div></div>'

  +'<div class="card"><label class="master"><input type="checkbox" id="sl-on" '+(cfg.enabled?'checked':'')+' onchange="slSave()"> <b>启用学习</b><div class="k" style="margin-top:3px">关闭仅停止采集与蒸馏，已学风格仍在风格库里（可手动删）</div></label>'
  +'<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line)"><div class="k" style="margin-bottom:4px"><b>学习对象</b></div>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slmode" value="all" '+(cfg.mode!=='members'&&cfg.mode!=='blacklist'?'checked':'')+' onchange="slMode()"> 全员——无差别学习所有群友</label>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slmode" value="members" '+(cfg.mode==='members'?'checked':'')+' onchange="slMode()"> 仅指定成员（白名单）</label>'
  +'<div id="sl-members-row" class="dimmed" style="margin:4px 0 4px 24px"><input id="sl-members" placeholder="QQ 号，逗号/换行分隔" style="width:100%" value="'+esc((cfg.members||[]).join(', '))+'"></div>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slmode" value="blacklist" '+(cfg.mode==='blacklist'?'checked':'')+' onchange="slMode()"> 黑名单之外——学习除这些人的所有群友</label>'
  +'<div id="sl-blacklist-row" class="dimmed" style="margin:4px 0 4px 24px"><input id="sl-blacklist" placeholder="QQ 号，逗号/换行分隔（可排除群里的其他 AI）" style="width:100%" value="'+esc((cfg.blacklist||[]).join(', '))+'"></div></div>'

  +'<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)"><div class="k" style="margin-bottom:4px"><b>喂给谁</b>（活人感参考注入谁的上下文——三选一）</div>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slscope" value="all" '+(cfg.scope==='all'||!cfg.scope?'checked':'')+' onchange="slScope()"> <b>全部人设</b>——所有人格都收到这份参考（含以后新建的）</label>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slscope" value="persona" '+(cfg.scope==='persona'?'checked':'')+' onchange="slScope()"> <b>仅指定人设</b>——从下面的人设卡里勾选，其他人设完全不受影响</label>'
  +'<div id="sl-personas-row" class="dimmed" style="margin:4px 0 4px 24px"><div id="sl-plist" class="k">加载人设列表…</div></div>'
  +'<label style="display:block;margin:4px 0"><input type="radio" name="slscope" value="global" '+(cfg.scope==='global'?'checked':'')+' onchange="slScope()"> <b>仅全局通用设定</b>——挂在全局底子上，所有人格共享（效果同「全部人设」，语义上是通用底子的一部分，人设切换不影响）</label>'
  +'<div class="k" style="margin-top:4px">与「精简上下文」不冲突：这份参考属于人设/风格类提示（永不裁剪），精简只裁聊天历史和记忆；每个群最多注入一条且硬上限 320 字（约 190 token），不会撑爆上下文。</div></div>'

  +'<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)"><div class="k" style="margin-bottom:4px"><b>学习群</b>（独立于群消息白名单——可以让机器人去没配置说话的群只旁观学习）</div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><input id="sl-groups" placeholder="群号，逗号/换行分隔" style="flex:1;min-width:240px" value="'+esc((cfg.groups||[]).join(', '))+'">'
  +'<button class="act ghost" style="margin:0" onclick="slSync()">从群消息白名单同步（合并）</button>'
  +'<button class="act ghost" style="margin:0" onclick="slSync(1)">整表替换为白名单</button></div>'
  +'<div class="k" id="sl-wlnames" style="margin-top:4px"></div>'
  +'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px"><span class="k">每攒 <input id="sl-min" type="number" min="20" max="500" style="width:60px" value="'+esc(String(cfg.learn_min||60))+'"> 条新消息学一次（每群至多 3 小时一次）</span>'
  +'<label class="k"><input type="checkbox" id="sl-inj" '+(cfg.inject_on!==false?'checked':'')+'> 学到的活人感注入她的回复上下文</label></div>'
  +'<div style="margin-top:8px;display:flex;gap:8px;align-items:center"><button class="act" style="margin:0" onclick="slSave()">保存设置</button><span class="k" id="sl-msg"></span></div></div>'

  +'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px"><b>学习状态</b>（点「立刻学」可在样本够时马上蒸馏）</div><div id="sl-statbox"></div></div></div>'

  +'<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><b>风格库</b><span class="badge" style="background:var(--tint);color:var(--dim)">'+recs.length+' 条</span><span class="k">——每群一条活人感特征；可手动新增/编辑/删除，清理无用的风格</span></div>'
  +'<div id="sl-recs"></div>'
  +'<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)"><div class="k" style="margin-bottom:4px">手动新增（想自己写一条活人感参考也行）</div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap"><input id="sl-ngid" placeholder="群号" style="width:120px"><input id="sl-ntraits" placeholder="特征，每行一条（- 开头可省）" style="flex:1;min-width:220px"><button class="act" style="margin:0" onclick="slRecAdd()">新增</button></div></div></div>';

 // ---- 渲染（return 前拼好；事件处理函数全在全局） ----
 var sb='';
 groups.forEach(function(g){
  sb+='<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0;padding:6px 8px;border:1px solid var(--line);border-radius:6px">'
   +'<b class="mono">'+esc(g.gid)+'</b>'+(g.gname?'<span class="k">'+esc(g.gname)+'</span>':'')
   +'<span class="k">已缓冲 '+g.buffered+' 条（新 '+g.new+'/'+g.learn_min+'）</span>'
   +(g.last_learn?'<span class="k">上次学习 '+new Date(g.last_learn*1000).toTimeString().slice(0,5)+'</span>':'')
   +'<span style="margin-left:auto"><button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="slLearnNow(\''+esc(g.gid)+'\')">立刻学</button></span></div>';
 });
 html=html.replace('<div id="sl-statbox"></div>',sb||'<div class="k">还没配置学习群</div>');
 var rb='';
 recs.forEach(function(r,i){
  var lines=String(r.traits||'').split('\n').map(function(l){return l.replace(/^- /,'')}).join('\n');
  rb+='<div style="border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0">'
   +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b class="mono">'+esc(r.gid)+'</b>'
   +(r.gname?'<span class="k">'+esc(r.gname)+'</span>':'')
   +(r.manual?'<span class="badge" style="background:#fff4e0;color:#b25e09">手动</span>':'<span class="badge" style="background:#e6f7e6;color:#1a7f37">学习 '+(r.samples||0)+'样本</span>')
   +'<span class="k">'+(r.updated?new Date(r.updated*1000).toLocaleDateString():'')+'</span>'
   +'<span style="margin-left:auto;display:flex;gap:6px"><button class="act" style="margin:0;padding:3px 10px;font-size:12px" onclick="slRecSave('+i+')">保存</button>'
   +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="slRecDel('+i+')">删除</button></span></div>'
   +'<textarea id="sl-traits-'+i+'" rows="4" style="width:100%;margin-top:6px;font-size:12.5px">'+esc(lines)+'</textarea></div>';
 });
 html=html.replace('<div id="sl-recs"></div>',rb||'<div class="k">暂无风格记录——配置学习群攒样本，或用下方手动新增</div>');

 setTimeout(function(){
  slMode();slScope();
  var paint=function(r){
   var el=document.getElementById('sl-wlnames');
   if(!el)return;
   if(r&&r.ok&&r.groups&&r.groups.length){
    el.textContent='群消息白名单：'+r.groups.map(function(g){return String(g.name||g.id)}).join('、');
   }else{el.textContent='（群消息白名单为空或读取失败）'}
  };
  if(window._SL_WL){paint(window._SL_WL);return}
  PC('wl_groups').then(function(r){window._SL_WL=r;paint(r)});
  // 人设卡列表（喂给谁）：勾选式多选（只列人设卡；全局是平级 radio 不在此列）
  PC('persona_names').then(function(r){
   var box=document.getElementById('sl-plist');if(!box)return;
   if(!r||!r.ok){box.textContent='人设列表读取失败';return}
   var chosen=(cfg.personas||[]);
   box.innerHTML=(r.personas||[]).map(function(n){
    return '<label style="display:inline-flex;gap:5px;align-items:center;margin:3px 10px 3px 0"><input type="checkbox" class="sl-pc" value="'+esc(n)+'" '+(chosen.indexOf(n)>=0?'checked':'')+'> '+esc(n)+(n===r.active?'（当前激活）':'')+'</label>';
   }).join('')||'（还没有任何人设卡——先到「人设」页创建）';
  });
 },0);
 return html;
}
var _SL_WL=null;
function slMode(){
 var m=(document.querySelector('input[name="slmode"]:checked')||{}).value||'all';
 var a=document.getElementById('sl-members-row'),b=document.getElementById('sl-blacklist-row');
 if(a)a.classList.toggle('dimmed',m!=='members');
 if(b)b.classList.toggle('dimmed',m!=='blacklist');
}
function slScope(){
 var s=(document.querySelector('input[name="slscope"]:checked')||{}).value||'all';
 var row=document.getElementById('sl-personas-row');
 if(row)row.classList.toggle('dimmed',s!=='persona');
}
async function slSave(){
 var msg=document.getElementById('sl-msg');msg.textContent='保存中…';
 var personas=[].slice.call(document.querySelectorAll('.sl-pc:checked')).map(function(x){return x.value});
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'save_cfg',args:{
  enabled:document.getElementById('sl-on').checked,
  inject_on:document.getElementById('sl-inj').checked,
  mode:(document.querySelector('input[name="slmode"]:checked')||{}).value||'all',
  members:document.getElementById('sl-members').value,
  blacklist:document.getElementById('sl-blacklist').value,
  groups:document.getElementById('sl-groups').value,
  learn_min:parseInt(document.getElementById('sl-min').value)||60,
  scope:(document.querySelector('input[name="slscope"]:checked')||{}).value||'all',
  personas:personas
 }})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(!r||r.ok===false){msg.textContent='保存失败：'+((r&&r.error)||'');return}
 var pct=(r.estimate&&r.estimate.pct)||0;
 msg.textContent='已保存 ✓ 预计消耗 +'+pct+'%（注入 '+(r.estimate.inject_tokens||0)+' token/条回复）';
}
async function slSync(replace){
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'wl_sync',args:{replace:!!replace}})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(!r||r.ok===false){toast('同步失败：'+((r&&r.error)||''),false);return}
 document.getElementById('sl-groups').value=(r.groups||[]).join(', ');
 toast('已从群消息白名单同步（共 '+((r.groups||[]).length)+' 群）；记得点「保存设置」');
}
async function slLearnNow(gid){
 toast('蒸馏中…（约几秒，样本越多越准）');
 var r=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'learn_now',args:{gid:gid}})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(!r||r.ok===false){toast(((r&&r.error)||'失败'),false);return}
 toast('蒸馏完成，刷新页面看风格库');
 setTimeout(function(){plPage('style_learn')},1500);
}
async function slRecSave(i){
 var v=document.getElementById('sl-traits-'+i).value.trim();
 var recs=(await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'records'})}).catch(function(){return{records:[]}})).records||[];
 var r=recs[i]||{};
 var traits=v.split('\n').map(function(l){l=l.trim();return l?('- '+l.replace(/^- /,'')):null}).filter(Boolean).join('\n');
 var out=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'record_save',args:{gid:r.gid,gname:r.gname,traits:traits,samples:r.samples,created:r.created}})}).catch(function(){return{ok:false,error:'请求失败'}});
 toast(out&&out.ok?'已保存':'保存失败：'+((out&&out.error)||''),out&&out.ok);
}
async function slRecDel(i){
 var recs=(await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'records'})}).catch(function(){return{records:[]}})).records||[];
 var r=recs[i]||{};
 if(!await dlgConfirm({title:'删除「'+(r.gname||r.gid)+'」的风格记录？',body:'删除后该群不再注入活人感参考（学习不受影响，攒够样本会重新蒸馏）。',danger:true,okText:'删除'}))return;
 var out=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'record_del',args:{id:r.id}})}).catch(function(){return{ok:false}});
 toast(out&&out.ok?'已删除':'删除失败',out&&out.ok);
 plPage('style_learn');
}
async function slRecAdd(){
 var gid=document.getElementById('sl-ngid').value.trim();
 var t=document.getElementById('sl-ntraits').value.trim();
 if(!gid||!t){toast('群号和特征都要填',false);return}
 var traits=t.split('\n').map(function(l){l=l.trim();return l?('- '+l.replace(/^- /,'')):null}).filter(Boolean).join('\n');
 var out=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'style_learn',method:'record_save',args:{gid:gid,traits:traits}})}).catch(function(){return{ok:false,error:'请求失败'}});
 if(!out||out.ok===false){toast('新增失败：'+((out&&out.error)||''),false);return}
 toast('已新增');
 plPage('style_learn');
}
