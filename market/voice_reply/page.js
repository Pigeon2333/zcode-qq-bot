// voice_reply 插件设置页（随插件包分发；引擎与 /api/voice/* 端点在宿主，旧宿主有 fallback 版）
// 插件页契约：page 函数接收宿主预取 data、返回 HTML 字符串，不自写 #view
let _VM=null;
function voicePage(d){
 _VM=(d&&d.providers)?d:(_VM||{config:{}});
 const P=_VM.providers||{};
 const c=_VM.config||{};
 const prov=Object.keys(P);
 const cur=c.provider||'edge';
 const vopts=prov.map(k=>'<option value="'+k+'" '+(k===cur?'selected':'')+'>'+esc(P[k].label)+'</option>').join('');
 const edgeOpts=(_VM.edge_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.edge_voice||'zh-CN-XiaoxiaoNeural')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const zzOpts=(_VM.zhipu_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.zhipu_voice||'tongtong')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const sfMOpts=(_VM.sf_models||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===(c.sf_model||'FunAudioLLM/cosyvoice2-0.5B')?'selected':'')+'>'+esc(v[0])+'</option>').join('');
 const sfVOpts=(_VM.sf_voices||[]).map(v=>'<option value="'+esc(v)+'" '+(v===(c.sf_voice||'anna')?'selected':'')+'>'+esc(v)+'</option>').join('');
 const mode=String(c.mode||'keyword');
 const kw=(c.keywords||[]).join('、');
 return '<div class="k" style="margin-bottom:10px">让机器人开口说话：语音内容跟人设走，只是换成语音发送。配置即时生效。</div>'
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
 setTimeout(voiceProvChange,0);   // 等 plPage 把返回的 HTML 插进 DOM 后再填供应商区
}
function voiceBill(){const P=_VM.providers||{};const k=document.getElementById('v-prov').value;const el=document.getElementById('v-bill');el.textContent=P[k]?P[k].billing:''}
function voiceProvChange(){
 voiceBill();
 const c=_VM.config||{};const k=document.getElementById('v-prov').value;
 const box=document.getElementById('v-provbox');
 if(k==='edge'){box.innerHTML='<div class="k" style="margin-bottom:4px">音色（微软公共接口，免费）</div><select id="v-ev">'+edgeOptsHtml()+'</select>'}
 else if(k==='zhipu'){box.innerHTML='<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
  +'<div><div class="k" style="margin-bottom:4px">预设音色</div><select id="v-zv">'+zzOptsHtml()+'</select></div>'
  +'<div><div class="k" style="margin-bottom:4px">克隆音色 speaker_id（填了优先生效）</div><input id="v-zsid" value="'+esc(String(c.zhipu_speaker_id||''))+'" placeholder="复刻得到的 speaker_id"></div>'
  +'<div style="grid-column:1/-1"><div class="k" style="margin-bottom:4px">智谱 API Key（留空 = 复用模型管理里的智谱 Key）</div><input id="v-zkey" type="password" value="'+esc(String(c.zhipu_key||''))+'" placeholder="留空自动复用"></div></div>'}
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
function edgeOptsHtml(){return(_VM.edge_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).edge_voice||'zh-CN-XiaoxiaoNeural')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function zzOptsHtml(){return(_VM.zhipu_voices||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).zhipu_voice||'tongtong')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfMOptsHtml(){return(_VM.sf_models||[]).map(v=>'<option value="'+esc(v[1])+'" '+(v[1]===((_VM.config||{}).sf_model||'FunAudioLLM/cosyvoice2-0.5B')?'selected':'')+'>'+esc(v[0])+'</option>').join('')}
function sfVOptsHtml(){return(_VM.sf_voices||[]).map(v=>'<option value="'+esc(v)+'" '+(v===((_VM.config||{}).sf_voice||'anna')?'selected':'')+'>'+esc(v)+'</option>').join('')}
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
 v.edge_voice=g('v-ev');v.zhipu_voice=g('v-zv');v.zhipu_speaker_id=g('v-zsid');v.zhipu_key=g('v-zkey');
 v.sf_key=g('v-sfk');v.sf_model=g('v-sfm');v.sf_voice=g('v-sfv');
 v.custom_base=g('v-cb');v.custom_key=g('v-ck');v.custom_model=g('v-cm');v.custom_voice=g('v-cv');
 return v}
async function voiceSave(){
 const r=await api('/api/voice/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect()})}).catch(()=>({ok:false,error:'请求失败'}));
 const el=document.getElementById('v-ok');
 if(r.ok===false){toast(r.error||'保存失败',false);if(el)el.textContent='保存失败：'+(r.error||'');return}
 _VM.config=r.config||voiceCollect();toast('语音设置已保存（即时生效）');if(el)el.textContent='已保存 ✓'}
async function voicePreview(){
 const msg=document.getElementById('v-pmsg');const au=document.getElementById('v-audio');
 msg.textContent='合成中…（免费供应商约 1-3 秒）';au.style.display='none';
 const r=await api('/api/voice/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice:voiceCollect(),text:document.getElementById('v-ptext').value})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){msg.textContent='试听失败：'+(r.error||'');return}
 au.src=r.audio;au.style.display='block';au.play().catch(()=>{});
 msg.textContent='试听音频 '+((r.size||0)/1024).toFixed(0)+' KB（每次试听按该供应商计费规则计费）'}
