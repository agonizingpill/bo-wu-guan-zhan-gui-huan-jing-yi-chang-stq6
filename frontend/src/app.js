const state = {
  token: localStorage.getItem('museum_token'),
  user: JSON.parse(localStorage.getItem('museum_user') || 'null'),
  page: 'monitor',
  cases: [],
  anomalies: [],
  dashboard: {},
  groups: { candidates: [], singletons: [] },
  selectedCandidate: null,
  claimNote: '',
  selectedCase: null,
  selectedEvent: null,
  notice: ''
};

const api = async (path, options = {}) => {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`/api${path}`, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '请求失败');
  return data;
};

const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const fmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const ticketLabel = { claimed: '已认领', in_progress: '处置中', closed: '已关闭', cancelled: '已撤销' };
const label = (value) => ({active:'在岗', maintenance:'维护', retired:'停用', open:'待处置', triaged:'已接单', escalated:'已升级', resolved:'待关闭', closed:'已关闭', watch:'观察', high:'高', critical:'严重', door_interference:'门开干扰', environment_outlier:'环境离群', continuous_drift:'连续偏移', pending:'待认领', claimed:'已认领', superseded:'已替换', single_case:'单柜升级', group_candidate:'同厅归并'}[value] || value);
const notify = (message) => { state.notice = message; render(); setTimeout(() => { state.notice = ''; render(); }, 3600); };

function render() {
  document.querySelector('#app').innerHTML = state.token ? shell() : login();
  bind();
}

function login() {
  return `<main class="login-screen">
    <section class="login-panel">
      <div class="login-mark">MCA / 1069</div>
      <p class="eyebrow">MUSEUM CASE ANOMALY</p>
      <h1>展柜环境异常<br><em>夜班值守台</em></h1>
      <p class="login-copy">把门磁上下文、环境采样与文物巡检放在同一条处置链上，让每一次异常都有来路、动作和恢复记录。</p>
      <form id="login-form" class="login-form">
        <label>值守账号<input name="username" placeholder="lin-operator" value="lin-operator" required></label>
        <label>通行口令<input name="password" type="password" placeholder="museum123" value="museum123" required></label>
        <button class="button primary" type="submit">进入夜班台 <span>↗</span></button>
      </form>
      <div class="login-hint">演示账号：lin-operator / guo-supervisor / viewer<br>统一口令 museum123</div>
    </section>
    <aside class="login-visual"><div class="visual-grid"></div><div class="visual-stamp">18:40<br><strong>展厅照明<br>已切换夜间模式</strong></div><div class="visual-line"></div></aside>
  </main>`;
}

function shell() {
  const title = { monitor: '展柜监测', anomalies: '异常事件', inspections: '文物巡检', case: '展柜档案' }[state.page];
  return `<div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-dot"></span><div><strong>MCA</strong><small>夜班值守台</small></div></div>
      <div class="rail-note">馆藏环境 · 11 / 21<br><b>夜班窗口 18:00—07:00</b></div>
      <nav>
        ${nav('monitor','展柜监测','▧')}
        ${nav('anomalies','异常事件','◒')}
        ${nav('inspections','文物巡检','⊙')}
        ${nav('case','展柜档案','⌘')}
      </nav>
      <div class="sidebar-foot"><span class="pulse"></span>传感链路在线<div class="user-chip"><b>${esc(state.user?.display_name || '')}</b><small>${esc(state.user?.role || '')} · 末次登录刚刚</small></div><button id="logout" class="logout">退出夜班台</button></div>
    </aside>
    <main class="main-area">
      <header class="topbar"><div><p class="eyebrow">MUSEUM ENVIRONMENT CONTROL / 2026.11.21</p><h1>${title}</h1></div><div class="top-actions"><span class="clock">${new Date().toLocaleTimeString('zh-CN',{hour12:false})} <i>LOCAL</i></span><button class="icon-btn" id="refresh">↻</button></div></header>
      ${state.notice ? `<div class="notice">${esc(state.notice)}</div>` : ''}
      <section id="content"></section>
    </main>
  </div>`;
}
function nav(page, text, icon) { return `<button class="nav-item ${state.page===page?'active':''}" data-page="${page}"><span>${icon}</span>${text}<b>↗</b></button>`; }

