// 群签到设置页：调自定义端点（/api/plugins/call → agent 里 api.route 注册的处理器）
// 插件页契约：page 函数返回 HTML 字符串，不自写 #view
async function checkinPage(d){
 const [bd, td] = await Promise.all([
  api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'group_checkin',method:'board'})}).catch(()=>({ok:false})),
  api('/api/plugins/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'group_checkin',method:'today'})}).catch(()=>({ok:false})),
 ]);
 const boardRows=(bd.ok?(bd.board||[]):[]).map(b=>
  '<div class="k" style="display:flex;gap:8px;align-items:center;margin:4px 0;padding:6px;border:1px solid var(--line);border-radius:6px">'
  +'<b style="min-width:28px;color:'+(b.rank<=3?'var(--accent,#4a7dff)':'var(--dim)')+'">#'+b.rank+'</b>'
  +'<span>'+esc(b.name||'?')+'</span>'
  +'<span class="badge" style="background:var(--tint);color:var(--dim);margin-left:auto">'+b.days+' 天</span></div>').join('');
 const todayRows=(td.ok?(td.today||[]):[]).map((t,i)=>
  '<div class="k" style="margin:3px 0">'+(i+1)+'. '+esc(t.name||'?')+'</div>').join('');
 return '<div class="card"><div class="k"><b>用法</b>：在群里发「签到」或「打卡」，机器人记录你是今天第几位。同一人每天只算一次。</div></div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>今日签到</b>（'+((td.ok?(td.today||[]):[]).length)+' 人）</div>'
 +(todayRows||'<div class="k">今天还没人签到</div>')+'</div>'
 +'<div class="card"><div class="k" style="margin-bottom:6px"><b>累计总榜</b>（前 20）</div>'
 +(boardRows||'<div class="k">还没有数据</div>')+'</div>';
}
