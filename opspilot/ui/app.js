'use strict';
/* OpsPilot console. Plain JS, no build step. State is fetched from the JSON API and re-rendered
   only when its signature changes, so open panels and scroll positions survive polling. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let m = await r.text(); try { m = JSON.parse(m).detail || m; } catch { /* plain text */ } throw new Error(m); }
  return (r.headers.get('content-type') || '').includes('json') ? r.json() : r.text();
}
const post = (p, b) => api(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b || {}) });
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };

function toast(msg, kind = '') {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), 4200);
}
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return 'just now'; if (s < 60) return Math.round(s) + 's ago'; if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago'; return new Date(ts * 1000).toLocaleDateString();
}
const dur = s => s < 60 ? s.toFixed(s < 10 ? 1 : 0) + 's' : Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
const pct = v => v == null ? '–' : +(v * 100).toFixed(1) + '%';

/* ------------------------------------------------------------------ vocabulary */
const SER = {
  'api.error_rate': { t: 'Error rate', u: 'api 5xx responses', f: v => (v * 100).toFixed(v < 0.1 ? 1 : 0) + '%' },
  'api.p99_latency_s': { t: 'p99 latency', u: 'api requests', f: v => v < 1 ? Math.round(v * 1000) + ' ms' : v.toFixed(2) + ' s' },
  'api.request_rate': { t: 'Throughput', u: 'api requests', f: v => v.toFixed(0) + ' rps' },
  'api.db_queries_per_request': { t: 'DB queries per request', u: 'N+1 shows up here', f: v => v.toFixed(1) },
  'api.db_pool_in_use': { t: 'DB pool in use', u: 'connections checked out', f: v => v.toFixed(0) },
  'api.db_pool_timeouts_per_s': { t: 'DB pool timeouts', u: 'waiting for a connection', f: v => v.toFixed(1) + '/s' },
  'api.memory_mb': { t: 'API heap', u: 'python heap in use', f: v => v.toFixed(0) + ' MB' },
  'api.cpu_percent': { t: 'API CPU', u: 'process', f: v => v.toFixed(0) + '%' },
  'api.payments_p99_s': { t: 'Payments call p99', u: 'third party', f: v => v < 1 ? Math.round(v * 1000) + ' ms' : v.toFixed(2) + ' s' },
  'worker.queue_depth': { t: 'Queue depth', u: 'pending jobs', f: v => v.toFixed(0) },
  'worker.queue_oldest_age_s': { t: 'Oldest job age', u: 'queue', f: v => v.toFixed(0) + ' s' },
  'worker.job_failures_per_s': { t: 'Job failures', u: 'worker attempts that raised', f: v => v.toFixed(1) + '/s' },
};
const serFmt = k => (SER[k]?.f) || (v => String(+v.toFixed(2)));
const CAT = { bad_deploy: 'Bad deploy', config_change: 'Config change', flag_change: 'Feature-flag change', downstream_dependency: 'Downstream dependency', poison_message: 'Poison message', unknown: 'Unknown' };
const STATUS = { detected: 'Detected', investigating: 'Investigating', proposed: 'Fix proposed', awaiting_approval: 'Awaiting approval', executing: 'Executing', verifying: 'Verifying', resolved: 'Resolved', escalated: 'Escalated to human', failed: 'Failed' };
const LIVE = new Set(['detected', 'investigating', 'proposed', 'awaiting_approval', 'executing', 'verifying']);
const TOOLICON = { find_anomalies: '∿', correlate_changes: '⇄', log_patterns: '≣', search_logs: '⌕', list_changes: '☰', get_change_diff: '±', get_config: '⚙', query_series: '▂▆', query_metric_raw: 'Σ' };
const pill = (cls, txt, live) => `<span class="pill ${cls}${live ? ' live' : ''}">${esc(txt)}</span>`;
const statusPill = (s, big) => `<span class="pill ${s}${LIVE.has(s) && s !== 'proposed' && s !== 'detected' ? ' live' : ''}${big ? ' status-big' : ''}">${esc(STATUS[s] || s)}</span>`;
function actStr(a) {
  switch (a.type) {
    case 'rollback_deploy': return `rollback_deploy  ${a.service} → v${a.to_version}`;
    case 'restart_service': return `restart_service  ${a.service}`;
    case 'revert_config': return `revert_config  ${a.service}.${a.key} → ${JSON.stringify(a.value)}`;
    case 'escalate': return `escalate  ${a.reason || ''}`;
    default: return JSON.stringify(a);
  }
}

/* ------------------------------------------------------------------ state + routing */
const S = { meta: null, incidents: [], page: null, token: 0, sig: '', detail: null, timer: null,
  ui: { tab: 'trace', open: {}, allCharts: false, filter: 'all', q: '', hl: null }, launch: null, evalSel: null };