async function loadData() {
  try {
    [state.dashboard, state.cases, state.anomalies, state.groups] = await Promise.all([api('/dashboard'), api('/cases'), api('/anomalies'), api('/anomaly-groups/candidates')]);
    if (state.page === 'case' && !state.selectedCase && state.cases[0]) state.selectedCase = state.cases[0].id;
    if (state.page === 'anomalies' && !state.selectedEvent && state.anomalies[0]) state.selectedEvent = state.anomalies[0].id;
    renderContent();
  } catch (error) { notify(error.message); }
}

function renderContent() {
  const target = document.querySelector('#content');
  if (!target) return;
  if (state.page === 'monitor') target.innerHTML = monitorPage();
  if (state.page === 'anomalies') target.innerHTML = anomaliesPage();
  if (state.page === 'inspections') target.innerHTML = inspectionsPage();
  if (state.page === 'case') target.innerHTML = casePage();
  bindPage();
}

function monitorPage() {
  const cards = [
    ['在岗展柜', state.dashboard.cases || 0, '全部处于主动采样'],
    ['24h 环境采样', state.dashboard.samples_today || 0, '按展柜与时序留痕'],
    ['待处置异常', state.dashboard.open_events || 0, '值守动作尚未闭环'],
    ['馆长升级', state.dashboard.escalated_events || 0, '连续偏移需要复核']
  ];
  return `<div class="metric-row">${cards.map((card, i) => `<div class="metric ${i===2?'warm':''}"><span>${card[0]}</span><strong>${card[1]}</strong><small>${card[2]}</small></div>`).join('')}</div>
    <div class="monitor-layout"><section class="panel cases-panel"><div class="panel-head"><div><p class="eyebrow">LIVE CASES / 01</p><h2>展厅里的四个呼吸点</h2></div><button class="text-button" data-page="case">查看档案 →</button></div><div class="case-list">${state.cases.map(caseRow).join('')}</div></section>
    <section class="panel side-panel"><div class="panel-head"><div><p class="eyebrow">NIGHT WATCH / 02</p><h2>值守提醒</h2></div></div><div class="watch-note"><div class="ring">${state.dashboard.door_interference || 0}</div><div><b>门开干扰</b><p>门磁开启段的突变只进入观察队列，关门复采后再判断设备状态。</p></div></div><div class="watch-rule"><span>算法链路</span><b>RobustScaler → IsolationForest</b><small>固定随机种子 1069 · 160 棵树</small></div><div class="watch-rule"><span>当前值守人</span><b>${esc(state.user?.display_name || '')}</b><small>角色 ${esc(state.user?.role || '')} · 审计实时写入</small></div></section></div>
    <section class="panel timeline-panel"><div class="panel-head"><div><p class="eyebrow">SHIFT TRACE / 03</p><h2>夜班交接线</h2></div><span class="status-dot">● 数据流动中</span></div><div class="timeline"><div><b>18:00</b><span>完成四柜夜间基线切换</span></div><div><b>18:20</b><span>MC-02 发现门开干扰，等待关门复采</span></div><div class="alert"><b>18:40</b><span>MC-03 连续偏移已升级馆长查看</span></div><div><b>19:00</b><span>文物巡检队列等待主管确认</span></div></div></section>`;
}
function caseRow(c) { const health = c.active_event_count ? 'attention' : 'steady'; return `<button class="case-row ${health}" data-case="${c.id}"><span class="case-code">${esc(c.code)}</span><span class="case-name"><b>${esc(c.artifact_title)}</b><small>${esc(c.gallery)} · ${esc(c.zone)}</small></span><span class="case-meta"><b>${c.sample_count}</b><small>采样点</small></span><span class="case-meta"><b>${c.active_event_count}</b><small>未闭环</small></span><span class="case-state">${c.active_event_count ? '需要值守' : '稳态'} <i>↗</i></span></button>`; }

