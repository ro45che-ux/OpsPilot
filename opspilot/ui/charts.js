/* Dependency-free SVG time-series chart with incident annotations (baseline, anomaly window,
   alert, change markers, action line, post-fix segment) and a hover crosshair. */
(function () {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }

  function draw(el, s) {
    el._spec = s;
    const W = el.clientWidth || 420, H = el.clientHeight || 150, m = { l: 46, r: 10, t: 12, b: 20 };
    const pts = s.points.filter(p => p[1] != null);
    if (pts.length < 2) { el.innerHTML = '<div class="empty" style="padding:50px 0">no data</div>'; return; }
    const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
    const ymax = niceMax(Math.max(...pts.map(p => p[1]), s.baseline ?? 0) * 1.1);
    const X = t => m.l + (t - x0) / ((x1 - x0) || 1) * (W - m.l - m.r);
    const Y = v => H - m.b - v / ymax * (H - m.t - m.b);
    const ref = s.ref ?? x1;
    const path = arr => arr.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join('');
    const pre = pts.filter(p => p[0] <= ref + 1e-6);
    const post = s.acted ? pts.filter(p => p[0] >= ref - 1e-6) : [];
    const lineCol = s.anomalous ? 'var(--bad)' : 'var(--acc)';
    const postCol = s.recovered ? 'var(--ok)' : 'var(--bad)';  // green only if the verifier said recovered
    let g = '';

    for (let i = 0; i <= 2; i++) {
      const v = ymax * i / 2, y = Y(v);
      g += `<line class="gl" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/><text class="ax" x="${m.l - 6}" y="${y + 3}" text-anchor="end">${esc(s.fmt(v))}</text>`;
    }
    const span = x1 - x0, step = span > 90 ? 30 : span > 45 ? 20 : 10;
    for (let r = Math.ceil((x0 - ref) / step) * step; ref + r <= x1 + 1e-6; r += step) {
      const x = X(ref + r);
      g += `<text class="ax" x="${x}" y="${H - 5}" text-anchor="middle">${r === 0 ? (s.acted ? 'fix' : 'now') : (r > 0 ? '+' : '') + r + 's'}</text>`;
    }
    if (s.onset != null && s.onset < ref) {
      const xa = Math.max(m.l, X(s.onset)), xb = X(Math.min(ref, x1));
      g += `<rect x="${xa}" y="${m.t}" width="${Math.max(0, xb - xa)}" height="${H - m.t - m.b}" fill="var(--bad)" opacity=".07"/>`;
    }
    if (s.acted && ref < x1) {
      g += `<rect x="${X(ref)}" y="${m.t}" width="${X(x1) - X(ref)}" height="${H - m.t - m.b}" fill="${postCol}" opacity=".07"/>`;
    }
    if (s.baseline != null) {
      g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(s.baseline)}" y2="${Y(s.baseline)}" stroke="var(--dim)" stroke-dasharray="4 4"/>`;
    }
    if (pre.length > 1) {
      g += `<path d="${path(pre)} L${X(pre[pre.length - 1][0])} ${Y(0)} L${X(pre[0][0])} ${Y(0)}Z" fill="${lineCol}" opacity=".10"/>`;
      g += `<path d="${path(pre)}" fill="none" stroke="${lineCol}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    if (post.length > 1) {
      g += `<path d="${path(post)} L${X(post[post.length - 1][0])} ${Y(0)} L${X(post[0][0])} ${Y(0)}Z" fill="${postCol}" opacity=".12"/>`;
      g += `<path d="${path(post)}" fill="none" stroke="${postCol}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    const marks = [];
    if (s.alert_ts && s.alert_ts >= x0 && s.alert_ts <= x1) marks.push({ t: s.alert_ts, label: 'Alert fired', col: 'var(--warn)', dash: '5 3' });
    for (const c of s.changes || []) {
      if (c.ts >= x0 && c.ts <= x1) marks.push({ t: c.ts, label: `${c.rollback ? 'Rollback' : c.type}: ${c.summary}`, col: c.rollback ? 'var(--ok)' : 'var(--acc2)', dash: '2 3', diamond: true });
    }
    if (s.acted) marks.push({ t: ref, label: s.recovered ? 'Approved fix applied' : 'Approved fix applied (did not recover)', col: s.recovered ? 'var(--ok)' : 'var(--bad)', dash: '' });
    for (const k of marks) {
      const x = X(k.t);
      g += `<line x1="${x}" x2="${x}" y1="${m.t}" y2="${H - m.b}" stroke="${k.col}" stroke-width="1.5" ${k.dash ? `stroke-dasharray="${k.dash}"` : ''}/>`;
      if (k.diamond) g += `<path d="M${x} ${m.t - 4}l4 4-4 4-4-4z" fill="${k.col}"/>`;
    }
    g += `<line class="cx" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--mut)" stroke-width="1" style="display:none"/><circle class="cd" r="4" fill="${lineCol}" stroke="var(--panel)" stroke-width="2" style="display:none"/>`;
    g += `<rect class="hit" x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent"/>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${g}</svg><div class="tip"></div>`;

    const svg = el.querySelector('svg'), tip = el.querySelector('.tip'), cx = svg.querySelector('.cx'), cd = svg.querySelector('.cd');
    const stroke = pts.map(p => X(p[0]));
    el.querySelector('.hit').addEventListener('mousemove', ev => {
      const rect = svg.getBoundingClientRect(), px = ev.clientX - rect.left;
      let bi = 0, bd = 1e9;
      stroke.forEach((x, i) => { const d = Math.abs(x - px); if (d < bd) { bd = d; bi = i; } });
      const [t, v] = pts[bi], x = X(t), y = Y(v);
      cx.setAttribute('x1', x); cx.setAttribute('x2', x); cx.style.display = '';
      cd.setAttribute('cx', x); cd.setAttribute('cy', y); cd.style.display = '';
      const near = marks.find(k => Math.abs(X(k.t) - px) < 6);
      const rel = Math.round(t - ref);
      tip.innerHTML = `<b>${esc(s.fmt(v))}</b> <span style="color:var(--mut)">${rel === 0 ? (s.acted ? 'at fix' : 'now') : (rel > 0 ? '+' : '') + rel + 's'}</span>` +
        (near ? `<div style="color:var(--mut);max-width:240px;white-space:normal">${esc(near.label)}</div>` : '');
      tip.style.display = 'block';
      const tw = tip.offsetWidth;
      tip.style.left = Math.min(Math.max(0, x - tw / 2), W - tw) + 'px';
      tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + 'px';
    });
    el.querySelector('.hit').addEventListener('mouseleave', () => { tip.style.display = 'none'; cx.style.display = 'none'; cd.style.display = 'none'; });
  }

  const ro = new ResizeObserver(entries => {
    for (const e of entries) { const el = e.target; if (el._spec && Math.abs((el._w || 0) - el.clientWidth) > 2) { el._w = el.clientWidth; draw(el, el._spec); } }
  });
  window.OpsCharts = { draw(el, spec) { el._w = el.clientWidth; draw(el, spec); ro.observe(el); } };
})();
