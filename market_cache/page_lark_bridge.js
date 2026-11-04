// 本文件随插件 zip 分发（宿主有同版 fallback）；数据端点在宿主
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