function anomaliesPage() {
  const selected = state.anomalies.find(item => item.id === state.selectedEvent) || state.anomalies[0];
  return `${groupBoard()}<div class="split-layout"><section class="panel anomaly-list"><div class="panel-head"><div><p class="eyebrow">EVENT QUEUE / ${state.anomalies.length.toString().padStart(2,'0')}</p><h2>异常事件队列</h2></div><span class="filter-pill">全部 · ${state.anomalies.length}</span></div>${state.anomalies.length ? state.anomalies.map(eventRow).join('') : empty('目前没有待处置异常', '继续保持采样，新的异常会进入这里。')}</section><section class="panel detail-panel">${selected ? eventDetail(selected) : empty('选择一个异常事件', '从左侧队列打开处置详情。')}</section></div>`;
}

function groupBoard() {
  const candidates = state.groups.candidates || [];
  const singletons = state.groups.singletons || [];
  return `<section class="panel group-board">
    <div class="panel-head"><div><p class="eyebrow">GALLERY MERGE / 05</p><h2>同厅归并待认领</h2></div><span class="filter-pill">${candidates.filter(c => c.status === 'pending').length} 条待认领 · ${singletons.length} 条单柜升级</span></div>
    <p class="group-hint">同厅展柜温度与湿度同向偏移、采样时段重叠且异常评分接近时归并为一条候选。认领后处置单保留当时的成员与依据；越过保护范围的告警不参与归并，仍按单柜升级。</p>
    <div class="candidate-list">${candidates.length ? candidates.map(candidateCard).join('') : `<div class="group-empty">当前没有满足归并条件的同厅告警。</div>`}</div>
    <div class="singleton-block"><p class="eyebrow">SINGLE CASE ESCALATION</p>${singletons.length ? `<div class="singleton-list">${singletons.map(singletonRow).join('')}</div>` : '<div class="group-empty">没有越过保护范围的单柜告警。</div>'}</div>
  </section>`;
}

function candidateCard(c) {
  const ticket = c.ticket;
  const memberCodes = [...new Set((c.members || []).map(m => m.case_code))];
  const claimable = c.status === 'pending' && ['operator', 'supervisor'].includes(state.user?.role);
  return `<article class="candidate-card ${c.status}">
    <header><span class="candidate-gallery">${esc(c.gallery)}</span><span class="candidate-status">${label(c.status)}</span></header>
    <b class="candidate-direction">${esc(c.direction_label)}</b>
    <div class="candidate-metrics"><span><small>展柜</small>${c.member_count}</span><span><small>时段</small>${fmt(c.window_start)} → ${fmt(c.window_end).slice(-8)}</span><span><small>评分</small>${Number(c.score_min).toFixed(3)} ~ ${Number(c.score_max).toFixed(3)}</span></div>
    <p class="candidate-members">${memberCodes.map(code => `<span>${esc(code)}</span>`).join('')}</p>
    <p class="candidate-basis">${esc(c.evidence?.rule || '')}</p>
    ${ticket ? `<p class="candidate-ticket">处置单 #${ticket.id} · ${esc(ticket.claimed_by_name || '')} 已认领 · ${ticketLabel[ticket.status] || ticket.status}</p>` : ''}
    ${claimable ? `<div class="claim-box"><input class="claim-note" data-claim-note="${c.id}" placeholder="认领依据（至少 4 字）" maxlength="500" value="${esc(state.claimNote)}"><button class="button primary" data-claim="${c.id}">认领派单</button></div>` : ''}
  </article>`;
}

function singletonRow(s) {
  return `<div class="singleton-row"><span class="case-code">${esc(s.case_code)}</span><span><b>${esc(s.artifact_title)}</b><small>${esc(s.gallery)} · ${fmt(s.sampled_at)} · ${(s.breaches || []).join(' / ') || '越过保护范围'}</small></span><strong>单柜升级</strong></div>`;
}

function groupBadge(e) {
  if (e.ticket) return `<span class="group-tag ticket">处置单 #${e.ticket.id}</span>`;
  if (e.candidate && e.candidate.status === 'pending') return `<span class="group-tag">同厅归并 · ${esc(e.candidate.gallery)}</span>`;
  if (e.escalation_basis === 'single_case') return `<span class="group-tag single">单柜升级</span>`;
  return '';
}

