// voice_reply 插件设置页（随插件包分发；引擎与 /api/voice/* 端点在宿主，旧宿主有 fallback 版）
// 插件页契约：page 函数接收宿主预取 data、返回 HTML 字符串，不自写 #view
var __VRVM=null;
function voicePage(d){
 __VRVM=(d&&d.providers)?d:(__VRVM||{config:{}});
 const P=__VRVM.providers||{};
 const c=__VRVM.config||{};
 const prov=Object.keys(P);
 const cur=c.provider||'edge';
 const vopts=prov.map(k=>'<option value="'+k+'" '+(k===cur?'selected':'')+'>'+esc(P[k].label)+'</option>').join('');
 const edgeOpts=(__VRVM.edge_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.edge_voice||'zh-CN-XiaoxiaoNeural')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const zzOpts=(__VRVM.zhipu_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.zhipu_voice||'tongtong')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const sfMOpts=(__VRVM.sf_models||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.sf_model||'FunAudioLLM/cosyvoice2-0.5B')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const sfVOpts=(__VRVM.sf_voices||[]).map(v=>'<option value="'+esc(v)+'" '+(v===(c.sf_voice||'anna')?'selected':'')+'>'+esc(v)+'</option>').join('');
 const mode=String(c.mode||'keyword');
 const kw=(c.keywords||[]).join('、');
 const html='<div class="k" style="margin-bottom:10px">让机器人开口说话：语音内容跟人设走，只是换成语音发送。配置即时生效。</div>'
 +'<div class="card"><div class="k" style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><b>开关与策略</b><span class="badge" style="background:var(--tint);color:var(--dim)">语音内容跟人设走，只换发送形式</span></div>'
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
 +'<input id="v-kw" value="'+esc(kw)+'" placeholder="触发词，用、分隔（如：念一下、发语音）" style="flex:1;min-width:220px">'
 +'<label class="k">语音概率 <input id="v-prob" type="number" min="0" max="100" step="5" value="'+esc(String(Math.round((c.prob||0)*100)))+'" style="width:64px"> %</label><span class="k">（触发词必语音；其余消息按此概率语音回复）</span></div>'
 +'<div style="margin-top:10px"><button class="act" style="margin:0" onclick="voiceSave()">保存设置</button><span class="k" id="v-ok" style="margin-left:8px"></span></div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:8px"><b>语音来源与音色</b>（供应商可随时换，互不影响）</div>'
 +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="k">供应商</span><select id="v-prov" onchange="voiceProvChange()">'+vopts+'</select></div>'
 +'<div class="k" id="v-bill" style="margin-top:8px;padding:8px;border:1px solid var(--line);border-radius:8px"></div>'
 +'<div id="v-provbox" style="margin-top:10px"></div>'
 +'<div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">'
 +'<input id="v-ptext" value="你好呀，我是你的语音小助手，听听这个声音怎么样？" style="flex:1;min-width:240px">'
 +'<button class="act ghost" style="margin:0" onclick="voicePreview()">试听（用当前表单配置）</button></div>'
 +'<audio id="v-audio" controls style="margin-top:8px;width:100%;display:none"></audio>'
 +'<div class="k" id="v-pmsg" style="margin-top:6px"></div></div>'
 +'<div class="card"><div class="k"><b>计费说明</b>（各家语音合成的收费情况，选前看清楚）</div>'
 +prov.map(k=>'<div style="margin-top:8px"><b>'+esc(P[k].label)+'</b><div class="k" style="margin-top:3px">'+esc(P[k].billing)+'</div></div>').join('')
 +'<div class="k" style="margin-top:10px">克隆专属音色（智谱，收费）：在 bigmodel.cn 控制台或开放平台用 3 秒以上人声样本创建复刻音色，把得到的 speaker_id 填到智谱栏即可——之后所有语音都用这个音色。</div></div>';
 // 必须在 return 之前调度（写在 return 后是死代码，永远不会执行——
 // 「首次进入智谱设置区空白、要切走再切回来才出现」就是它）
 setTimeout(voiceProvChange,0);
 return html;
}
function voiceBill(){const P=__VRVM.providers||{};const k=document.getElementById('v-prov').value;const el=document.getElementById('v-bill');el.textContent=P[k]?P[k].billing:''}
function voiceProvChange(){
 voiceBill();
 const c=__VRVM.config||{};const k=document.getElementById('v-prov').value;
 const box=document.getElementById('v-provbox');
 if(k==='edge'){box.innerHTML='<div class="k" style="margin-bottom:4px">音色（微软公共接口，免费）</div><select id="v-ev">'+edgeOptsHtml()+'</select>'}
 else if(k==='zhipu'){box.innerHTML='<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">预设音色</div><select id="v-zv">'+zzOptsHtml()+'</select></div>'
  +'<div><div class="k" style="margin-bottom:4px">克隆音色 speaker_id（填了优先生效）</div><input id="v-zsid" value="'+esc(String(c.zhipu_speaker_id||''))+'" placeholder="复刻得到的 speaker_id"></div>'
  +'<div style="grid-column:1/-1"><div class="k" style="margin-bottom:4px">智谱 API Key（留空 = 复用模型管理里的智谱 Key）</div><input id="v-zkey" type="password" value="'+esc(String(c.zhipu_key||''))+'" placeholder="留空自动复用"></div>'
  +'<div style="grid-column:1/-1;border:1px dashed var(--line);border-radius:8px;padding:10px">'
  +'<div class="k" style="margin-bottom:6px"><b>一键克隆音色</b> · 选文件 → 自动截取 → 上传克隆 → 自动填入 → 试听</div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
  +'<input type="file" id="v-file" accept="audio/*,video/*" style="flex:1;min-width:180px">'
  +'<label class="k">起始 <input id="v-start" type="number" min="0" step="1" value="0" style="width:58px"> 秒</label>'
  +'<label class="k">时长 <input id="v-dur" type="number" min="3" max="30" step="1" value="12" style="width:52px"> 秒</label>'
  +'<input id="v-vname" placeholder="音色名（如 爱音）" style="width:130px">'
  +'<button class="act" style="margin:0" onclick="voiceClone()">开始克隆</button></div>'
  +'<input id="v-sample" placeholder="可选：这段音频里说的话（填了克隆更像）" style="width:100%;margin-top:6px">'
  +'<div class="k" id="v-cmsg" style="margin-top:6px">支持 mp4/mov/mp3/m4a/wav；页面自动截取所选片段转 16kHz 单声道 WAV 再上传（智谱限 3-30 秒、≤10MB）。克隆与合成都计费。</div></div>'
  +'<div style="grid-column:1/-1;border:1px dashed var(--line);border-radius:8px;padding:10px">'
  +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><b>已克隆音色</b>'
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="voiceVoicesLoad()">刷新列表</button>'
  +'<span class="k" id="v-vlmsg"></span></div>'
  +'<div id="v-vlist" class="k">（点「刷新列表」拉取本账号的复刻音色）</div></div></div>'}
  else if(k==='siliconflow'){box.innerHTML='<div style="display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">硅基流动 API Key</div><input id="v-sfk" type="password" value="'+esc(String(c.sf_key||''))+'"></div>'
  +'<div><div class="k" style="margin-bottom:4px">模型</div><select id="v-sfm">'+sfMOptsHtml()+'</select></div>'
  +'<div><div class="k" style="margin-bottom:4px">音色</div><select id="v-sfv">'+sfVOptsHtml()+'</select></div></div>'}
 else{box.innerHTML='<div style="display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">接口地址（https，含 /v1/）</div><input id="v-cb" value="'+esc(String(c.custom_base||''))+'" placeholder="https://api.openai.com/v1/"></div>'
  +'<div><div class="k" style="margin-bottom:4px">API Key</div><input id="v-ck" type="password" value="'+esc(String(c.custom_key||''))+'"></div>'
  +'<div><div class="k" style="margin-bottom:4px">模型</div><input id="v-cm" value="'+esc(String(c.custom_model||'tts-1'))+'"></div>'
  +'<div><div class="k" style="margin-bottom:4px">音色</div><input id="v-cv" value="'+esc(String(c.custom_voice||'alloy'))+'"></div></div>'}
}
function edgeOptsHtml(){return(__VRVM.edge_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((__VRVM.config||{}).edge_voice||'zh-CN-XiaoxiaoNeural')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function zzOptsHtml(){return(__VRVM.zhipu_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((__VRVM.config||{}).zhipu_voice||'tongtong')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfMOptsHtml(){return(__VRVM.sf_models||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((__VRVM.config||{}).sf_model||'FunAudioLLM/cosyvoice2-0.5B')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfVOptsHtml(){return(__VRVM.sf_voices||[]).map(v=>'<option value="'+esc(v)+'" '+(v===((__VRVM.config||{}).sf_voice||'anna')?'selected':'')+'>'+esc(v)+'</option>').join('')}
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
 v.edge_voice=g('v-ev');v.zhipu_voice=g('v-zv');v.zhipu_speaker_id=g('v-sid')||g('v-zsid');
 // Key fallback：表单字段可能还没渲染（首次加载 zhipu 表单区时序问题），
 // 读不到就用已保存的配置值（用户已保存过 Key 的话）
 v.zhipu_key=g('v-zkey')||((__VRVM.config||{}).zhipu_key||'');
 v.sf_key=g('v-sfk')||((__VRVM.config||{}).sf_key||'');
 v.sf_model=g('v-sfm');v.sf_voice=g('v-sfv');
 v.custom_base=g('v-cb')||((__VRVM.config||{}).custom_base||'');
 v.custom_key=g('v-ck')||((__VRVM.config||{}).custom_key||'');
 v.custom_model=g('v-cm');v.custom_voice=g('v-cv');
 return v}
async function voiceSave(){
 const r=await api('/api/voice/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect()})}).catch(()=>({ok:false,error:'请求失败'}));
 const el=document.getElementById('v-ok');
 if(r.ok===false){toast(r.error||'保存失败',false);if(el)el.textContent='保存失败：'+(r.error||'');return}
 __VRVM.config=r.config||voiceCollect();toast('语音设置已保存（即时生效）');if(el)el.textContent='已保存 ✓'}
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
async function voiceClone(){
 const msg=document.getElementById('v-cmsg');
 const f=(document.getElementById('v-file').files||[])[0];
 if(!f){msg.textContent='先选一个音频或视频文件';return}
 if(f.size>120*1024*1024){msg.textContent='文件太大（超过 120MB），请先剪出 10-30 秒再上传';return}
 const start=Math.max(0,parseFloat(document.getElementById('v-start').value)||0);
 const dur=Math.min(30,Math.max(3,parseFloat(document.getElementById('v-dur').value)||12));
 const name=(document.getElementById('v-vname').value||'').trim();
 if(!name){msg.textContent='给这个音色起个名字（如 爱音）';return}
 msg.textContent='① 解码音频中…（长视频需要几秒）';
 let wav;
 try{
  const buf=await f.arrayBuffer();
  const Ctx=window.AudioContext||window.webkitAudioContext;
  const ac=new Ctx();
  const ab=await ac.decodeAudioData(buf);
  if(ac.close)ac.close();
  if(!ab||!ab.duration){msg.textContent='解码失败：浏览器读不出这段音频';return}
  if(ab.duration<start+1){msg.textContent='起始位置超出音频时长（该文件约 '+Math.round(ab.duration)+' 秒）';return}
  msg.textContent='② 截取 '+dur+' 秒并转 WAV…';
  wav=await vcWav(ab,start,dur);
 }catch(e){msg.textContent='音频解码失败：'+((e&&e.message)||e)+'。换 mp3/wav/m4a 试试，或先用工具剪一段。';return}
 msg.textContent='③ 上传并克隆中…（约 10-30 秒，按智谱计费）';
 const r=await api('/api/voice/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect(),audio:vcB64(wav),voice_name:name,sample_text:document.getElementById('v-sample').value,preview_text:'你好呀，我是你的语音助手，听听这个声音怎么样？'})}).catch(()=>({ok:false,error:'请求失败（克隆需要 0.4.12 及以上版本的应用）'}));
 if(!r||r.ok===false){msg.textContent='克隆失败：'+((r&&r.error)||'未知错误');return}
 const sid=document.getElementById('v-zsid');if(sid)sid.value=r.voice_id;
 let renamed='';
 msg.textContent='✓ 克隆完成（'+r.voice_id+'）'+renamed+'，已填入并保存。点上面「试听」听听效果。';
 await voiceSave();
}
// ---- 已克隆音色管理：列表 / 一键填入 / 试听 / 删除 ----
async function voiceVoicesLoad(){
 const box=document.getElementById('v-vlist');const m=document.getElementById('v-vlmsg');
 if(!box)return;
 m&&(m.textContent='拉取中…');
 const r=await api('/api/voice/voices',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 if(m)m.textContent='';
 if(!r||r.ok===false){box.innerHTML='拉取失败：'+esc((r&&r.error)||'');return}
 const vs=r.voices||[];
 if(!vs.length){
  box.innerHTML='还没有克隆音色（下面「一键克隆」一个，或去 bigmodel.cn 控制台创建）';
  return;
 }
 const cur=r.current||'';
 box.innerHTML=vs.map(v=>'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:5px 0;padding:7px;border:1px solid var(--line);border-radius:6px">'
  +'<b>'+esc(v.name||'(未命名)')+'</b>'
  +(v.id===cur?'<span class="badge" style="background:var(--tint);color:var(--dim)">当前使用</span>':'')
  +'<span class="k mono" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(v.id)+'">'+esc(v.id)+'</span>'
  +(v.created?'<span class="k">'+esc(v.created)+'</span>':'')
  +(v.url?'<a class="k" href="'+esc(v.url)+'" target="_blank">试听</a>':'')
  +'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:12px" onclick="voiceVoiceFill(\''+esc(v.id)+'\')">填入使用</button>'
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="voiceVoiceDel(\''+esc(v.id)+'\',\''+esc(v.name||v.id)+'\')">删除</button></div>').join('')
  +'<div class="k" style="margin-top:6px">删除不可恢复（平台侧真删）；删「当前使用」的音色会自动退回预设音色。</div>';
}
async function voiceVoiceFill(id){
 const sid=document.getElementById('v-zsid');
 if(sid)sid.value=id;
 await voiceSave();
 toast('已切换到音色 '+id.slice(0,13)+'…（即时生效）');
 voiceVoicesLoad();
}
async function voiceVoiceDel(id,name){
 if(!await dlgConfirm({title:'删除音色「'+name+'」？',body:'将从智谱平台永久删除（不可恢复）。删除后用这个音色的语音会改用预设音色。',danger:true,okText:'删除'}))return;
 const r=await api('/api/voice/voice-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice_id:id})}).catch(()=>({ok:false,error:'请求失败'}));
 if(!r||r.ok===false){toast('删除失败：'+((r&&r.error)||''),false);return}
 toast('已删除「'+name+'」');
 voiceVoicesLoad();
}
