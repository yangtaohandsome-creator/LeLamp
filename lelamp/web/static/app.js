const apiBase = '/api/v1/web';
const byId = id => document.getElementById(id);
const sessionKey = 'lelamp-web-session';
// randomUUID is unavailable on plain LAN HTTP; getRandomValues also works there.
const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2,'0')).join('');
let sessionId = sessionStorage.getItem(sessionKey) || newId();
sessionStorage.setItem(sessionKey, sessionId);
const clientId = sessionStorage.getItem('lelamp-web-client') || newId();
sessionStorage.setItem('lelamp-web-client', clientId);
let sessionEpoch = null;
let capturedPose = null;
let toastTimer = null;

async function api(path, body, signal) {
  const response = await fetch(apiBase + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {'X-LeLamp-Client':clientId} : {'Content-Type': 'application/json','X-LeLamp-Client':clientId},
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin', signal
  });
  let result;
  try { result = await response.json(); } catch { throw new Error(`HTTP ${response.status}`); }
  if (!response.ok || (result.ok === false && !('answer' in result))) throw new Error(result.error || result.message || `HTTP ${response.status}`);
  return result;
}
function toast(message) {
  const el = byId('toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 3600);
}
function node(tag, text, className) {
  const el = document.createElement(tag); el.textContent = text;
  if (className) el.className = className; return el;
}
async function action(name, args = {}) {
  const result = await api('/action', {name, arguments: args});
  toast(name.startsWith('start_') && name.endsWith('_tracking') ? '请求已接受，正在读取跟随状态' : (result.message || '已完成'));
  await Promise.all([refreshState(), refreshItems()]);
  if (visionVisible()) await refreshVision();
  return result;
}
async function maintenance(step, body = {}) {
  const result = await api(`/maintenance/${step}`, body);
  await refreshMaintenance(); toast('维护步骤完成'); return result.data;
}
async function refreshState() {
  const {state} = await api('/state');
  if(sessionEpoch !== null && sessionEpoch !== state.session_epoch){sessionId=newId();sessionStorage.setItem(sessionKey,sessionId);}
  sessionEpoch=state.session_epoch;
  const entries = [
    ['机械', state.mechanically_asleep ? '睡眠 · 扭矩释放' : '已唤醒'],
    ['模式', state.current_mode], ['朝向', `${state.base_heading_degrees ?? 0}°`],
    ['办公照明', state.work_light ? `${state.work_pose} / ${state.work_tone} / ${state.work_brightness}%` : '未开启'],
    ['运动', state.motion_active ? '进行中' : '静止'], ['播报', state.speaking ? '正在说话' : '静默'],
    ['语音', state.voice_running ? (state.speaking ? '播报中' : '运行中') : '已暂停'], ['维护', state.maintenance.active ? '独占维护中' : '未开启']
  ];
  const grid = byId('state-grid'); grid.replaceChildren();
  for (const [label, value] of entries) {
    const card = node('div', '', 'card'); card.append(node('small', label), node('strong', String(value))); grid.append(card);
  }
  byId('connection').textContent = state.simulation ? '本机模拟 · 无硬件 / 无真实播报' : '已连接';
  return state;
}

// A status consumer only: never start a camera or a control task from polling.
let visionData = null, visionConnected = false, visionStarting = false, visionStopping = false;
let visionTimer = null, visionRequest = null, visionAbort = null, visionRevision = 0;
const visionVisible = () => !document.hidden && byId('control').classList.contains('active');
const gestureLabels = {Open_Palm:'张开手掌', Closed_Fist:'握拳', Thumb_Up:'竖拇指', Thumb_Down:'拇指向下', Victory:'剪刀手', Pointing_Up:'食指向上', ILoveYou:'爱你', None:'未分类'};
function renderVision() {
  const v = visionData;
  if (previewEnabled && visionConnected && (v?.maintenance_active || !v?.running || v?.error)) clearPreview('视觉已停止或不可用');
  const blocked = !visionConnected || !v || v.maintenance_active;
  const cannotStart = blocked || !v.enabled || visionStarting || visionStopping;
  byId('vision-face').disabled = Boolean(cannotStart || v?.current_mode === 'work_light');
  byId('vision-hand').disabled = Boolean(cannotStart || !v?.hand_control_calibrated || (v?.current_mode === 'work_light' && !v.work_light_hand_enabled));
  byId('vision-stop').disabled = Boolean(blocked || visionStopping);
  let phase = '未启动';
  if (!visionConnected) phase = '状态不可用';
  else if (v.maintenance_active) phase = '暂停 · 独占维护';
  else if (v.error) phase = '异常';
  else if (v.tracking_requested) {
    if (v.tracking_status === 'created' || v.tracking_status === 'entering_home' || v.tracking_status === 'entering_limits' || v.status === 'starting') phase = '启动中';
    else if (!v.tracking_active) phase = '暂停';
    else if (v.tracking_phase?.startsWith('light_')) phase = ({light_hold:'照明锁定',light_acquire:'等待握拳手',light_follow:'手部调向',light_lost:'丢失暂停'})[v.tracking_phase] || '暂停';
    else if (v.tracking_phase === 'hand_hold') phase = '位置锁定';
    else if (v.tracking_phase === 'hand_acquire') phase = '等待近距离手';
    else if (v.directive === 'home') phase = '回初始姿态';
    else if (v.tracking_source === 'hand') phase = '手部跟随';
    else phase = '人脸跟随';
  }
  const hint = !visionConnected ? '暂时无法读取状态；上次检测结果已失效。' :
    v.maintenance_active ? '维护期间禁止视觉控制。' :
    v.current_mode === 'work_light' ? (!v.work_light_hand_enabled ? '照明手势调向未启用。' : v.target_ambiguous ? '多手目标不明确，保持当前位置。' : v.tracking_phase === 'light_lost' ? `保持低亮度；${Number(v.lost_remaining_seconds).toFixed(1)}秒内握拳可恢复，超时锁定。` : '张开→握拳调向，握拳→张开锁定；调向后3秒内连续张开→握拳→张开退出照明。人脸跟随请先退出照明。') :
    !v.enabled ? '视觉功能未启用。' :
    !v.hand_control_calibrated ? '手部阈值尚未标定；人脸跟随仍可使用。' :
    v.tracking_phase === 'hand_acquire' ? '请在摄像头前放入一只近距离手；超时后继续人脸跟随。' :
    v.tracking_phase === 'hand_hold' ? '当前角度保持；继续手势交互或点击人脸跟随切换。' :
    '状态约每秒更新；检测到目标不代表舵机已完成跟随。';
  byId('vision-hint').textContent = hint + (v?.simulation ? ' 当前为模拟数据，无真实摄像头或运动。' : '');
  byId('vision-error').textContent = visionConnected ? (v.error || '') : '';
  const valid = visionConnected && v?.fresh;
  const perceptionLabels = {running:'运行中', starting:'启动中', stopped:'未启动', stopping:'停止中', error:'异常', disabled:'未启用'};
  const gestures = valid ? v.gestures.map(h => `${h.track_id || '手'}：${gestureLabels[h.gesture] || h.gesture || '未分类'}${typeof h.confidence === 'number' ? ' '+Math.round(h.confidence*100)+'%' : ''}`).join('；') : '—';
  const entries = [
    ['跟随状态', phase], ['视觉感知', visionConnected ? (perceptionLabels[v.status] || v.status) : '—'],
    ['推理频率', visionConnected && v.running ? `${Number(v.inference_hz).toFixed(1)} Hz` : '—'],
    ['最近检测', valid ? `${v.face_detection_enabled === false ? "人脸检测已停用" : `人脸 ${v.face_count}`} · 手 ${v.hand_count}` : '暂无有效结果'],
    ['最近手势', valid ? (gestures || '未检测到手') : '—'],
    ['结果年龄', visionConnected && v.result_age_ms != null ? `${Math.round(v.result_age_ms)} ms${valid ? '（读取时）' : ' · 已失效'}` : '—'],
    ['手部标定', visionConnected ? (v.hand_control_calibrated ? '已标定' : '未标定') : '—']
  ];
  const grid = byId('vision-grid'); grid.replaceChildren();
  for (const [label, value] of entries) {const card=node('div','','card');card.append(node('small',label),node('strong',value));grid.append(card);}
}
function refreshVision() {
  if (!visionVisible()) return Promise.resolve();
  if (visionRequest) return visionRequest;
  clearTimeout(visionTimer);
  const revision = visionRevision;
  const controller = new AbortController(); visionAbort = controller;
  const timeout = setTimeout(() => controller.abort(), 4000);
  visionRequest = api('/vision', undefined, controller.signal).then(({vision}) => {
    if (revision !== visionRevision) return;
    visionData = vision; visionConnected = true; renderVision();
  }).catch(() => {
    if (visionVisible()) {visionConnected = false; renderVision();}
  }).finally(() => {
    clearTimeout(timeout); visionRequest = null; visionAbort = null;
    if (visionVisible()) visionTimer = setTimeout(refreshVision, revision === visionRevision ? 1000 : 0);
  });
  return visionRequest;
}
function updateVisionPolling() {
  updatePreview();
  visionRevision++; // A return during an aborted request must refresh immediately.
  clearTimeout(visionTimer);
  if (visionVisible()) {visionConnected = false;renderVision();refreshVision();}
  else {visionAbort?.abort();}
}
document.addEventListener('visibilitychange', updateVisionPolling);
document.querySelectorAll('[data-vision-action]').forEach(button => button.addEventListener('click', async () => {
  const stop = button.dataset.visionAction === 'stop_tracking';
  if (button.disabled || (stop ? visionStopping : visionStarting)) return;
  if (stop) visionStopping = true; else visionStarting = true;
  visionRevision++; renderVision();
  try {await action(button.dataset.visionAction);} catch(e) {toast(e.message);}
  finally {if (stop) visionStopping=false;else visionStarting=false;visionRevision++;renderVision();await refreshVision();}
}));

function itemRow(label, controls) {
  const row = node('div', '', 'item'); row.append(node('span', label));
  const buttons = node('div', '', 'buttons');
  for (const [title, fn] of controls) {
    const button = node('button', title); button.addEventListener('click', () => fn().catch(e => toast(e.message))); buttons.append(button);
  }
  row.append(buttons); return row;
}
async function refreshItems() {
  const {timers, alarms, todos} = await api('/items');
  const timerBox = byId('timers'); timerBox.replaceChildren();
  for (const t of timers) timerBox.append(itemRow(`${t.timer_id} · ${Math.ceil(t.remaining)} 秒 · ${t.status} · ${t.message || ''}`, [
    [t.status === 'paused' ? '继续' : '暂停', () => action(t.status === 'paused' ? 'resume_timer' : 'pause_timer', {timer_id:t.timer_id})],
    ['+60 秒', () => action('add_timer_time', {timer_id:t.timer_id, seconds:60})],
    ['取消', () => action('cancel_timer', {timer_id:t.timer_id})]
  ]));
  if (!timers.length) timerBox.append(node('p', '暂无进行中的计时器', 'hint'));
  const alarmBox = byId('alarms'); alarmBox.replaceChildren();
  for (const a of alarms) alarmBox.append(itemRow(`${a.alarm_id} · ${a.trigger_at || a.next_trigger_at || ''} · ${a.recurrence || ''} · ${a.message || ''}`, [
    ['取消', () => action('cancel_alarm', {alarm_id:a.alarm_id})]
  ]));
  if (!alarms.length) alarmBox.append(node('p', '暂无进行中的闹钟', 'hint'));
  const todoBox = byId('todos'); todoBox.replaceChildren();
  const completed = byId('todo-completed').checked;
  const visibleTodos = todos.filter(t => (t.status === 'completed') === completed);
  visibleTodos.forEach((t, index) => {
    const controls = completed ? [] : [
      ['修改', async () => {const text = prompt('修改待办', t.text); if (text !== null && text.trim()) await action('update_todo', {todo_id:t.todo_id,text:text.trim()});}],
      ['完成', () => action('complete_todo', {todo_id:t.todo_id})]
    ];
    controls.push(['删除', () => action('delete_todo', {todo_id:t.todo_id})]);
    todoBox.append(itemRow(`${index + 1}. ${t.text}`, controls));
  });
  if (!visibleTodos.length) todoBox.append(node('p', completed ? '暂无已完成事项' : '暂无待办', 'hint'));
}
async function refreshMaintenance() {
  const {maintenance:m} = await api('/maintenance');
  byId('maintenance-state').textContent = `状态：${m.active ? '维护中' : '未开启'}\n设备：${m.device || '未连接'}\n采样：${m.sampling ? '进行中' : '停止'} · ${m.samples} 帧${m.error ? '\n提示：'+m.error : ''}`;
  return m;
}
for (const button of document.querySelectorAll('nav button')) button.addEventListener('click', () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('selected', x === button));
  document.querySelectorAll('.page').forEach(x => x.classList.toggle('active', x.id === button.dataset.page));
  if (button.dataset.page === 'manage') refreshItems().catch(e => toast(e.message));
  if (button.dataset.page === 'maintain') refreshMaintenance().catch(e => toast(e.message));
  updateVisionPolling();
});
const motions = ['nod','headshake','happy_wiggle','excited','sad','shy','shock','curious'];
const labels = ['点头','摇头','高兴','兴奋','难过','害羞','震惊','好奇'];
motions.forEach((name, i) => {const b=node('button',labels[i]);b.addEventListener('click', () => action('play_motion',{name}).catch(e=>toast(e.message)));byId('motion-buttons').append(b);});
document.querySelectorAll('[data-action]').forEach(b => b.addEventListener('click', () => action(b.dataset.action, JSON.parse(b.dataset.args || '{}')).catch(e => toast(e.message))));
document.querySelectorAll('[data-step]').forEach(b => b.addEventListener('click', async () => {try{const result=await maintenance(b.dataset.step, JSON.parse(b.dataset.payload || '{}'));if(b.dataset.step==='range_stop')byId('calibration-result').textContent=JSON.stringify(result,null,2);}catch(e){toast(e.message);}}));
function renderHealth(health) {
  const checks = health.checks || [];
  const issues = checks.filter(c => c.status === 'failed' || c.status === 'warning');
  const unknown = checks.filter(c => c.status !== 'ok' && c.status !== 'failed' && c.status !== 'warning');
  const summary = byId('health-summary');
  summary.replaceChildren();
  summary.append(node('strong', issues.length ? `发现 ${issues.length} 项需要处理：` : checks.length ? '已检查项目未发现异常。' : '暂无检查结果。'));
  if (issues.length) {
    const list = document.createElement('ul');
    issues.forEach(c => list.append(node('li', c.detail || c.id)));
    summary.append(list);
  }
  if (unknown.length) summary.append(node('p', `另有 ${unknown.length} 项暂不可测或需人工确认，见详细结果。`));
  byId('health-result').textContent = JSON.stringify(health, null, 2);
}
byId('health-run').addEventListener('click', async () => {
  const button = byId('health-run'); button.disabled = true;
  byId('health-summary').textContent = '正在检查……';
  byId('health-result').textContent = '';
  try {renderHealth((await api('/health')).health);}
  catch(e){byId('health-summary').textContent = `自检未完成：${e.message}`;}
  finally {button.disabled = false;}
});
byId('chat-form').elements.text.addEventListener('keydown', ev => {
  // 中文输入法确认候选词时的 Enter 不应提交。
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
    ev.preventDefault();
    if (!ev.repeat) byId('chat-form').requestSubmit();
  }
});
byId('chat-form').addEventListener('submit', async ev => {
  ev.preventDefault(); const form=ev.currentTarget, text=form.elements.text.value.trim(); if (!text) return;
  byId('chat-log').append(node('div',text,'bubble me')); form.elements.text.value='';
  const pending=node('div','小灯在想……','bubble');byId('chat-log').append(pending);
  try {const result=await api('/chat',{text,session_id:sessionId,speak:form.elements.speak.checked});
    pending.textContent=result.answer;pending.append(node('small',`${result.status} · ${result.spoken?'已播报':'未播报'}`));
    if(result.new_session){sessionId=newId();sessionStorage.setItem(sessionKey,sessionId);}
    await refreshState();
  } catch(e){pending.textContent=`请求失败：${e.message}`;}
  byId('chat-log').scrollTop=byId('chat-log').scrollHeight;
});
byId('timer-form').addEventListener('submit', async ev => {ev.preventDefault();const f=ev.currentTarget;try{await action('create_timer',{duration_seconds:Number(f.elements.duration_seconds.value),message:f.elements.message.value});f.reset();}catch(e){toast(e.message);}});
byId('alarm-form').addEventListener('submit', async ev => {ev.preventDefault();const f=ev.currentTarget;const args={trigger_at:new Date(f.elements.trigger_at.value).toISOString(),message:f.elements.message.value,recurrence:f.elements.recurrence.value};if(args.recurrence==='weekly' && f.elements.day_of_week.value!=='')args.day_of_week=Number(f.elements.day_of_week.value);try{await action('create_alarm',args);f.reset();}catch(e){toast(e.message);}});
byId('todo-form').addEventListener('submit', async ev => {ev.preventDefault();const f=ev.currentTarget;try{await action('create_todo',{text:f.elements.text.value});f.reset();}catch(e){toast(e.message);}});
byId('maintenance-begin').addEventListener('click', () => maintenance('begin').catch(e=>toast(e.message)));
byId('maintenance-end').addEventListener('click', () => maintenance('end').catch(e=>toast(e.message)));
byId('record-form').addEventListener('submit', async ev => {ev.preventDefault();const f=ev.currentTarget;try{const x=await maintenance('record_save',{name:f.elements.name.value,overwrite:f.elements.overwrite.checked});toast(`保存 ${x.frames} 帧${x.backup?'，原文件已备份':''}`);}catch(e){toast(e.message);}});
byId('preview-form').addEventListener('submit', async ev => {ev.preventDefault();try{await maintenance('record_preview',{name:ev.currentTarget.elements.name.value});}catch(e){toast(e.message);}});
byId('calibration-save').addEventListener('click', async () => {if(!confirm('确认保存当前校准？原文件将先备份。'))return;try{const x=await maintenance('calibration_save');byId('calibration-result').textContent=JSON.stringify(x,null,2);}catch(e){toast(e.message);}});
byId('pose-capture').addEventListener('click', async()=>{try{capturedPose=await maintenance('pose_capture',{pose:byId('pose-name').value});byId('pose-result').textContent=JSON.stringify(capturedPose.values,null,2);}catch(e){toast(e.message);}});
byId('pose-save').addEventListener('click', async()=>{if(!capturedPose || capturedPose.pose!==byId('pose-name').value){toast('请先读取当前姿态');return;}if(!confirm('确认覆盖此姿态？原配置会备份。'))return;try{await maintenance('pose_save',capturedPose);}catch(e){toast(e.message);}});
window.addEventListener('pagehide',()=>{if(byId('maintenance-state').textContent.includes('维护中'))fetch(apiBase+'/maintenance/disconnect',{method:'POST',headers:{'Content-Type':'application/json','X-LeLamp-Client':clientId},body:'{}',keepalive:true}).catch(()=>{});});
setInterval(()=>{refreshState().catch(()=>{byId('connection').textContent='连接断开';});if(byId('manage').classList.contains('active'))refreshItems().catch(()=>{});if(byId('maintenance-state').textContent.includes('维护中'))api('/maintenance/heartbeat',{}).catch(()=>{});},5000);
Promise.all([refreshState(),refreshItems(),refreshMaintenance()]).catch(e=>toast(e.message));