function eventRow(e) { return `<button class="event-row ${state.selectedEvent===e.id?'selected':''}" data-event="${e.id}"><span class="severity ${e.severity}">${e.severity==='critical'?'!':'·'}</span><span><b>${esc(e.case_code)} · ${label(e.anomaly_type)}</b><small>${fmt(e.sampled_at)} · ${esc(e.reason)}</small>${groupBadge(e)}</span><strong>${label(e.status)}</strong></button>`; }

function groupEvidence(e) {
  if (e.ticket) return `<div class="evidence-card"><p class="eyebrow">CLAIMED TICKET / 处置单</p><p>处置单 #${e.ticket.id} 由 ${esc(e.ticket.claimed_by_name || '')} 于 ${fmt(e.ticket.claimed_at)} 认领，当前状态「${ticketLabel[e.ticket.status] || e.ticket.status}」。派单保留认领时的成员与依据。</p></div>`;
  if (e.candidate) {
    const members = e.candidate.members || [];
    return `<div class="evidence-card"><p class="eyebrow">GROUP BASIS / 成组依据</p><p><b>${esc(e.candidate.gallery)}</b> · ${esc(e.candidate.direction_label)} · ${e.candidate.member_count} 个展柜 · 时段 ${fmt(e.candidate.window_start)} → ${fmt(e.candidate.window_end)}</p><ul class="evidence-members">${members.map(m => `<li><span class="case-code">${esc(m.case_code)}</span><span>${esc(m.artifact_title)}</span><small>${Number(m.score).toFixed(3)} · ${fmt(m.sampled_at)}</small></li>`).join('')}</ul><small>${esc(e.candidate.basis || '')}</small></div>`;
  }
  if (e.escalation_basis === 'single_case') return `<div class="evidence-card single"><p class="eyebrow">SINGLE CASE ESCALATION / 单柜升级</p><p>该告警的采样越过展柜温湿保护范围，不参与同厅归并，保持单柜升级处理。</p></div>`;
  return '';
}

function eventDetail(e) { return `<div class="detail-kicker"><span class="severity ${e.severity}">${e.severity}</span><span>${label(e.anomaly_type)}</span><span>${label(e.status)}</span></div><h2>${esc(e.case_code)} / ${esc(e.artifact_title)}</h2><p class="detail-copy">${esc(e.reason)}</p>${groupEvidence(e)}<div class="data-strip"><div><small>异常分数</small><b>${Number(e.score).toFixed(3)}</b></div><div><small>采样时间</small><b>${fmt(e.sampled_at)}</b></div><div><small>门磁</small><b>${e.door_open?'开启干扰':'关闭'}</b></div></div><div class="measure-grid"><div><span>温度</span><b>${e.temperature} °C</b></div><div><span>相对湿度</span><b>${e.humidity} %</b></div><div><span>CO₂</span><b>${e.co2_ppm} ppm</b></div><div><span>连续计数</span><b>${e.consecutive_count}</b></div></div><div class="action-box"><p class="eyebrow">DUTY ACTION / 值守处置</p><div class="action-buttons">${e.door_context?'<button class="button amber" data-action="door">完成关门复核</button>':'<button class="button" data-action="adjust">记录环境调整</button>'}<button class="button" data-action="inspect">安排文物巡检</button>${e.status==='escalated'?'<button class="button dark" data-action="curator">记录馆长处置</button>':''}<button class="button primary" data-action="close">关闭异常</button></div><small>关闭条件：已完成值守动作 + 文物巡检通过。升级事件需追加馆长处置记录。</small></div>`; }

