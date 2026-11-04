// 本文件随插件 zip 分发（宿主有同版 fallback）；数据端点在宿主
function chatImpPage(d,p){
 const src=d&&d.source||'qq', label=(d&&d.label)||'聊天', hint=(d&&d.hint)||'';
 const ss=(d&&d.sessions)||[];
 const pb=(d&&d.probe)||{};
 const rows=ss.map(s=>'<div class="card" style="margin-top:10px"><div class="k" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(s.name)+'</b><span class="badge" style="background:var(--tint);color:var(--dim)">'+s.count+' 条</span>'+(s.range&&s.range[0]?'<span class="k">'+esc(s.range[0])+' ~ '+esc(s.range[1])+'</span>':'')+'<span style="margin-left:auto;display:flex;gap:6px"><button class="act" style="margin:0;padding:4px 14px;font-size:12.5px" onclick="ciLearn(\''+src+'\',\''+esc(s.name)+'\')">✨ 学习成人设</button><button class="act ghost danger" style="margin:0;padding:4px 12px;font-size:12px" onclick="ciDelete(\''+src+'\',\''+esc(s.name)+'\')">删除</button></span></div>'+(s.senders&&s.senders.length?'<div class="k" style="margin-top:4px">说话人：'+esc(s.senders.join('、'))+'</div>':'')+'</div>').join('');
 const envRow=(ok,txt)=>'<div class="k" style="margin:3px 0">'+(ok?'✅ ':'⚠ ')+esc(txt)+'</div>';
 const dirsHtml=(pb.data_dirs||[]).map(x=>'<div class="k mono" style="font-size:12px;margin:3px 0">📂 '+esc(x.label)+'：'+esc(x.path)+'</div>').join('')||'<div class="k">未在常见位置找到聊天记录数据库（可能装在自定义目录）</div>';
 const isQQ=src==='qq';
 const cp=(id,txt)=>'<button class="act ghost" style="margin:0;padding:3px 10px;font-size:11.5px" onclick="ciCopy(\''+id+'\')">复制</button><textarea id="'+id+'" style="position:absolute;left:-9999px;top:0">'+esc(txt)+'</textarea>';
 const wxReady=(pb.wx_skill_installed&&pb.wx_cli_cmd);
 const step2=isQQ
  ?((pb.qq_cli_available
    ?'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">方式 A（推荐）：本机已装 qq-cli，直接导出</div>'+(pb.python_ok===false?'<div class="k" style="color:var(--danger,#c00)">⚠ 未检测到系统 Python（打包版调用 qq-cli 需要）：python.org 安装 Python 3 后重试</div>':'')+'<div style="display:flex;gap:8px;align-items:center"><input id="ci-chat" placeholder="QQ号 / 昵称 / 群名（需先运行过一次 qq_cli.py init 提取密钥）" style="flex:1" onchange=""><button class="act" style="margin:0" onclick="ciQqCliExport()">导出并导入</button></div></div><div class="k" style="margin-top:6px">方式 B：自己用 qq-cli 导出 md/json 后点下面按钮导入</div>'
    :'<div class="k">本机未装 qq-cli 技能——可在 ZCode 里安装 skillhub 的 <b>xujianpeng/qq-cli</b>（装好后回本页即可一键导出），或自己用 qq-cli / 任意工具导出 md/json</div>')
   +'<div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div>'
   +'<div class="k" style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px"><b>方式 C（免导出工具，任何 QQ 版本可用）：</b>在 QQ 里长按选中想让我学的聊天记录 → 合并转发发给机器人，并在消息里说「导入」。<span class="k">例如私聊里发一条合并转发+文字「导入 和小明的聊天」——内容直接进下面的隔离库。每次转发有限条数，记录多可分几次转发（会话名相同会自动编号，学习时可分别点）。</span></div>')
  :(wxReady
    ?'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">方式 A（推荐）：本机 wechat-cli 已就绪，直接导出</div><div style="display:flex;gap:8px;align-items:center"><input id="ci-wxchat" placeholder="联系人备注/昵称 或 群名" style="flex:1" onchange=""><button class="act" style="margin:0" onclick="ciWxCliExport()">导出并导入</button></div></div><div class="k" style="margin-top:6px">首次使用需先提取密钥（微信登录状态下，终端跑一次技能 scripts/extract_keys.py）</div><div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div>'
    :'<div style="margin-top:8px"><div class="k" style="margin-bottom:4px">装好下面两步即可解锁「一键导出」（当前：'+(pb.wx_skill_installed?'技能 ✅':'技能 ⚠ 未装')+' · '+(pb.wx_cli_cmd?'命令 ✅':'命令 ⚠ 未装')+'）</div>'
     +'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">① skillhub install wechat-cli --namespace user_514405d2 '+cp('ci-cmd1','skillhub install wechat-cli --namespace user_514405d2')+'</div>'
     +'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">② pip install git+https://github.com/jidechao/py-wechat-cli.git '+cp('ci-cmd2','pip install git+https://github.com/jidechao/py-wechat-cli.git')+'</div>'
     +'<div class="k" style="margin-top:6px">②装完后再登录微信提取一次密钥（技能 scripts/extract_keys.py）；两步都 ✅ 后回本页刷新。装不动也没关系：用任意工具导出 md/txt 后点下面按钮导入。</div>'
     +'<div style="margin-top:8px"><button class="act ghost" style="margin:0" onclick="ciPick(\''+src+'\')">选择已导出的文件导入…</button></div></div>');
 return '<div class="card"><div class="k"><b>① 环境检测</b></div>'
 +envRow(pb.client_running,label+' 客户端正在运行'+(pb.client_running?'':'（导出/读库前请先打开并登录'+label+'）'))
 +'<div class="k" style="margin-top:4px">本机聊天数据目录：</div>'+dirsHtml
 +(isQQ?envRow(pb.qq_cli_available,'qq-cli 技能'+(pb.qq_cli_available?'已安装（位于 ~/.zcode/skills/qq-cli）':'未安装'))
   +envRow(pb.python_ok!==false,'系统 Python（打包版调用技能导出用）'+(pb.python_ok===false?'未检测到（python.org 安装）':'可用'))
   +envRow(pb.qq_key_ready,'QQ 数据库密钥'+(pb.qq_key_ready?'已配置（若导出报解密失败请重新提取）':'未提取——首次导出前需跑一次 init'))
   +(pb.qq_key_ready?'':'<div class="k mono" style="font-size:12px;margin:4px 0;display:flex;gap:6px;align-items:center">'+esc(pb.qq_cli_init_cmd||'')+' '+cp('ci-cmdinit',pb.qq_cli_init_cmd||'')+'</div><div class="k" style="font-size:12px;margin:2px 0 6px">保持 QQ 登录状态运行，按提示在 QQ 搜索框输入任意词触发提取（密钥从你登录中的 QQ 进程提取）；数据目录不在默认位置时加 --db-dir 指到 nt_db 所在目录。命令可直接复制粘贴（已含你的用户名绝对路径，PowerShell/cmd 通用）</div>'):envRow(pb.wx_skill_installed,'wechat-cli 技能（skillhub user_514405d2）'+(pb.wx_skill_installed?'已安装':'未安装'))+envRow(!!pb.wx_cli_cmd,'wechat-cli 命令（py-wechat-cli）'+(pb.wx_cli_cmd?'可用：'+esc(pb.wx_cli_cmd):'未安装')))
 +'</div>'
 +'<div class="card"><div class="k"><b>② 导出聊天记录</b></div><div class="k" style="margin-top:6px">'+esc(hint)+'</div>'+step2+'</div>'
 +'<div class="card"><div class="k"><b>③ 隔离库中的会话</b>（导入后在这里，点「学习成人设」让机器人消化）</div>'+(ss.length?rows:'<div class="k" style="margin-top:6px">还没有导入任何会话。</div>')+'</div>'
 +'<div class="k" style="margin-top:6px">隔离库位置：<span class="mono">~/.zcode-qq-bot/chat_import/</span>（不进记忆、不进上下文，只是学习原料）</div>';
}
function qqChatImpPage(d,p){return chatImpPage(Object.assign({},d,{source:'qq',label:'QQ',hint:'见上方方式 A/B'}),p)}
function wxChatImpPage(d,p){return chatImpPage(Object.assign({},d,{source:'wx',label:'微信',hint:'就绪后可直接一键导出；未就绪按第②步两行命令装好后回本页'}),p)}
let _VM=null;
// 插件页契约：page 函数接收宿主预取的 data、返回 HTML 字符串；
// 不允许自写 #view（会把宿主注入的「← 返回插件列表」按钮冲掉）

async function ciQqCliExport(){
 const chat=document.getElementById('ci-chat').value.trim();
 if(!chat){toast('先填会话（QQ号/昵称/群名）',false);return}
 const r=await runTask('QQ 导出「'+chat.slice(0,12)+'」','/api/chatimport/qqcli-export',{chat:chat});
 if(r.ok===false){toast(r.error||'导出失败',false);return}
 toast('已导入「'+(r.session||chat)+'」'+(r.count||0)+' 条');load()}
async function ciWxCliExport(){
 const chat=document.getElementById('ci-wxchat').value.trim();
 if(!chat){toast('先填会话（联系人备注/昵称 或 群名）',false);return}
 const r=await runTask('微信导出「'+chat.slice(0,12)+'」','/api/chatimport/wxcli-export',{chat:chat});
 if(r.ok===false){toast(r.error||'导出失败',false);return}
 toast('已导入「'+(r.session||chat)+'」'+(r.count||0)+' 条');load()}
function ciCopy(id){
 const t=document.getElementById(id);if(!t)return;
 t.select();document.execCommand('copy');toast('已复制，去终端粘贴执行')}
async function ciPick(src){
 toast('请选择导出文件…');
 const r=await api('/api/chatimport/pick',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'解析失败',false);return}
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog"><h3>导入到隔离库</h3><div class="dlg-body">'
 +'解析成功：<b>'+r.count+'</b> 条 · '+esc((r.range||[])[0]||'?')+' ~ '+esc((r.range||[])[1]||'?')+'<br>说话人：'+esc((r.senders||[]).join('、')||'?')+'<div class="k mono" style="margin-top:8px;font-size:12px">'+(r.preview||[]).map(x=>esc(x)).join('<br>')+'</div>'
 +'<div style="margin-top:10px"><div class="k" style="margin-bottom:4px">会话名（用于隔离库展示）</div><input id="ci-name" value="'+esc((r.path||'').split(/[\\\\/]/).pop()||'').replace(/\\.[^.]+$/,'')+'" style="width:100%"></div>'
 +'</div><div class="dlg-btns"><button class="act ghost">取消</button><button class="act">导入</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=e=>{if(e.target===m)close()};
 m.querySelector('.dlg-btns .act').onclick=async()=>{
  const name=document.getElementById('ci-name').value.trim()||'会话';
  close();
  const st=await api('/api/chatimport/store',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name,path:r.path})}).catch(()=>({ok:false,error:'请求失败'}));
  if(st.ok===false){toast(st.error||'导入失败',false);return}
  toast('已导入「'+name+'」'+st.count+' 条到隔离库');load()};
}
async function ciDelete(src,name){
 if(!await dlgConfirm({title:'删除会话',body:'删除隔离库中的「'+esc(name)+'」？学习原料将不可恢复（已保存的人设/记忆不受影响）。',okText:'删除',danger:true}))return;
 const r=await api('/api/chatimport/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name})}).catch(()=>({ok:false}));
 if(r.ok!==false)load();else toast('删除失败',false);
}
async function ciLearn(src,name){
 const who=await dlgPrompt({title:'学习「'+name+'」',body:'要让机器人重点学习谁的说话风格？<br>留空 = 学习整体氛围与主要说话人。',placeholder:'说话人名字（可留空）',okText:'开始学习'});
 if(who===null)return;
 toast('机器人正在消化记录（约 30-90 秒）…');
 const r=await api('/api/chatimport/learn',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:src,session:name,who:who||''})}).catch(()=>({ok:false,error:'请求失败'}));
 if(r.ok===false){toast(r.error||'学习失败',false);return}
 const ps=await api('/api/personas').catch(()=>({personas:[]}));
 const exist=(ps.personas||[]).map(x=>x.name);
 const suggest=r.persona_name||'';
 const m=document.createElement('div');m.className='dlg-mask';
 m.innerHTML='<div class="dlg" role="dialog" style="max-width:640px"><h3>学习结果 → 人设草稿</h3><div class="dlg-body">'
 +'<div class="k" style="margin-bottom:4px">人设名'+(exist.includes(suggest)?'（<b style="color:var(--err)">与现有人设重名，保存将覆盖</b>）':'')+'</div><input id="ci-pname" value="'+esc(suggest)+'" style="width:60%">'
 +'<div class="k" style="margin:8px 0 4px">人设卡正文（可编辑）</div><textarea id="ci-ptext" rows="12" style="width:100%;resize:vertical;font-size:13px">'+esc(r.persona)+'</textarea>'
 +'<div class="k" style="margin:8px 0 4px">候选记忆（勾选后写入长期记忆；需填关联对象）</div><div id="ci-mems" style="max-height:140px;overflow:auto">'+(r.memories||[]).map((x,i)=>'<label style="display:flex;gap:6px;align-items:flex-start;margin:3px 0"><input type="checkbox" checked data-i="'+i+'"> <span class="k">'+esc(x)+'</span></label>').join('')+'</div>'
 +'<div style="display:flex;gap:8px;margin-top:8px;align-items:center"><span class="k" style="flex:none">记忆关联</span><input id="ci-mid" placeholder="QQ号/群号（不填则不写记忆）" style="flex:1"></div>'
 +'</div><div class="dlg-btns"><button class="act ghost">取消</button><button class="act">保存人设</button></div></div>';
 document.body.appendChild(m);
 const close=()=>m.remove();
 m.querySelector('.dlg-btns .ghost').onclick=close;
 m.onclick=e=>{if(e.target===m)close()};
 m.querySelector('.dlg-btns .act').onclick=async()=>{
  const pname=document.getElementById('ci-pname').value.trim();
  const ptext=document.getElementById('ci-ptext').value.trim();
  if(!pname||ptext.length<20){toast('人设名与正文不能为空/过短',false);return}
  if(exist.includes(pname)){
   const old=(ps.personas||[]).find(x=>x.name===pname);
   if(!await dlgConfirm({title:'覆盖人设「'+pname+'」',body:'已存在同名的人设卡（'+((old&&old.notes)||'')+'…）。<br><b>覆盖后旧内容将丢失，确定？</b>',okText:'覆盖保存',danger:true}))return;
  }
  const sv=await api('/api/personas/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:pname,text:ptext})}).catch(()=>({ok:false}));
  if(sv.ok===false){toast('人设保存失败',false);return}
  toast('人设「'+pname+'」已保存（人设页可启用）');
  const mid=document.getElementById('ci-mid').value.trim();
  const picked=[...document.querySelectorAll('#ci-mems input:checked')].map(cb=>(r.memories||[])[+cb.dataset.i]).filter(Boolean);
  if(mid&&/^\d{5,12}$/.test(mid)&&picked.length){
   for(const t of picked){await api('/api/memory/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope:'contact',id:mid,text:t})}).catch(()=>{})}
   toast('已写入 '+picked.length+' 条记忆');
  }
  close();