byId('todo-completed').addEventListener('change', () => refreshItems().catch(e => toast(e.message)));


let previewEnabled=false, previewTimer=null, previewAbort=null, previewBusy=false, previewEpoch=0, previewFailures=0, previewExpiry=null, previewLast=0, previewDiscard=null;
function clearPreview(message) {
  clearTimeout(previewExpiry);clearTimeout(previewDiscard);
  const c=byId('vision-preview');c.hidden=true;c.width=1;c.height=1;
  if(message)byId('preview-status').textContent=message;
}
function updatePreview() {
  previewEpoch++;clearTimeout(previewTimer);previewAbort?.abort();
  clearPreview(previewEnabled?'等待最新画面…':'预览关闭');previewLast=0;
  if(previewEnabled&&visionVisible()&&!previewBusy)previewTimer=setTimeout(fetchPreview,0);
}
byId('preview-toggle').addEventListener('click',()=>{
  previewEnabled=!previewEnabled;previewFailures=0;
  byId('preview-toggle').textContent=previewEnabled?'关闭预览':'开启预览';updatePreview();
});
window.addEventListener('pagehide',()=>{previewEnabled=false;updatePreview();});
async function fetchPreview() {
  if(!previewEnabled||!visionVisible()||previewBusy)return;
  previewBusy=true;const epoch=previewEpoch, started=performance.now();
  const controller=new AbortController();previewAbort=controller;
  const timeout=setTimeout(()=>controller.abort(),1000);
  // Leave scheduling margin below the server limit of 5 FPS.
  let retryDelay=220;
  try {
    const r=await fetch(apiBase+'/vision/preview',{signal:controller.signal,cache:'no-store'});
    if(epoch!==previewEpoch||!previewEnabled||!visionVisible())return;
    if(r.status===429){
      retryDelay=500+Math.random()*200;
      if(byId('vision-preview').hidden)byId('preview-status').textContent='预览繁忙，等待可用帧…';
      return;
    }
    if(!r.ok)throw Error('视觉未运行、已过期或暂不可用');
    const buffer=await r.arrayBuffer();
    if(epoch!==previewEpoch||!previewEnabled||!visionVisible())return;
    if(buffer.byteLength<4)throw Error('预览数据无效');
    const n=new DataView(buffer).getUint32(0), meta=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,4,n)));
    if(visionConnected && (visionData?.maintenance_active || !visionData?.running || visionData?.error)) {clearPreview('视觉已停止或不可用');return;}
    if(visionConnected && visionData?.face_detection_enabled === false && meta.face_detection_enabled) {clearPreview('感知模式已切换，等待新画面');return;}
    const w=meta.width,h=meta.height;
    if(!Number.isInteger(w)||!Number.isInteger(h)||w<1||h<1||w*h>640*480||buffer.byteLength!==4+n+w*h*3)throw Error('预览尺寸无效');
    const age=meta.result_age_ms+performance.now()-started;
    // Display freshness is independent of motion-control freshness. Never replay a queue.
    if(!Number.isFinite(age)||age>1000)throw Error('预览数据已过期');
    const pixels=new Uint8Array(buffer,4+n), c=byId('vision-preview');
    if(c.width!==w||c.height!==h){c.width=w;c.height=h;}
    const ctx=c.getContext('2d'), image=ctx.createImageData(w,h);
    for(let i=0,j=0;i<pixels.length;i+=3,j+=4){image.data[j]=pixels[i+2];image.data[j+1]=pixels[i+1];image.data[j+2]=pixels[i];image.data[j+3]=255;}
    ctx.putImageData(image,0,0);ctx.lineWidth=1.5;ctx.font='12px sans-serif';
    for(const f of meta.faces){ctx.strokeStyle=f.id===meta.active_face_target_id?'#ffdf00':'#48ff70';ctx.strokeRect(f.box[0]*w,f.box[1]*h,f.box[2]*w,f.box[3]*h);}
    const edges=[[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[0,17],[17,18],[18,19],[19,20]];
    for(const hand of meta.hands){ctx.strokeStyle=hand.id===meta.active_hand_target_id?'#ffdf00':'#48ff70';for(const [a,b] of edges){const p=hand.points[a],q=hand.points[b];if(!p||!q)continue;ctx.beginPath();ctx.moveTo(p[0]*w,p[1]*h);ctx.lineTo(q[0]*w,q[1]*h);ctx.stroke();}const p=hand.points[0]||[.03,.2];ctx.fillStyle='#fff';ctx.fillText(gestureLabels[hand.gesture]||hand.gesture||'未分类',Math.min(p[0]*w,w-80),Math.max(15,p[1]*h));}
    c.hidden=false;const now=performance.now(),fps=previewLast?1000/(now-previewLast):0;previewLast=now;previewFailures=0;
    byId('preview-status').textContent=`${meta.simulation?'模拟画面 · ':''}预览 ${fps.toFixed(1)} FPS · ${Math.round(age)}ms${meta.face_detection_enabled?'':' · 人脸检测已停用'}`;
    clearTimeout(previewExpiry);clearTimeout(previewDiscard);
    previewExpiry=setTimeout(()=>{byId('preview-status').textContent='画面已过期 · 保留最后一帧，等待更新';},Math.max(1,1000-age));
    previewDiscard=setTimeout(()=>clearPreview('持续未收到新画面，等待更新'),Math.max(1,3000-age));
  } catch(e) {
    if(epoch!==previewEpoch)return;
    // A missed frame must not flash the canvas off. Expiry timers still run.
    if(byId('vision-preview').hidden)byId('preview-status').textContent=e.name==='AbortError'?'预览请求超时':e.message;
    if(++previewFailures>=3){previewEnabled=false;byId('preview-toggle').textContent='开启预览';clearPreview('预览已暂停，请重新开启；跟随不受此操作影响');}
  } finally {
    clearTimeout(timeout);previewBusy=false;previewAbort=null;
    if(previewEnabled&&visionVisible())previewTimer=setTimeout(fetchPreview,Math.max(50,retryDelay-(performance.now()-started)));
  }
}