function inspectionsPage() {
  return `<div class="inspection-layout"><section class="panel inspection-intro"><p class="eyebrow">ARTIFACT CHECK / POST-INCIDENT</p><h2>巡检是异常闭环的最后一道门</h2><p>每一次环境异常都要留下文物状态证据。巡检通过后，主管才可以关闭异常。失败会把事件重新打开，并保留原因与操作者。</p><div class="inspection-mark">⊙</div></section><section class="panel inspection-table"><div class="panel-head"><div><p class="eyebrow">RECENT CHECKS</p><h2>最近巡检</h2></div><button class="text-button" data-page="anomalies">回到异常队列 →</button></div>${state.anomalies.map(e => `<div class="inspection-row"><span class="case-code">${esc(e.case_code)}</span><span><b>${esc(e.artifact_title)}</b><small>${label(e.anomaly_type)} · ${fmt(e.created_at)}</small></span><span class="result ${e.passed_checks ? 'pass' : 'pending'}">${e.passed_checks ? '已通过' : '待巡检'}</span><button class="text-button" data-event="${e.id}">打开 →</button></div>`).join('')}</section></div>`;
}
function casePage() { const current = state.cases.find(c => c.id === state.selectedCase) || state.cases[0]; return `<div class="case-layout"><section class="panel case-directory"><div class="panel-head"><div><p class="eyebrow">DISPLAY CASE REGISTER / 04</p><h2>展柜档案</h2></div><span class="filter-pill">${state.cases.length} 个在岗柜</span></div>${state.cases.map(c => `<button class="directory-item ${current?.id===c.id?'selected':''}" data-case="${c.id}"><span>${esc(c.code)}</span><b>${esc(c.artifact_title)}</b><small>${esc(c.zone)}</small></button>`).join('')}</section><section class="panel case-detail">${current ? caseDetail(current) : empty('没有展柜档案', '先创建一个在岗展柜。')}</section></div>`; }
function caseDetail(c) { return `<div class="case-heading"><div><p class="eyebrow">${esc(c.gallery)} / ${esc(c.zone)}</p><h2>${esc(c.artifact_title)}</h2><span class="case-code">${esc(c.code)} · ${esc(c.artifact_code)}</span></div><span class="case-status">${label(c.status)}</span></div><div class="threshold-band"><div><small>温度窗口</small><b>${c.target_temp_min} — ${c.target_temp_max} °C</b></div><div><small>湿度窗口</small><b>${c.target_rh_min} — ${c.target_rh_max} %</b></div><div><small>门磁编号</small><b>${esc(c.door_sensor_id)}</b></div></div><div class="sample-table"><div class="table-title"><span>最近环境采样</span><small>${c.samples?.length || c.sample_count} 条记录</small></div>${(c.samples || []).slice(0,8).map(s => `<div class="sample-row"><span>${fmt(s.sampled_at)}</span><b>${s.temperature} °C</b><b>${s.humidity} %</b><b>${s.co2_ppm} ppm</b><span class="door-tag ${s.door_open?'open':''}">${s.door_open?'门开':'闭合'}</span></div>`).join('')}</div><button class="button primary" id="analyze-case" data-case-id="${c.id}">重新运行异常识别 ↗</button></div>`; }
function empty(title, copy) { return `<div class="empty"><div>○</div><h3>${title}</h3><p>${copy}</p></div>`; }