const route = () => { const [a, b] = location.hash.replace(/^#\/?/, '').split('/'); return { page: a || 'overview', id: b }; };
function schedule(fn, ms, token) {
  clearTimeout(S.timer);
  S.timer = setTimeout(async () => {
    if (token !== S.token) return;
    try { await fn(); } catch (e) { console.error(e); }
    if (token === S.token) schedule(fn, typeof ms === 'function' ? ms() : ms, token);
  }, typeof ms === 'function' ? ms() : ms);
}
async function mount() {
  const r = route(); S.token++; S.sig = ''; S.page = r.page; S.detail = null;
  $$('.nav a.item').forEach(a => a.classList.toggle('on', a.dataset.nav === (r.page === 'incident' ? 'incidents' : r.page)));
  const tok = S.token;
  if (!S.meta) S.meta = await api('/api/meta');
  $('#llmTxt').textContent = S.meta.llm_ready ? `LLM ready · ${S.meta.provider} · ${S.meta.model}` : 'LLM agents: no API key';
  const P = { overview: pageOverview, incidents: pageIncidents, incident: pageIncident, benchmark: pageBenchmark, scenarios: pageScenarios }[r.page] || pageOverview;
  $('#view').innerHTML = '<div class="grid"><div class="skel" style="height:90px"></div><div class="skel" style="height:260px"></div></div>';
  window.scrollTo(0, 0);
  await P(tok, r);
}
const crumbs = (...xs) => { $('#crumbs').innerHTML = xs.map((x, i) => i === xs.length - 1 ? `<b>${esc(x)}</b>` : esc(x)).join(' <span class="dim">/</span> '); };

/* ------------------------------------------------------------------ global poll (badge, chain) */
async function globalPoll() {
  try {
    S.incidents = await api('/api/incidents');
    const n = S.incidents.filter(i => i.status === 'awaiting_approval').length;
    const b = $('#navBadge'); b.hidden = !n; b.textContent = n;
    document.title = (n ? `(${n}) ` : '') + 'OpsPilot';
  } catch { /* server restarting */ }
}
async function chainPoll() {
  try {
    const v = await api('/api/audit/verify');
    $('#chainDot').className = 'dot ' + (v.ok ? 'ok' : 'bad');
    $('#chainTxt').textContent = v.ok ? `audit chain intact · ${v.entries}` : `AUDIT CHAIN BROKEN at #${v.first_bad_seq}`;
  } catch { /* ignore */ }
}

/* ------------------------------------------------------------------ shared fragments */
function incTable(list, empty) {
  if (!list.length) return `<div class="empty">${empty || 'No incidents yet.'}</div>`;
  return `<div class="tablewrap"><table><thead><tr><th>Incident</th><th>Status</th><th>Root cause</th><th>Severity</th><th>Age</th><th class="nowrap">Duration</th></tr></thead><tbody>` +
    list.map(i => `<tr class="row" data-href="#/incident/${i.id}">
      <td><b>${esc(i.title || i.scenario || i.id)}</b><div class="mono dim">${i.id}</div></td>
      <td>${statusPill(i.status)}</td>
      <td>${i.root_cause ? `${esc(CAT[i.root_cause.category] || i.root_cause.category)} <span class="chip">${esc(i.root_cause.service)}</span><div class="dim" style="font-size:12px">${Math.round(i.root_cause.confidence * 100)}% confidence</div>` : '<span class="dim">–</span>'}</td>
      <td>${i.severity ? `<span class="sev-${i.severity}">● ${i.severity}</span>` : '–'}</td>
      <td class="mut nowrap">${ago(i.created_at)}</td><td class="mono nowrap">${i.terminal ? dur(i.duration_s) : '<span class="dim">running</span>'}</td></tr>`).join('') + '</tbody></table></div>';
}
function scenarioTiles(withBtn = true) {
  return `<div class="tiles">` + S.meta.scenarios.map(s => `<div class="tile">
    <div style="display:flex;gap:8px;align-items:center"><span class="diff-${s.difficulty}" title="${s.difficulty}">${'●'.repeat({ easy: 1, medium: 2, hard: 3 }[s.difficulty])}<span class="dim">${'●'.repeat(3 - { easy: 1, medium: 2, hard: 3 }[s.difficulty])}</span></span><span class="chip">${esc(s.alert.service)}</span><span class="sev-${s.alert.severity}" style="font-size:12px;margin-left:auto">${s.alert.severity}</span></div>
    <h4>${esc(s.title)}</h4><p>${esc(s.description)}</p>
    <div class="meta"><span class="mut">${esc(s.alert.name)}</span>${withBtn ? `<button class="btn sm pri" style="margin-left:auto" data-act="launch" data-sc="${s.id}" ${s.snapshot ? '' : 'disabled title="not recorded yet"'}>Run</button>` : ''}</div></div>`).join('') + '</div>';
}
const EV_LABEL = { incident_created: ['+', 'Incident opened'], alert_received: ['!', 'Alert received'], findings: ['◎', 'Root cause proposed'], policy_decision: ['⚖', 'Policy decision'],
  approval_requested: ['✋', 'Approval requested'], approval_approved: ['✓', 'Approved'], approval_rejected: ['✗', 'Rejected'], executed: ['⚡', 'Fix executed'], verification: ['◉', 'Recovery verified'],
  success_criteria_declared: ['◈', 'Success criteria declared'], agent_finished: ['■', 'Agent finished'], resumed: ['↻', 'Resumed after restart'] };
function evLabel(e) { if (e.startsWith('status:')) return ['→', 'Status: ' + (STATUS[e.slice(7)] || e.slice(7))]; return EV_LABEL[e] || ['·', e.replace(/_/g, ' ')]; }

/* ------------------------------------------------------------------ pages: overview */
async function pageOverview(tok) {
  crumbs('Overview');
  const refresh = async () => {
    const [inc, feed] = await Promise.all([api('/api/incidents'), api('/api/feed?limit=30')]);
    if (tok !== S.token) return;
    S.incidents = inc;
    const sig = JSON.stringify([inc.map(i => [i.id, i.status, i.updated_at]), feed[0]?.seq]);
    if (sig === S.sig) return; S.sig = sig;
    const done = inc.filter(i => i.terminal), res = inc.filter(i => i.status === 'resolved'), esc_ = inc.filter(i => i.status === 'escalated');
    const pend = inc.filter(i => i.status === 'awaiting_approval');
    const ttr = res.map(i => i.duration_s).sort((a, b) => a - b), med = ttr.length ? ttr[Math.floor(ttr.length / 2)] : null;
    $('#view').innerHTML = `
    <div class="page-h"><div><h1>Operations overview</h1><p>OpsPilot investigates alerts from logs, metrics and deploy history, cites its evidence, asks a human before touching anything, then proves the fix worked.</p></div><div class="sp"></div></div>
    <div class="grid g4" style="margin-bottom:16px">
      <div class="card kpi"><div class="l">Incidents</div><div class="v">${inc.length}</div><div class="s">${inc.length - done.length} in progress</div></div>
      <div class="card kpi"><div class="l">Resolved</div><div class="v">${res.length}<small>${done.length ? pct(res.length / done.length) : ''}</small></div><div class="s">of ${done.length} finished</div></div>
      <div class="card kpi"><div class="l">Escalated to human</div><div class="v">${esc_.length}</div><div class="s">no safe automated fix</div></div>
      <div class="card kpi ${pend.length ? 'warn' : ''}"><div class="l">Awaiting approval</div><div class="v" style="${pend.length ? 'color:var(--warn)' : ''}">${pend.length}</div><div class="s">${pend.length ? `<a href="#/incident/${pend[0].id}">review now →</a>` : 'nothing blocked on you'}</div></div>
      <div class="card kpi"><div class="l">Median time to resolve</div><div class="v">${med == null ? '–' : dur(med)}</div><div class="s">${S.incidents.some(i => i.active) ? 'demo pacing inflates this' : 'investigate → verified'}</div></div>
    </div>
    ${pend.length ? `<div class="card approval" style="margin-bottom:16px"><h2>${pend.length} fix${pend.length > 1 ? 'es' : ''} waiting for your approval</h2>${pend.map(i => `<div class="act-row"><b>${esc(i.title)}</b><span class="mut" style="flex:1">${esc(i.pending_plan || '')}</span><a class="btn sm" href="#/incident/${i.id}">Review</a></div>`).join('')}</div>` : ''}
    <div class="card" style="margin-bottom:16px"><h3>Simulate an incident <span class="sp"></span><span class="mut" style="text-transform:none;letter-spacing:0;font-weight:500">8 fault-injected scenarios with hidden ground truth</span></h3>${scenarioTiles()}</div>
    <div class="grid" style="grid-template-columns:minmax(0,1.7fr) minmax(0,1fr)" id="ovgrid">
      <div class="card"><h3>Recent incidents <span class="sp"></span><a href="#/incidents" style="text-transform:none;letter-spacing:0">View all →</a></h3>${incTable(inc.slice(0, 7), 'No incidents yet. Pick a scenario above and press Run.')}</div>
      <div class="card"><h3>Activity</h3><div class="feed">${feed.length ? feed.map(f => { const [ic, lab] = evLabel(f.event); return `<a href="#/incident/${f.incident_id}"><span class="ic">${ic}</span><span>${esc(lab)}<br><small>${esc(f.actor)} · <span class="mono">${f.incident_id}</span></small></span><small>${ago(f.ts)}</small></a>`; }).join('') : '<div class="empty">Nothing yet.</div>'}</div></div>
    </div>`;
    if (innerWidth < 1000) $('#ovgrid').style.gridTemplateColumns = '1fr';
  };
  await refresh(); schedule(refresh, 2000, tok);
}

/* ------------------------------------------------------------------ pages: incidents list */
async function pageIncidents(tok) {
  crumbs('Incidents');
  const refresh = async () => {
    const inc = await api('/api/incidents'); if (tok !== S.token) return; S.incidents = inc;
    const sig = JSON.stringify([inc.map(i => [i.id, i.status, i.updated_at]), S.ui.filter, S.ui.q]);
    if (sig === S.sig) return; S.sig = sig; paint();
  };
  const paint = () => {
    const f = S.ui.filter, q = S.ui.q.toLowerCase();
    const counts = s => S.incidents.filter(i => s === 'all' ? true : s === 'open' ? !i.terminal : i.status === s).length;
    const list = S.incidents.filter(i => (f === 'all' || (f === 'open' ? !i.terminal : i.status === f)) && (!q || (i.title + i.id + (i.scenario || '') + (i.root_cause?.category || '')).toLowerCase().includes(q)));
    $('#view').innerHTML = `<div class="page-h"><div><h1>Incidents</h1><p>Every investigation, with its evidence, approvals and tamper-evident audit trail.</p></div><div class="sp"></div>
      <input type="text" id="q" placeholder="Search…" value="${esc(S.ui.q)}" style="width:220px"></div>
      <div class="chips" style="margin-bottom:14px">${['all', 'open', 'awaiting_approval', 'resolved', 'escalated', 'failed'].map(s => `<button class="seg-b btn sm ${f === s ? 'pri' : 'ghost'}" data-act="filter" data-f="${s}">${s === 'all' ? 'All' : s === 'open' ? 'In progress' : STATUS[s]} · ${counts(s)}</button>`).join('')}</div>
      <div class="card">${incTable(list, 'No incidents match.')}</div>`;
    const q_ = $('#q'); q_.addEventListener('input', () => { S.ui.q = q_.value; const pos = q_.selectionStart; S.sig = ''; paint(); const n = $('#q'); n.focus(); n.setSelectionRange(pos, pos); });
  };
  await refresh(); schedule(refresh, 2000, tok);
}

/* ------------------------------------------------------------------ pages: scenarios */
async function pageScenarios() {
  crumbs('Scenarios');
  $('#view').innerHTML = `<div class="page-h"><div><h1>Scenarios</h1><p>Each one is a real fault injected into a running orders system (API + worker) and recorded. The expected root cause and fix are hidden from the agent and used only to grade it afterwards.</p></div></div>
    <div class="card">${scenarioTiles()}</div>
    <div class="callout" style="margin-top:16px"><b>Replay</b> serves the recorded telemetry deterministically, including the recovery that follows the <i>correct</i> fix, so a wrong fix visibly fails to recover. <b>Live</b> starts the real processes, injects the fault, and executes the approved action against them (about 70 seconds).</div>`;
}

/* ------------------------------------------------------------------ pages: benchmark */
async function pageBenchmark(tok) {
  crumbs('Benchmark');
  const refresh = async () => {
    const d = await api('/api/eval'); if (tok !== S.token) return;
    const sig = JSON.stringify([d.generated_at, d.job.running, d.job.done, d.job.error]); if (sig === S.sig) return; S.sig = sig;
    const agents = Object.keys(d.aggregate), A = S.meta.agents;
    const sel = S.evalSel || (S.evalSel = new Set(['null', 'heuristic', 'oracle', 'oracle-wrong']));
    const lbl = id => (A.find(a => a.id === id) || { label: id }).label;
    const kind = id => (A.find(a => a.id === id) || {}).kind;
    const noLLM = !agents.some(a => ['llm', 'llm-basic', 'single-prompt'].includes(a));
    const j = d.job;
    $('#view').innerHTML = `<div class="page-h"><div><h1>Benchmark</h1><p>The same 8 incidents, every agent, graded on outcomes: right root cause, right action, verified recovery, and whether a wrong or unapproved action ever ran.</p></div><div class="sp"></div>
      <button class="btn pri" data-act="evalrun" ${j.running ? 'disabled' : ''}>${j.running ? 'Running…' : '▶ Run benchmark'}</button></div>
      ${j.running ? `<div class="card" style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><b>Running ${j.done}/${j.total}</b><span class="mut mono">${esc((j.log || []).slice(-1)[0] || '')}</span></div><div class="progress"><i style="width:${j.total ? j.done / j.total * 100 : 0}%"></i></div></div>` : ''}
      ${j.error ? `<div class="callout" style="border-color:var(--bad);margin-bottom:16px"><b>Benchmark failed:</b> ${esc(j.error)}</div>` : ''}
      <div class="card" style="margin-bottom:16px"><h3>Agents to include</h3><div class="chips">${A.map(a => `<label class="opt ${a.available ? '' : 'off'} ${sel.has(a.id) && a.available ? 'on' : ''}" style="padding:6px 10px" title="${esc(a.available ? a.desc : a.why)}"><input type="checkbox" data-act="evalsel" data-id="${a.id}" ${sel.has(a.id) && a.available ? 'checked' : ''} ${a.available ? '' : 'disabled'}><span><b style="font-size:13px">${esc(a.label)}</b></span></label>`).join('')}</div></div>
      ${noLLM ? `<div class="callout" style="margin-bottom:16px"><b>No LLM agent has been benchmarked yet.</b> ${S.meta.llm_ready ? 'Tick the LLM agents above and run it to compare them with the runbook baseline.' : 'Put <code>GEMINI_API_KEY</code> (free tier) or <code>ANTHROPIC_API_KEY</code> in <code>.env</code>, restart the server, then run it here. The numbers below are only the non-LLM baselines and harness self-tests.'}</div>` : ''}
      ${!agents.length ? '<div class="card empty">No results yet. Press “Run benchmark”.</div>' : `
      <div class="lb" style="margin-bottom:16px">${agents.map(a => { const m = d.aggregate[a]; const col = m.success >= .75 ? 'var(--ok)' : m.success >= .4 ? 'var(--warn)' : 'var(--bad)'; return `<div class="card lbc">
        <div style="display:flex;gap:8px;align-items:center"><span class="name">${esc(lbl(a))}</span>${kind(a) === 'selftest' ? pill('', 'harness test') : kind(a) === 'baseline' ? pill('', 'baseline') : ''}${d.models[a] ? `<span class="chip" title="model that produced these numbers">${esc(d.models[a])}</span>` : ''}</div>
        <div class="big" style="color:${col}">${pct(m.success)}</div><div class="sub">end-to-end success over ${m.episodes} episodes</div><div class="bar" style="margin-top:8px"><i style="width:${(m.success || 0) * 100}%;background:${col}"></i></div>
        <div class="stat"><span>Root cause correct</span><b>${pct(m.rca_top1)}</b><span>Right first action</span><b>${pct(m.action_correct)}</b><span>Wrong action executed</span><b style="color:${m.wrong_executed ? 'var(--bad)' : 'inherit'}">${pct(m.wrong_executed)}</b><span>Evidence precision / recall</span><b>${pct(m.ev_precision)} / ${pct(m.ev_recall)}</b><span>Verification correct</span><b>${pct(m.verify_correct)}</b><span>Unapproved executions</span><b style="color:${m.unapproved_exec ? 'var(--bad)' : 'var(--ok)'}">${m.unapproved_exec}</b><span>Tool calls / incident</span><b>${m.tool_calls == null ? '–' : m.tool_calls.toFixed(1)}</b></div></div>`; }).join('')}</div>
      <div class="card"><h3>Per scenario</h3><div class="tablewrap"><table class="heat"><thead><tr><th>Scenario</th>${agents.map(a => `<th>${esc(lbl(a))}</th>`).join('')}</tr></thead><tbody>${d.scenarios.map(sid => {
        const sc = S.meta.scenarios.find(s => s.id === sid);
        return `<tr><td><b>${esc(sc ? sc.title : sid)}</b><div class="mono dim">${sid}</div></td>${agents.map(a => { const rs = d.rows.filter(r => r.agent === a && r.scenario === sid); if (!rs.length) return '<td><span class="cell m">–</span></td>'; const ok = rs.filter(r => r.success).length / rs.length; const r0 = rs[0]; const tip = `${r0.status}${r0.verdict ? ' · ' + r0.verdict : ''}${r0.wrong_executed ? ' · WRONG ACTION RAN' : ''}${r0.red_herring ? ' · blamed the red herring' : ''}`; return `<td title="${esc(tip)}"><span class="cell ${ok >= .99 ? 'y' : ok <= .01 ? 'n' : 'm'}">${ok >= .99 ? '✓' : ok <= .01 ? '✗' : Math.round(ok * 100) + '%'}</span></td>`; }).join('')}</tr>`;
      }).join('')}</tbody></table></div><p class="mut" style="font-size:12.5px;margin:12px 0 0">Success needs the right root cause <i>and</i> the right end state: verified recovery after a correct fix, or a clean escalation when nothing on our side can fix it. Hover a cell for details. Evidence precision and recall are structural matches, not a semantic judgement.</p></div>`}`;
  };
  await refresh(); schedule(refresh, () => $('.progress') ? 700 : 4000, tok);
}

/* ------------------------------------------------------------------ pages: incident war room */
async function pageIncident(tok, r) {
  const id = r.id; crumbs('Incidents', id);
  S.ui.open = {}; S.ui.hl = null; S.ui.tab = 'trace'; S.ui.allCharts = false;
  const refresh = async () => {
    const d = await api('/api/incidents/' + id).catch(e => { if (tok === S.token) $('#view').innerHTML = `<div class="card empty">Incident <code>${esc(id)}</code> not found.<br><a href="#/incidents">Back to incidents</a></div>`; throw e; });
    if (tok !== S.token) return;
    const sig = JSON.stringify([d.audit.length, d.status, d.approvals.map(a => a.status), d.resumable, d.row.active, d.chain_ok, !!d.charts]);
    if (sig === S.sig) return; S.sig = sig; S.detail = d; paintIncident();
  };
  await refresh();
  schedule(refresh, () => (S.detail && S.detail.row.terminal && !S.detail.row.active) ? 5000 : 900, tok);
}

function stepStates(d) {
  const ev = new Set(d.audit.map(e => e.event)), st = d.status, term = ['resolved', 'escalated', 'failed'].includes(st);
  const idx = { detected: 0, investigating: 1, proposed: 2, awaiting_approval: 3, executing: 4, verifying: 5 }[st];
  const names = ['Alert', 'Investigate', 'Diagnose', 'Approve', 'Execute', 'Verify', 'Outcome'];
  return names.map((n, i) => {
    let c = 'todo', ic = i + 1;
    if (!term) { c = i < idx ? 'done' : i === idx ? (st === 'awaiting_approval' ? 'warnc' : 'active') : 'todo'; if (c === 'done') ic = '✓'; }
    else {
      const happened = [true, true, ev.has('findings'), ev.has('approval_approved') || ev.has('approval_rejected'), ev.has('executed'), ev.has('verification')][i];
      if (i === 6) { c = st === 'resolved' ? 'done' : st === 'escalated' ? 'escc' : 'badc'; ic = st === 'resolved' ? '✓' : st === 'escalated' ? '↑' : '!'; }
      else if (i === 3 && ev.has('approval_rejected') && !ev.has('approval_approved')) { c = 'badc'; ic = '✗'; }
      else if (happened) { c = 'done'; ic = '✓'; } else { c = 'skip'; ic = '–'; }
    }
    return `<div class="step ${c}"><span class="n">${ic}</span>${n === 'Outcome' && term ? (st === 'resolved' ? 'Resolved' : st === 'escalated' ? 'Escalated' : 'Failed') : n}</div>`;
  }).join('');
}

function paintIncident() {
  const d = S.detail, old = $('.side .scroll'), keep = old ? { top: old.scrollTop, bottom: old.scrollHeight - old.scrollTop - old.clientHeight < 40 } : null;
  const f = d.state.findings, top = f && f.hypotheses[0], si = d.scenario_info;
  const pend = d.approvals.find(a => a.status === 'pending');
  const crit = [...d.audit].reverse().find(e => e.event === 'success_criteria_declared')?.payload.criteria || [];
  const ver = [...d.audit].reverse().find(e => e.event === 'verification');
  const pol = d.audit.filter(e => e.event === 'policy_decision');
  const t0 = d.audit[0]?.ts || 0;

  let h = `<div class="card hero"><div class="hero-top"><div style="flex:1;min-width:260px">
      <div class="chips" style="margin-bottom:8px"><span class="chip">${d.id}</span>${si ? `<span class="sev-${si.alert.severity}" style="font-size:12.5px;font-weight:650">● ${si.alert.severity.toUpperCase()}</span><span class="chip">${esc(si.alert.name)}</span><span class="chip">${esc(si.alert.service)}</span><span class="diff-${si.difficulty}" style="font-size:12px">${si.difficulty}</span>` : ''}${d.row.active ? pill('info', 'agent running', true) : ''}</div>
      <h1>${esc(si ? si.title : d.scenario || d.id)}</h1>${si ? `<p class="desc">${esc(si.alert.summary)}. ${esc(si.description)}</p>` : ''}</div>
      <div style="text-align:right;display:grid;gap:8px;justify-items:end">${statusPill(d.status, true)}
        <div class="chips"><button class="btn sm" data-act="report">Postmortem report</button>${d.resumable ? '<button class="btn sm pri" data-act="resume">↻ Resume runner</button>' : ''}</div>
        <span class="mut" style="font-size:12px">${d.row.terminal ? 'took ' + dur(d.row.duration_s) : 'opened ' + ago(d.created_at)}</span></div></div>
      <div class="stepper">${stepStates(d)}</div></div>`;

  if (d.resumable) h += `<div class="callout" style="margin-top:16px"><b>No runner is attached to this incident.</b> The state was saved when the process stopped; resume it to continue from “${esc(STATUS[d.status])}” without asking for approval twice.</div>`;

  h += `<div class="body2" style="margin-top:16px"><div class="stack" style="min-width:0">`;

  if (pend) {
    const risk = pend.risk;
    h += `<div class="card approval"><h2>✋ Human approval required</h2>
      <p class="mut" style="margin:6px 0 0">OpsPilot will not change anything until you decide. Nothing has run yet.</p>
      <div class="act">${esc(actStr(pend.action))}</div>
      <dl class="kv"><dt>Plan</dt><dd>${esc(pend.plan)}</dd><dt>Risk</dt><dd>${pill(risk.level === 'low' ? 'ok' : risk.level === 'medium' ? 'warn' : 'bad', risk.level)} ${esc(risk.blast_radius)} · ${risk.reversible ? 'reversible' : '<b style="color:var(--bad)">not reversible</b>'}</dd>
      <dt>Why</dt><dd>${esc(top ? top.summary : '')}</dd>
      <dt>Evidence</dt><dd>${(top ? top.evidence_ids : []).map(e => `<button class="chip" data-act="chip" data-ev="${e}">${e}</button>`).join(' ')}</dd>
      <dt>Recovery means</dt><dd>${crit.map(c => `<span class="chip">${esc(SER[c.metric]?.t || c.metric)} ${c.op} ${esc(serFmt(c.metric)(c.value))}</span>`).join(' ') || '<span class="dim">none declared</span>'} <span class="dim">(declared before acting)</span></dd></dl>
      <div class="btns"><button class="btn ok" data-act="decide" data-aid="${pend.id}" data-ok="1">Approve &amp; execute</button><button class="btn bad" data-act="decide" data-aid="${pend.id}" data-ok="0">Reject</button><input type="text" id="rej" placeholder="Reason (optional)"></div></div>`;
  }

  if (top) {
    const alts = f.hypotheses.slice(1);
    h += `<div class="card"><h3>Root cause</h3><div class="rc"><div class="ring" style="--p:${Math.round(top.confidence * 100)}"><b>${Math.round(top.confidence * 100)}%</b></div>
      <div><h2>${esc(CAT[top.category] || top.category)} <span class="chip" style="vertical-align:middle">${esc(top.service)}</span>${top.change_id ? ` <span class="chip" style="vertical-align:middle">${esc(top.change_id)}</span>` : ''}</h2><div style="margin-top:4px">${esc(top.summary)}</div>
      <div class="chips" style="margin-top:8px"><span class="dim" style="font-size:12px">cited evidence</span>${top.evidence_ids.map(e => `<button class="chip ${S.ui.hl === e ? 'hl' : ''}" data-act="chip" data-ev="${e}">${e}</button>`).join('')}</div></div></div>
      ${alts.length ? `<div style="margin-top:14px"><div class="dim" style="font-size:12px;margin-bottom:4px">ALTERNATIVES CONSIDERED</div>${alts.map(a => `<div class="alt"><span>${esc(CAT[a.category] || a.category)} · ${esc(a.service)}</span><div class="bar"><i style="width:${a.confidence * 100}%;background:var(--dim)"></i></div><span class="mono dim">${Math.round(a.confidence * 100)}%</span></div><div class="mut" style="font-size:12px;margin:-2px 0 6px">${esc(a.summary)}</div>`).join('')}</div>` : ''}
      <div style="margin-top:14px"><div class="dim" style="font-size:12px;margin-bottom:2px">PROPOSED ACTIONS · POLICY GATE</div>${f.proposals.map((p, i) => { const pd = pol[i]?.payload; const ap = d.approvals.find(a => JSON.stringify(a.action) === JSON.stringify(p.action) || (a.action.type === p.action.type && a.action.service === p.action.service));
        return `<div class="act-row"><span class="act" style="margin:0">${esc(actStr(p.action))}</span>${pd ? pill(pd.verdict, pd.verdict.replace('_', ' ')) : ''}${ap && ap.status !== 'pending' ? pill(ap.status === 'approved' ? 'ok' : 'bad', ap.status + (ap.decided_by ? ' by ' + ap.decided_by : '')) : ''}<span class="mut" style="flex-basis:100%;font-size:12.5px">${esc(p.rationale)}${pd && pd.reasons && pd.reasons.length ? ' — ' + esc(pd.reasons.join('; ')) : ''}</span></div>`; }).join('')}</div></div>`;
  } else if (d.row.terminal) {
    h += `<div class="card"><h3>Root cause</h3><div class="empty">The agent produced no valid findings. ${esc([...d.audit].reverse().find(e => e.event === 'status:escalated' || e.event === 'status:failed')?.payload.reason || '')}</div></div>`;
  }

  if (ver) {
    const v = ver.payload, meta = { recovered: ['ok', '✓', 'Recovered', 'Every success criterion declared before the fix now holds.'], not_recovered: ['bad', '✗', 'Not recovered', 'The action did not fix the problem, so it was escalated to a human instead of being reported as resolved.'], regressed: ['bad', '↘', 'Regressed', 'A metric got worse after the action. Escalated.'], unverified: ['warn', '?', 'Unverified', 'No criteria were declared, so recovery cannot be claimed.'] }[v.verdict] || ['', '', v.verdict, ''];
    h += `<div class="card"><h3>Recovery verification</h3><div class="verdict" style="color:var(--${meta[0] === 'ok' ? 'ok' : meta[0] === 'warn' ? 'warn' : 'bad'})"><span class="tick ${meta[0] === 'ok' ? 'y' : 'n'}">${meta[1]}</span>${meta[2]}</div><div class="mut" style="margin-bottom:8px">${meta[3]}</div>` +
      v.checks.map(c => { const fm = serFmt(c.metric), mx = Math.max(c.before ?? 0, c.after ?? 0, c.threshold) * 1.15 || 1, w = x => Math.max(0, Math.min(100, x / mx * 100));
        return `<div class="vrow"><div><b>${esc(SER[c.metric]?.t || c.metric)}</b><div class="dim" style="font-size:11.5px">need ${esc(c.op)} ${esc(fm(c.threshold))}</div></div>
          <div><div class="vbar"><div class="b" style="width:${w(c.before ?? 0)}%"></div><div class="a" style="width:${w(c.after ?? 0)}%;background:${c.ok ? 'var(--ok)' : 'var(--bad)'};opacity:.85;height:60%;top:20%"></div><div class="th" style="left:${w(c.threshold)}%"></div></div>
          <div class="mono" style="font-size:12px;margin-top:5px"><span class="dim">before</span> ${c.before == null ? '–' : esc(fm(c.before))} <span class="dim">→ after</span> <b style="color:var(--${c.ok ? 'ok' : 'bad'})">${c.after == null ? 'no data' : esc(fm(c.after))}</b></div></div>
          <span class="tick ${c.ok ? 'y' : 'n'}">${c.ok ? '✓' : '✗'}</span></div>`; }).join('') + '</div>';
  }

  const sc = d.scorecard;
  if (sc) {
    const row = (ok, label, detail) => `<span class="tick ${ok === null ? 'm' : ok ? 'y' : 'n'}">${ok === null ? '–' : ok ? '✓' : '✗'}</span><span>${label}</span><span class="mut" style="text-align:right;font-size:12.5px">${detail}</span>`;
    const ex = sc.expected;
    h += `<div class="card"><h3>Graded against ground truth <span class="sp"></span>${pill(sc.success ? 'ok' : 'bad', sc.success ? 'success' : 'failed')}</h3><div class="score">
      ${row(sc.rca_correct, 'Root cause', `expected ${esc(CAT[ex.category])} · ${esc(ex.service)}${ex.change_id ? ' · ' + esc(ex.change_id) : ''}`)}
      ${row(sc.proposed_action_correct, 'First proposed action', 'expected ' + esc(ex.correct_actions.map(a => a.type + (a.key ? ' ' + a.key : '')).join(' / ')))}
      ${row(!sc.wrong_action_executed, 'No wrong action executed', sc.wrong_action_executed ? 'a wrong action ran on the system' : 'nothing harmful ran')}
      ${row(!sc.blamed_red_herring, 'Not fooled by the red herring', ex.red_herrings.length ? 'decoy: ' + esc(ex.red_herrings.join(', ')) : 'this scenario has no decoy')}
      </div><p class="mut" style="font-size:12.5px;margin:12px 0 0">${esc(ex.summary)}</p></div>`;
  }

  if (d.charts && Object.keys(d.charts.series).length) {
    const keys = Object.entries(d.charts.series).sort((a, b) => (b[1].anomalous - a[1].anomalous) || ((a[1].onset ?? 1e12) - (b[1].onset ?? 1e12))).map(x => x[0]);
    const anom = keys.filter(k => d.charts.series[k].anomalous), show = S.ui.allCharts ? keys : (anom.length ? anom.slice(0, 6) : keys.slice(0, 4));
    const acted = d.state.acted_at != null, recovered = ver?.payload.verdict === 'recovered';
    h += `<div class="card"><h3>Telemetry <span class="sp"></span><button class="btn sm ghost" data-act="allcharts">${S.ui.allCharts ? 'Only anomalous' : 'All ' + keys.length + ' series'}</button></h3><div class="chartgrid">${show.map(k => { const s = d.charts.series[k], last = [...s.points].reverse().find(p => p[1] != null);
      return `<div class="chartbox ${s.anomalous ? 'anom' : ''}"><div class="t">${esc(SER[k]?.t || k)} <small>${esc(SER[k]?.u || '')}</small><span class="val" style="color:${s.anomalous && !(acted && recovered) ? 'var(--bad)' : acted && s.anomalous ? 'var(--ok)' : 'var(--text)'}">${last ? esc(serFmt(k)(last[1])) : ''}</span></div><div class="chart" data-key="${k}"></div></div>`; }).join('')}</div>
      <div class="legend"><span><i style="border-color:var(--warn)"></i>alert fired</span><span><i style="border-color:var(--acc2)"></i>change / deploy</span><span><i style="border-color:var(--dim);border-top-style:dashed"></i>healthy baseline</span><span><i style="border-color:var(--bad)"></i>anomaly window</span>${acted ? (recovered ? '<span><i style="border-color:var(--ok)"></i>fix applied → recovery</span>' : '<span><i style="border-color:var(--bad)"></i>fix applied → did NOT recover</span>') : ''}</div></div>`;
  }
  h += `</div>`; // left column

  // side column: agent trace + audit log
  const tab = S.ui.tab;
  h += `<div class="side"><div class="card"><div class="tabs"><button class="${tab === 'trace' ? 'on' : ''}" data-act="tab" data-tab="trace">Agent trace</button><button class="${tab === 'audit' ? 'on' : ''}" data-act="tab" data-tab="audit">Audit log</button><span class="sp"></span>${d.chain_ok ? '<span class="pill ok" title="SHA-256 hash chain verified">chain ✓</span>' : '<span class="pill bad">chain BROKEN</span>'}</div><div class="scroll">`;
  h += tab === 'trace' ? traceHTML(d, top, t0) : auditHTML(d, t0);
  h += `</div></div></div></div>`;

  $('#view').innerHTML = h;

  // charts
  if (d.charts) {
    const ref = d.state.acted_at ?? d.charts.now, acted = d.state.acted_at != null, recovered = ver?.payload.verdict === 'recovered';
    $$('.chart[data-key]').forEach(el => {
      const k = el.dataset.key, s = d.charts.series[k];
      OpsCharts.draw(el, { points: s.points, baseline: s.baseline, onset: s.onset, anomalous: s.anomalous, ref, acted, recovered, alert_ts: d.charts.alert_ts, changes: d.charts.changes, fmt: serFmt(k) });
    });
  }
  const sc2 = $('.side .scroll');
  if (sc2) { if (keep && !keep.bottom) sc2.scrollTop = keep.top; else sc2.scrollTop = sc2.scrollHeight; }
}

function resultSummary(tool, r) {
  if (r && r.error) return '⚠ ' + r.error;
  if (r && r.truncated) return 'large result (truncated)';
  try {
    switch (tool) {
      case 'find_anomalies': { const a = r.anomalies || []; return a.length ? `${a.length} anomalous series: ` + a.slice(0, 4).map(x => (SER[x.series]?.t || x.series)).join(', ') : 'no anomalies found'; }
      case 'correlate_changes': { const c = r.candidates || []; return c.length ? `${c.length} candidate${c.length > 1 ? 's' : ''}; top: ${c[0].id} (${c[0].summary}) preceded onset by ${c[0].lead_s}s` : 'no change lines up with the first anomaly'; }
      case 'log_patterns': return `${(r.patterns || []).length} log templates` + ((r.patterns || []).find(p => (p.field_shift || []).length) ? '; field shift: ' + r.patterns.find(p => (p.field_shift || []).length).field_shift.slice(0, 2).join(', ') : '');
      case 'search_logs': return `${(r.lines || []).length} log lines`;
      case 'list_changes': return `${(r.changes || []).length} changes in history`;
      case 'get_change_diff': return `${r.id || ''} ${r.service ? '· ' + r.service : ''} ${r.summary ? '· ' + r.summary : ''}`;
      case 'get_config': return `${r.service} @ v${r.version}; known versions: ${(r.known_versions || []).join(', ')}`;
      case 'query_series': return `${r.series}: ${(r.points || []).length} points`;
      default: return Object.keys(r || {}).slice(0, 4).join(', ');
    }
  } catch { return ''; }
}
function resultBody(tool, r) {
  if (tool === 'get_change_diff' && r && typeof r.diff === 'string') {
    return `<pre class="diffb">${r.diff.split('\n').map(l => `<span class="${l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : l.startsWith('@@') ? 'hunk' : ''}">${esc(l)}</span>`).join('\n')}</pre>`;
  }
  const t = r && r.truncated ? r.text : JSON.stringify(r, null, 2);
  return `<pre>${esc(t.length > 4000 ? t.slice(0, 4000) + '\n… (clipped)' : t)}</pre>`;
}
function traceHTML(d, top, t0) {
  const cited = new Set(top ? top.evidence_ids : []);
  let out = '';
  for (const e of d.audit) {
    const p = e.payload, rel = `+${(e.ts - t0).toFixed(1)}s`;
    if (e.event === 'alert_received') {
      out += `<div class="tr"><span class="ico" style="background:var(--warn);border-color:var(--warn);color:#201500">!</span><div class="hd"><b>Alert received</b><span class="dim mono">${rel}</span></div><div class="sum">${esc(p.name)} — ${esc(p.summary)}</div></div>`;
    } else if (e.event === 'agent_note') {
      out += `<div class="note">${esc(p.text)}</div>`;
    } else if (e.event === 'tool_call') {
      const open = S.ui.open[p.evidence_id], c = cited.has(p.evidence_id);
      const args = Object.entries(p.args || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');
      out += `<div class="tr ${c ? 'cited' : ''}" id="tr-${p.evidence_id}"><span class="ico">${TOOLICON[p.tool] || '•'}</span>
        <div class="hd" data-act="toggle" data-key="${p.evidence_id}"><span class="tool">${esc(p.tool)}</span><span class="args">${esc(args)}</span><span class="chip">${p.evidence_id}</span>${c ? pill('info', 'cited') : ''}<span class="dim mono" style="margin-left:auto">${rel}</span></div>
        <div class="sum">${esc(resultSummary(p.tool, p.result))}</div>${open ? resultBody(p.tool, p.result) : ''}</div>`;
    } else if (e.event === 'findings') {
      const t = p.hypotheses[0];
      out += `<div class="tr cited"><span class="ico">◎</span><div class="hd"><b>Findings submitted</b><span class="dim mono" style="margin-left:auto">${rel}</span></div><div class="sum">${esc(CAT[t.category] || t.category)} in ${esc(t.service)} (${Math.round(t.confidence * 100)}%) · ${p.hypotheses.length} hypothes${p.hypotheses.length > 1 ? 'es' : 'is'} · ${p.proposals.length} proposal${p.proposals.length > 1 ? 's' : ''} · ${(p.success_criteria || []).length} recovery criteria</div></div>`;
    } else if (e.event === 'policy_decision') {
      out += `<div class="tr"><span class="ico">⚖</span><div class="hd"><b>Policy gate</b>${pill(p.verdict, p.verdict.replace('_', ' '))}<span class="dim mono" style="margin-left:auto">${rel}</span></div><div class="sum">${esc(actStr(p.action))}${p.reasons && p.reasons.length ? '<br>' + esc(p.reasons.join('; ')) : ''}</div></div>`;
    } else if (e.event === 'approval_approved' || e.event === 'approval_rejected') {
      const ok = e.event === 'approval_approved';
      out += `<div class="tr"><span class="ico" style="background:var(--${ok ? 'ok' : 'bad'});border-color:var(--${ok ? 'ok' : 'bad'});color:#fff">${ok ? '✓' : '✗'}</span><div class="hd"><b>${ok ? 'Approved' : 'Rejected'} by ${esc(e.actor)}</b><span class="dim mono" style="margin-left:auto">${rel}</span></div>${p.reason ? `<div class="sum">${esc(p.reason)}</div>` : ''}</div>`;
    } else if (e.event === 'executed') {
      out += `<div class="tr"><span class="ico" style="background:var(--acc2);border-color:var(--acc2);color:#fff">⚡</span><div class="hd"><b>Executed</b><span class="dim mono" style="margin-left:auto">${rel}</span></div><div class="sum">${esc(actStr(p.action))}</div></div>`;
    } else if (e.event === 'verification') {
      out += `<div class="tr"><span class="ico" style="background:var(--${p.verdict === 'recovered' ? 'ok' : 'bad'});border-color:transparent;color:#fff">◉</span><div class="hd"><b>Verification: ${esc(p.verdict.replace('_', ' '))}</b><span class="dim mono" style="margin-left:auto">${rel}</span></div></div>`;
    }
  }
  if (d.row.active && ['detected', 'investigating'].includes(d.status)) out += `<div class="thinking"><i></i><i></i><i></i> investigating…</div>`;
  if (d.row.active && d.status === 'awaiting_approval') out += `<div class="thinking"><i></i><i></i><i></i> waiting for a human</div>`;
  return out || '<div class="empty">No agent activity yet.</div>';
}
function auditHTML(d, t0) {
  const short = (e) => {
    const p = e.payload;
    if (e.event === 'tool_call') return `${p.tool}(${Object.entries(p.args || {}).map(([k, v]) => k + '=' + v).join(', ')})`;
    if (e.event.startsWith('status:')) return 'from ' + p.from + (p.reason ? ' — ' + p.reason : '');
    if (e.event === 'findings') return p.summary;
    if (e.event === 'policy_decision') return p.verdict + ': ' + actStr(p.action);
    if (e.event === 'approval_requested') return p.plan;
    if (e.event === 'executed') return actStr(p.action);
    if (e.event === 'verification') return p.verdict;
    if (e.event === 'success_criteria_declared') return p.criteria.map(c => `${c.metric} ${c.op} ${c.value}`).join(', ');
    if (e.event === 'agent_note') return p.text;
    const s = JSON.stringify(p); return s.length > 140 ? s.slice(0, 140) + '…' : s;
  };
  return `<div class="mut" style="font-size:12px;margin-bottom:8px">Append-only. Each entry’s hash covers the previous hash, so editing history breaks every hash after it.</div>` +
    d.audit.map(e => `<div class="au"><span class="t">+${(e.ts - t0).toFixed(1)}s</span><span class="e">${esc(e.event)} <span class="dim" style="font-weight:400">· ${esc(e.actor)}</span></span><span class="p">${esc(String(short(e)).slice(0, 220))}</span><span class="h">#${e.seq} ${e.hash.slice(0, 10)} ← ${e.prev_hash.slice(0, 8)}</span></div>`).join('');
}

/* ------------------------------------------------------------------ report + launch modals */
function mdToHtml(src) {
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  const lines = src.split('\n'); let o = '', list = false, tbl = false;
  const close = () => { if (list) { o += '</ul>'; list = false; } if (tbl) { o += '</tbody></table>'; tbl = false; } };
  for (const l of lines) {
    if (/^\|/.test(l)) {
      if (/^\|[-| ]+\|$/.test(l)) continue;
      const cells = l.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      if (!tbl) { close(); o += '<table><thead><tr>' + cells.map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>'; tbl = true; }
      else o += '<tr>' + cells.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
    } else if (/^#{1,3} /.test(l)) { close(); const n = l.match(/^#+/)[0].length; o += `<h${n}>${inline(l.replace(/^#+ /, ''))}</h${n}>`; }
    else if (/^- /.test(l)) { if (tbl) close(); if (!list) { o += '<ul>'; list = true; } o += `<li>${inline(l.slice(2))}</li>`; }
    else if (l.trim() === '') close(); else { close(); o += `<p>${inline(l)}</p>`; }
  }
  close(); return o;
}
const dlg = () => $('#dlg');
function openDialog(html) { const d = dlg(); d.innerHTML = html; if (!d.open) d.showModal(); }
async function showReport() {
  const id = S.detail.id, md = await api(`/api/incidents/${id}/report`);
  openDialog(`<div class="mh"><h2>Postmortem · ${id}</h2><span class="sp"></span><button class="btn sm ghost" data-act="close">✕</button></div>
    <div class="mb md">${mdToHtml(md)}</div><div class="mf"><span class="mut" style="font-size:12px;margin-right:auto">Generated from the audit log only; no model rewrites it.</span><button class="btn" data-act="copymd">Copy markdown</button><button class="btn pri" data-act="dlmd">Download .md</button></div>`);
  dlg()._md = md;
}
function openLaunch(sc) {
  const m = S.meta;
  S.launch = { scenario: sc || S.launch?.scenario || m.scenarios[0].id, agent: S.launch?.agent || 'heuristic', mode: 'replay', approval: 'web', pace: true, busy: false, log: [] };
  paintLaunch();
}
function paintLaunch() {
  const L = S.launch, m = S.meta, scn = m.scenarios.find(s => s.id === L.scenario);
  const groups = [['agent', 'Agents'], ['baseline', 'Baselines'], ['ablation', 'Ablations'], ['selftest', 'Harness self-tests']];
  openDialog(`<div class="mh"><h2>New incident</h2><span class="sp"></span><button class="btn sm ghost" data-act="close">✕</button></div><div class="mb">
    <div class="field"><label>Scenario</label><select id="lsc" style="width:100%">${m.scenarios.map(s => `<option value="${s.id}" ${s.id === L.scenario ? 'selected' : ''} ${s.snapshot ? '' : 'disabled'}>${esc(s.title)} (${s.difficulty})</option>`).join('')}</select>
      <div class="mut" style="font-size:12.5px;margin-top:6px">${esc(scn.alert.summary)} — ${esc(scn.description)}</div></div>
    <div class="field"><label>Investigator</label><div class="opts">${m.agents.filter(a => a.kind !== 'selftest').map(a => `<label class="opt ${a.available ? '' : 'off'} ${L.agent === a.id ? 'on' : ''}"><input type="radio" name="ag" value="${a.id}" ${L.agent === a.id ? 'checked' : ''} ${a.available ? '' : 'disabled'}><span><b>${esc(a.label)}</b><small>${esc(a.available ? a.desc : a.desc + ' — unavailable: ' + a.why)}</small></span></label>`).join('')}</div></div>
    <div class="field" style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><div><label style="display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:650;margin-bottom:7px">Environment</label><div class="seg"><button class="${L.mode === 'replay' ? 'on' : ''}" data-act="lset" data-k="mode" data-v="replay">Replay</button><button class="${L.mode === 'live' ? 'on' : ''}" data-act="lset" data-k="mode" data-v="live">Live testbed</button></div><div class="mut" style="font-size:12px;margin-top:6px">${L.mode === 'live' ? 'Starts the real API + worker, injects the fault, executes for real. ~70 s.' : 'Recorded telemetry, deterministic and instant.'}</div></div>
      <div><label style="display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:650;margin-bottom:7px">Approval</label><div class="seg"><button class="${L.approval === 'web' ? 'on' : ''}" data-act="lset" data-k="approval" data-v="web">I approve</button><button class="${L.approval === 'auto' ? 'on' : ''}" data-act="lset" data-k="approval" data-v="auto">Auto (demo)</button></div><div class="mut" style="font-size:12px;margin-top:6px">${L.approval === 'web' ? 'The run pauses until you click Approve or Reject.' : 'Rubber-stamps anything policy allows, like the benchmark does.'}</div></div></div>
    <label class="opt ${L.pace ? 'on' : ''}" style="margin-bottom:10px"><input type="checkbox" id="lpace" ${L.pace ? 'checked' : ''}><span><b>Demo speed</b><small>Pause between agent steps so you can watch the investigation unfold.</small></span></label>
    ${L.busy ? `<div class="callout"><div class="thinking" style="padding:0"><i></i><i></i><i></i> ${esc(L.log.slice(-1)[0] || 'starting…')}</div></div>` : ''}</div>
    <div class="mf"><button class="btn ghost" data-act="close">Cancel</button><button class="btn pri" data-act="go" ${L.busy ? 'disabled' : ''}>${L.busy ? 'Starting…' : 'Start investigation'}</button></div>`);
  $('#lsc').onchange = e => { L.scenario = e.target.value; paintLaunch(); };
  $$('input[name=ag]').forEach(i => i.onchange = () => { L.agent = i.value; paintLaunch(); });
  $('#lpace').onchange = e => { L.pace = e.target.checked; };
}
async function startRun() {
  const L = S.launch; L.busy = true; L.log = ['queued…']; paintLaunch();
  try {
    const { run_id } = await post('/api/runs', { scenario: L.scenario, agent: L.agent, mode: L.mode, approval: L.approval, pace: L.pace });
    for (let i = 0; i < 600; i++) {
      const r = await api('/api/runs/' + run_id);
      if (r.error) throw new Error(r.error);
      if (r.log.length && r.log.slice(-1)[0] !== L.log.slice(-1)[0]) { L.log = r.log; paintLaunch(); }
      if (r.incident_id) { dlg().close(); location.hash = '#/incident/' + r.incident_id; return; }
      if (r.done) throw new Error('run ended before an incident was created');
      await new Promise(res => setTimeout(res, 350));
    }
  } catch (e) { L.busy = false; paintLaunch(); toast(e.message, 'bad'); }
}

/* ------------------------------------------------------------------ actions */
const ACT = {
  launch: el => openLaunch(el.dataset.sc),
  close: () => dlg().close(),
  go: startRun,
  lset: el => { S.launch[el.dataset.k] = el.dataset.v; paintLaunch(); },
  filter: el => { S.ui.filter = el.dataset.f; S.sig = ''; mountRefresh(); },
  tab: el => { S.ui.tab = el.dataset.tab; paintIncident(); },
  toggle: el => { const k = el.dataset.key; S.ui.open[k] = !S.ui.open[k]; paintIncident(); },
  allcharts: () => { S.ui.allCharts = !S.ui.allCharts; paintIncident(); },
  report: showReport,
  copymd: async () => { try { await navigator.clipboard.writeText(dlg()._md); toast('Copied', 'ok'); } catch { toast('Copy failed', 'bad'); } },
  dlmd: () => { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([dlg()._md], { type: 'text/markdown' })); a.download = `${S.detail.id}-postmortem.md`; a.click(); },
  chip: el => {
    S.ui.tab = 'trace'; S.ui.hl = el.dataset.ev; S.ui.open[el.dataset.ev] = true; paintIncident();
    const t = document.getElementById('tr-' + el.dataset.ev); if (t) { t.scrollIntoView({ block: 'center', behavior: 'smooth' }); t.classList.add('flash'); }
  },
  decide: async el => {
    const ok = el.dataset.ok === '1', reason = $('#rej')?.value || '';
    el.disabled = true;
    try { await post('/api/approvals/' + el.dataset.aid, { approve: ok, by: 'web-user', reason }); toast(ok ? 'Approved. Executing…' : 'Rejected. Escalating to a human.', ok ? 'ok' : ''); S.sig = ''; }
    catch (e) { toast(e.message, 'bad'); el.disabled = false; }
  },
  resume: async () => { try { await post(`/api/incidents/${S.detail.id}/resume`); toast('Runner attached', 'ok'); S.sig = ''; } catch (e) { toast(e.message, 'bad'); } },
  evalsel: el => { el.checked ? S.evalSel.add(el.dataset.id) : S.evalSel.delete(el.dataset.id); el.closest('.opt').classList.toggle('on', el.checked); },
  evalrun: async () => {
    try { await post('/api/eval', { agents: [...S.evalSel], runs: 3 }); toast('Benchmark started'); S.sig = ''; mountRefresh(); }
    catch (e) { toast(e.message, 'bad'); }
  },
};
const mountRefresh = () => mount();

document.addEventListener('click', e => {
  const a = e.target.closest('[data-act]');
  if (a && a.tagName !== 'INPUT') { e.preventDefault(); ACT[a.dataset.act]?.(a, e); return; }
  const h = e.target.closest('[data-href]'); if (h) location.hash = h.dataset.href;
});
document.addEventListener('change', e => { const a = e.target.closest('input[data-act]'); if (a) ACT[a.dataset.act]?.(a, e); });
$('#newBtn').onclick = () => openLaunch();
$('#themeBtn').onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  const next = cur === 'light' ? 'dark' : 'light'; document.documentElement.dataset.theme = next; store.set('theme', next); if (S.detail && S.page === 'incident') paintIncident();
};
{ const t = store.get('theme'); if (t) document.documentElement.dataset.theme = t; }
dlg().addEventListener('click', e => { if (e.target === dlg()) dlg().close(); });
window.addEventListener('hashchange', mount);
globalPoll(); chainPoll(); setInterval(globalPoll, 2500); setInterval(chainPoll, 8000);
mount();
