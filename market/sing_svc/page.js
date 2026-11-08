// sing_svc 插件设置页：整合包引导/引擎启停/模型导入管理/使用说明（随插件包分发）
async function singPage(d){
 const st=await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'sing_svc',method:'status'})}).catch(()=>({ok:false}));
 const cfg=(st&&st.cfg)||{};
 const eng=(st&&st.engine)||{};
 const ms=(st&&st.models)||[];
 const det=(st&&st.detected)||[];
 const engBadge=eng.running?'<span class="badge" style="background:#e6f7e6;color:#1a7f37">● 运行中'+(eng.model?('· '+esc(eng.model)):'')+'</span>'
  :'<span class="badge" style="background:var(--tint);color:var(--dim)">已停止</span>';
 const modelRow=m=>'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 10px;border:1.5px solid '+(cfg.default_model===m.id?'#1a7f37':'var(--line)')+';border-radius:8px;background:'+(cfg.default_model===m.id?'#f0faf0':'transparent')+'">'
  +'<b style="min-width:70px">'+esc(m.name)+'</b>'
  +(cfg.default_model===m.id?'<span class="badge" style="background:#1a7f37;color:#fff">✓ 默认音色</span>':'')
  +(m.ok?'':'<span class="k" style="color:var(--err)">⚠ 文件缺失</span>')
  +(m.spk?'<span class="k">说话人 '+esc(m.spk)+'</span>':'')
  +'<span style="margin-left:auto;display:flex;gap:6px">'
  +'<button class="act '+(cfg.default_model===m.id?'ghost':'')+'" style="margin:0;padding:3px 12px;font-size:12px" onclick="sgUse(\''+esc(m.id)+'\')">'+(cfg.default_model===m.id?'取消默认':'设为默认')+'</button>'
  +'<button class="act ghost danger" style="margin:0;padding:3px 10px;font-size:12px" onclick="sgDel(\''+esc(m.id)+'\',\''+esc(m.name)+'\')">删除</button></span></div>';
 const html=head('AI 翻唱：你唱一句，她用指定的音色唱回去（本机 GPU，零 API 成本）')
 +'<div class="card"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"><b>so-vits-svc 引擎</b>'+engBadge
  +(eng.running?'<button class="act ghost" style="margin:0;padding:4px 12px;font-size:12px" onclick="sgEngine(\'stop\')">停止</button>':'<button class="act" style="margin:0;padding:4px 12px;font-size:12px" onclick="sgEngine(\'start\')">启动引擎</button>')
  +'<button class="act ghost" style="margin:0;padding:4px 12px;font-size:12px" onclick="singPage()">刷新状态</button></div>'
  +'<div class="k" style="margin-bottom:6px">① 装 so-vits-svc 4.1 整合包（含 runtime 与 inference 的 Windows 一键包即可，不用能训练的完整版；模型推理只用显卡）。解压到任意盘后在下面填目录。</div>'
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
  +'<input id="sg-dir" placeholder="整合包目录（如 E:\\so-vits-svc-4.1）" value="'+esc(cfg.dir||'')+'" style="flex:1;min-width:240px">'
  +'<button class="act ghost" style="margin:0" onclick="sgPick()">浏览…</button>'
  +'<button class="act" style="margin:0" onclick="sgSaveDir()">保存目录</button>'
  +(det.length?(' <span class="k">检测到：'+det.map(x=>'<a href="javascript:void 0" onclick="sgUseDir(\''+esc(x)+'\')">'+esc(x.split('\\\\').pop().split('/').pop())+'</a>').join(' / ')+'（点击填入）</span>'):'')
  +'</div>'
  +'<div class="k" style="margin-top:6px">ffmpeg：'+((st&&st.ffmpeg)?'✓ 已找到（输入转码用）':'✗ 未找到（整合包一般自带；装 GPT-SoVITS 整合包也会自动复用）')+'</div>'
  +'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)" class="k">'
  +'变调 <input type="number" id="sg-tran" min="-24" max="24" value="'+(cfg.tran||0)+'" style="width:70px"> 半音'
  +'　最长 <input type="number" id="sg-max" min="15" max="300" value="'+(cfg.max_sec||120)+'" style="width:70px"> 秒'
  +'<button class="act ghost" style="margin:0;padding:4px 12px;font-size:12px" onclick="sgSaveTune()">保存</button>'
  +'<span>（变调用于整体升/降 key，如男声转女声 +12；清唱干声效果最好）</span></div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>翻唱音色（歌声模型）</b>——社区 so-vits-svc 模型包（含 G*.pth + config.json 的文件夹），导入后设默认即可用</div>'
  +(ms.length?'<div style="display:grid;gap:6px">'+ms.map(modelRow).join('')+'</div>':'<div class="k">（还没有音色——先导入一个模型包）</div>')
  +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)">'
  +'<input id="sg-src" placeholder="模型文件夹路径（点右侧浏览选择）" readonly style="flex:1;min-width:220px">'
  +'<button class="act ghost" style="margin:0" onclick="sgPickDir()">浏览…</button>'
  +'<input id="sg-name" placeholder="音色名（如 初音）" style="width:140px">'
  +'<button class="act" style="margin:0" onclick="sgImport()">导入</button>'
  +'<span class="k" id="sg-msg"></span></div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>在 QQ 里怎么用</b></div>'
  +'<div class="k" style="line-height:1.9">① 录一段自己的清唱语音发出来（十几秒内，清唱干声效果最好）<br>② 引用那条语音，对机器人说「<b>翻唱</b>」或「<b>用XX唱</b>」（群里需要 @她）<br>③ 她回一句「翻唱中」→ 约半分钟后以目标音色把整段唱出来发回<br>音色没指名时用默认音色；转换跑在本机显卡上，不花任何 API 费用。<br>需要应用 <b>0.5.14+</b>（老版本的「取语音」能力不可用）。</div></div>';
 return html;
}
async function sgCall(method,args){
 return await api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'sing_svc',method:method,args:args||{}})}).catch(()=>({ok:false,error:'请求失败'}));
}
function sgRender(){const el=document.getElementById('view');if(el)singPage().then(h=>{el.innerHTML=h})}
async function sgPick(){
 const r=await api('/api/pick-dir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'选择 so-vits-svc 整合包根目录（含 runtime 和 inference）'})}).catch(()=>({dir:''}));
 if(r&&r.dir){const el=document.getElementById('sg-dir');if(el)el.value=r.dir}
}
async function sgPickDir(){
 const r=await api('/api/pick-dir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'选择歌声模型文件夹（含 G*.pth 和 config.json）'})}).catch(()=>({dir:''}));
 if(r&&r.dir){const el=document.getElementById('sg-src');if(el)el.value=r.dir}
}
function sgUseDir(d){const el=document.getElementById('sg-dir');if(el)el.value=d;sgSaveDir()}
async function sgSaveDir(){
 const el=document.getElementById('sg-dir');
 const r=await sgCall('cfg_save',{dir:(el&&el.value||'').trim()});
 if(!r||r.ok===false){toast('保存失败：'+((r&&r.error)||''),false);return}
 toast('目录已保存'+((r.cfg&&r.cfg.dir)?'':'（空=未设置）'));sgRender();
}
async function sgSaveTune(){
 const t=parseInt((document.getElementById('sg-tran').value||'0'),10);
 const m=parseInt((document.getElementById('sg-max').value||'120'),10);
 const r=await sgCall('cfg_save',{tran:t,max_sec:m});
 if(!r||r.ok===false){toast('保存失败',false);return}
 toast('已保存（变调 '+t+' 半音 / 上限 '+m+' 秒）');
}
async function sgEngine(act){
 const r=await sgCall('engine',{action:act});
 if(!r||r.ok===false){toast('操作失败：'+((r&&r.error)||''),false);return}
 toast(r.hint||(act==='stop'?'已停止':'已下发启动'));
 setTimeout(sgRender,600);
}
async function sgImport(){
 const msg=document.getElementById('sg-msg');
 const src=(document.getElementById('sg-src').value||'').trim();
 const name=(document.getElementById('sg-name').value||'').trim();
 if(!src){if(msg)msg.textContent='先浏览选择模型文件夹';return}
 if(!name){if(msg)msg.textContent='给音色起个名字';return}
 if(msg)msg.textContent='导入中…（复制模型文件，约几秒到几十秒）';
 const r=await sgCall('model_import',{src:src,name:name});
 if(!r||r.ok===false){if(msg)msg.textContent='导入失败：'+((r&&r.error)||'');return}
 if(msg)msg.textContent='';
 toast('✓ 已导入「'+(r.name||name)+'」'+(r.spk?('（说话人 '+r.spk+'）'):'')+'——点「设为默认」即可开唱');
 document.getElementById('sg-src').value='';
 document.getElementById('sg-name').value='';
 sgRender();
}
async function sgUse(id){
 const r=await sgCall('model_use',{id:id});
 if(!r||r.ok===false){toast('操作失败：'+((r&&r.error)||''),false);return}
 toast(r.hint||'已设置');sgRender();
}
async function sgDel(id,name){
 if(!await dlgConfirm({title:'删除音色「'+name+'」？',body:'删除复制的模型文件（不可恢复）。',danger:true,okText:'删除'}))return;
 const r=await sgCall('model_del',{id:id});
 if(!r||r.ok===false){toast('删除失败：'+((r&&r.error)||''),false);return}
 toast('已删除「'+name+'」');sgRender();
}