function bind() {
  document.querySelector('#login-form')?.addEventListener('submit', async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { const data = await api('/login', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password') }) }); state.token = data.access_token; state.user = data.user; localStorage.setItem('museum_token', state.token); localStorage.setItem('museum_user', JSON.stringify(state.user)); render(); await loadData(); } catch (error) { alert(error.message); } });
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => { state.page = button.dataset.page; render(); loadData(); }));
  document.querySelector('#logout')?.addEventListener('click', () => { state.token = null; state.user = null; localStorage.clear(); render(); });
  document.querySelector('#refresh')?.addEventListener('click', () => loadData());
  bindPage();
}
function bindPage() {
  document.querySelectorAll('[data-case]').forEach(button => button.addEventListener('click', async () => { state.selectedCase = Number(button.dataset.case); if (state.page === 'monitor') { state.page='case'; } render(); await loadData(); if (state.page === 'case') { const detail = await api(`/cases/${state.selectedCase}`); state.cases = state.cases.map(c => c.id===state.selectedCase ? {...c, ...detail} : c); renderContent(); } }));
  document.querySelectorAll('[data-event]').forEach(button => button.addEventListener('click', async () => { state.selectedEvent = Number(button.dataset.event); state.page='anomalies'; render(); await loadData(); }));
  document.querySelectorAll('[data-claim-note]').forEach(input => input.addEventListener('input', () => { state.claimNote = input.value; }));
  document.querySelectorAll('[data-claim]').forEach(button => button.addEventListener('click', () => claimCandidate(Number(button.dataset.claim), button)));
  document.querySelector('#analyze-case')?.addEventListener('click', async (event) => { try { const run = await api(`/analyze?case_id=${event.currentTarget.dataset.caseId}`, { method: 'POST' }); notify(`算法任务 #${run.id} 已入队，worker 正在处理`); pollRun(run.id); } catch (error) { notify(error.message); } });
  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => handleAction(button.dataset.action)));
}
async function pollRun(id) { for (let i=0;i<15;i++) { await new Promise(r => setTimeout(r, 1000)); const run = await api(`/runs/${id}`); if (run.status === 'succeeded') { notify(`异常识别完成，发现 ${run.metrics.outlier_count} 个候选事件`); await loadData(); return; } if (run.status === 'failed') { notify(run.error); return; } } }
async function claimCandidate(id, button) {
  const candidate = (state.groups.candidates || []).find(item => item.id === id);
  const note = (state.claimNote || '').trim();
  if (!candidate) return;
  if (note.length < 4) { notify('请填写至少 4 个字的认领依据'); return; }
  const requestId = `claim-${id}-${candidate.revision}-${state.user?.id}-${Date.now()}`;
  button.disabled = true;
  try {
    const ticket = await api(`/anomaly-groups/candidates/${id}/claim`, { method: 'POST', body: JSON.stringify({ revision: candidate.revision, note, request_id: requestId }) });
    state.claimNote = '';
    notify(`处置单 #${ticket.id} 已认领，保留 ${ticket.members.length} 条告警与认领依据`);
    await loadData();
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
}
async function handleAction(action) { const e = state.anomalies.find(item => item.id === state.selectedEvent); if (!e) return; try { if (action === 'close') { await api(`/anomalies/${e.id}/close`, { method: 'POST' }); notify('异常已关闭'); } else if (action === 'inspect') { await api(`/anomalies/${e.id}/artifact-checks`, { method: 'POST', body: JSON.stringify({case_id:e.case_id,event_id:e.id,check_type:'post_incident',result:'pass',notes:'夜班巡检确认展柜玻璃、底座与文物状态正常'}) }); notify('文物巡检已记录为通过'); } else { const map = { door:'verify_door_sealed', adjust:'adjust_climate', curator:'call_curator' }; await api(`/anomalies/${e.id}/duty-actions`, { method:'POST', body: JSON.stringify({action_type:map[action], notes: action==='door'?'门已关闭，复采前完成门磁复核':'已按值守清单完成处置记录', status:'completed'}) }); notify('值守动作已写入审计'); } await loadData(); } catch (error) { notify(error.message); } }

render();
if (state.token) loadData();
