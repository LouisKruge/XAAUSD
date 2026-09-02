/* XAUUSD Trading Terminal — vanilla JS, hand-rolled SVG charts, zero dependencies.
 *
 * DEVIATION from docs/architecture/01-tech-stack.md, which specified Vite + React +
 * lightweight-charts. This is a no-build single-page app instead, because:
 *   - a Windows VPS whose appeal is being easy to rebuild should not need a node
 *     toolchain to render a dashboard;
 *   - a trading box should not depend on a CDN it cannot reach when the network is
 *     degraded, and vendoring a chart library to avoid that costs more than the ~200
 *     lines of SVG here;
 *   - the charts needed are an equity curve, a drawdown band and horizontal bars.
 * The API contract is unchanged, so a React front end can replace this file without
 * touching the backend.
 */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const fmt = {
  money: (v, dp = 2) =>
    v == null || Number.isNaN(v) ? '—' :
    (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US',
      { minimumFractionDigits: dp, maximumFractionDigits: dp }),
  pct: (v, dp = 2) => v == null || Number.isNaN(v) ? '—' : (v * 100).toFixed(dp) + '%',
  signedPct: (v, dp = 2) =>
    v == null || Number.isNaN(v) ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(dp) + '%',
  num: (v, dp = 2) => v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(dp),
  r: (v) => v == null || Number.isNaN(v) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'R',
  int: (v) => v == null ? '—' : Number(v).toLocaleString('en-US'),
  time: (v) => {
    if (!v) return '—';
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? String(v)
      : d.toISOString().slice(0, 16).replace('T', ' ') + 'Z';
  },
  timeShort: (v) => {
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(5, 16).replace('T', ' ');
  },
};

const sign = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral');

// ---------------------------------------------------------------------------
// SVG charts. Every chart carries a hover layer; identity is never colour-alone.
// ---------------------------------------------------------------------------

const tooltip = (() => {
  let el = null;
  return {
    show(html, x, y) {
      if (!el) { el = document.createElement('div'); el.className = 'tooltip'; document.body.appendChild(el); }
      el.innerHTML = html;
      el.style.display = 'block';
      const r = el.getBoundingClientRect();
      el.style.left = Math.min(x + 14, window.innerWidth - r.width - 8) + 'px';
      el.style.top = Math.max(8, y - r.height - 10) + 'px';
    },
    hide() { if (el) el.style.display = 'none'; },
  };
})();

function svgEl(name, attrs = {}) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

/** Equity curve with a drawdown band beneath it. One series: no legend needed. */
function equityChart(container, points, height = 240) {
  container.innerHTML = '';
  if (!points || points.length < 2) {
    container.innerHTML = '<div class="empty">No equity history yet.</div>';
    return;
  }
  const pad = { l: 62, r: 14, t: 12, b: 24 };
  const w = container.clientWidth || 800;
  const h = height;
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;

  const vals = points.map((p) => p.equity);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || Math.max(1, hi * 0.01);
  lo -= span * 0.08; hi += span * 0.08;

  const X = (i) => pad.l + (i / (points.length - 1)) * iw;
  const Y = (v) => pad.t + ih - ((v - lo) / (hi - lo)) * ih;

  const svg = svgEl('svg', { class: 'chart', viewBox: `0 0 ${w} ${h}`, height: h });
  const defs = svgEl('defs');
  defs.innerHTML =
    '<linearGradient id="eqfade" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="#f2f2f4" stop-opacity="0.14"/>' +
    '<stop offset="100%" stop-color="#f2f2f4" stop-opacity="0"/></linearGradient>';
  svg.appendChild(defs);

  // recessive grid + axis labels
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    const y = Y(v);
    svg.appendChild(svgEl('line', { class: 'grid-line', x1: pad.l, y1: y, x2: w - pad.r, y2: y }));
    const t = svgEl('text', { class: 'axis-text', x: pad.l - 8, y: y + 3, 'text-anchor': 'end' });
    t.textContent = '$' + Math.round(v).toLocaleString('en-US');
    svg.appendChild(t);
  }
  for (const i of [0, Math.floor(points.length / 2), points.length - 1]) {
    const t = svgEl('text', {
      class: 'axis-text', x: X(i), y: h - 6,
      'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle',
    });
    t.textContent = fmt.timeShort(points[i].ts);
    svg.appendChild(t);
  }

  // drawdown band: running peak down to equity
  let peak = -Infinity;
  const peaks = points.map((p) => (peak = Math.max(peak, p.equity)));
  const ddPath = points.map((p, i) => `${i ? 'L' : 'M'}${X(i)},${Y(peaks[i])}`).join(' ') +
    ' ' + points.map((p, i) => `L${X(points.length - 1 - i)},${Y(points[points.length - 1 - i].equity)}`).join(' ') + ' Z';
  svg.appendChild(svgEl('path', { class: 'dd-area', d: ddPath }));

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${X(i)},${Y(p.equity)}`).join(' ');
  svg.appendChild(svgEl('path', { class: 'series-area', d: `${line} L${X(points.length - 1)},${pad.t + ih} L${X(0)},${pad.t + ih} Z` }));
  svg.appendChild(svgEl('path', { class: 'series', d: line }));

  // hover crosshair
  const hover = svgEl('line', { class: 'hover-line', y1: pad.t, y2: pad.t + ih, x1: 0, x2: 0, opacity: 0 });
  const marker = svgEl('circle', { r: 4, fill: '#f2f2f4', opacity: 0 });
  svg.appendChild(hover); svg.appendChild(marker);
  const hit = svgEl('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent' });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const rect = svg.getBoundingClientRect();
    const rel = ((ev.clientX - rect.left) / rect.width) * w;
    let i = Math.round(((rel - pad.l) / iw) * (points.length - 1));
    i = Math.max(0, Math.min(points.length - 1, i));
    const p = points[i];
    hover.setAttribute('x1', X(i)); hover.setAttribute('x2', X(i)); hover.setAttribute('opacity', 1);
    marker.setAttribute('cx', X(i)); marker.setAttribute('cy', Y(p.equity)); marker.setAttribute('opacity', 1);
    const dd = peaks[i] > 0 ? (peaks[i] - p.equity) / peaks[i] : 0;
    tooltip.show(
      `${fmt.time(p.ts)}<br>equity ${fmt.money(p.equity)}<br>drawdown ${fmt.pct(dd)}`,
      ev.clientX, ev.clientY);
  });
  hit.addEventListener('mouseleave', () => {
    hover.setAttribute('opacity', 0); marker.setAttribute('opacity', 0); tooltip.hide();
  });
  container.appendChild(svg);
}

/** Horizontal bars for R distribution. Sign is encoded by position AND label. */
function rDistribution(container, trades, height = 190) {
  container.innerHTML = '';
  if (!trades || !trades.length) {
    container.innerHTML = '<div class="empty">No closed trades yet.</div>';
    return;
  }
  const buckets = [
    { label: '< -1R', lo: -Infinity, hi: -1 },
    { label: '-1 to -0.5R', lo: -1, hi: -0.5 },
    { label: '-0.5 to 0R', lo: -0.5, hi: 0 },
    { label: '0 to 1R', lo: 0, hi: 1 },
    { label: '1 to 2R', lo: 1, hi: 2 },
    { label: '2 to 3R', lo: 2, hi: 3 },
    { label: '> 3R', lo: 3, hi: Infinity },
  ];
  const counts = buckets.map((b) => trades.filter((t) => t.r > b.lo && t.r <= b.hi).length);
  const max = Math.max(1, ...counts);
  const frag = document.createDocumentFragment();
  buckets.forEach((b, i) => {
    const row = document.createElement('div');
    row.className = 'bar-row';
    const negative = b.hi <= 0;
    row.innerHTML =
      `<div class="name">${b.label}</div>` +
      `<div class="bar-track"><div class="bar-fill ${negative ? 'penalty' : 'strong'}" ` +
      `style="width:${(counts[i] / max) * 100}%"></div></div>` +
      `<div class="val">${counts[i]}</div>`;
    frag.appendChild(row);
  });
  container.appendChild(frag);
}

/** Score breakdown: earned versus maximum, per category. */
function scoreBars(container, breakdown) {
  container.innerHTML = '';
  if (!breakdown) { container.innerHTML = '<div class="empty">No score breakdown.</div>'; return; }
  const cats = breakdown.categories || {};
  const maxes = breakdown.maximums || {};
  const frag = document.createDocumentFragment();
  for (const [k, v] of Object.entries(cats)) {
    const max = maxes[k] || 1;
    const strong = v >= 0.7 * max;
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML =
      `<div class="name">${k.replace(/_/g, ' ')}</div>` +
      `<div class="bar-track"><div class="bar-fill ${strong ? 'strong' : ''}" ` +
      `style="width:${(v / max) * 100}%"></div></div>` +
      `<div class="val">${v.toFixed(1)}/${max}</div>`;
    frag.appendChild(row);
  }
  for (const [k, v] of Object.entries(breakdown.penalties || {})) {
    if (!v) continue;
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML =
      `<div class="name" style="color:var(--short)">penalty · ${k.replace(/_/g, ' ')}</div>` +
      `<div class="bar-track"><div class="bar-fill penalty" style="width:${Math.min(100, v * 5)}%"></div></div>` +
      `<div class="val">-${v.toFixed(1)}</div>`;
    frag.appendChild(row);
  }
  container.appendChild(frag);
}

/** Rejection ledger bars — why the bot did not trade. */
function ledgerBars(container, ledger) {
  container.innerHTML = '';
  if (!ledger || !ledger.length) {
    container.innerHTML = '<div class="empty">No rejections recorded in this window.</div>';
    return;
  }
  const max = Math.max(...ledger.map((l) => l.count));
  const frag = document.createDocumentFragment();
  ledger.slice(0, 14).forEach((l) => {
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML =
      `<div class="name">${l.gate.replace(/_/g, ' ')}</div>` +
      `<div class="bar-track"><div class="bar-fill" style="width:${(l.count / max) * 100}%"></div></div>` +
      `<div class="val">${fmt.int(l.count)}</div>`;
    frag.appendChild(row);
  });
  container.appendChild(frag);
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

const state = { data: {}, decisions: [], performance: null, rejections: null, config: null };

// Bearer token. Empty on a loopback deployment, where the server requires none.
// Held per-browser; it is never sent anywhere but this origin.
function authToken() {
  try { return localStorage.getItem('xauusd_token') || ''; } catch (e) { return ''; }
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const t = authToken();
  if (t) h['Authorization'] = `Bearer ${t}`;
  return h;
}

function promptForToken() {
  // Name the file and the key. A bare "requires an access token" leaves the operator
  // hunting for a secret they were never told had been created.
  const t = prompt(
    'This dashboard needs its access token.\n\n'
    + 'Open the .env file in your installation folder, find the line\n'
    + 'XAUUSD_DASHBOARD__AUTH_TOKEN=  and paste everything after the "=".\n\n'
    + 'To remove this prompt entirely on a local install, delete that line and restart.');
  if (!t) return false;
  try { localStorage.setItem('xauusd_token', t.trim()); } catch (e) { return false; }
  return true;
}

async function api(path) {
  const r = await fetch(path, { headers: authHeaders() });
  if (r.status === 401) {
    // Ask once, then retry. A wrong token must not spin.
    if (promptForToken()) {
      const retry = await fetch(path, { headers: authHeaders() });
      if (retry.ok) return retry.json();
    }
    throw new Error(`${path} -> 401 (token rejected)`);
  }
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function renderCommandCentre() {
  const d = state.data || {};
  const acct = d.account || {};
  const risk = d.risk || {};
  const ks = d.kill_switch || {};

  $('#tiles').innerHTML = [
    tile('Equity', fmt.money(acct.equity), sign(0), `balance ${fmt.money(acct.balance)}`),
    tile('Daily P&L', fmt.money(risk.daily_pnl), sign(risk.daily_pnl),
         `dd ${fmt.pct(risk.daily_drawdown || 0)} of ${fmt.pct(risk.daily_limit || 0.02)}`),
    tile('Weekly P&L', fmt.money(risk.weekly_pnl), sign(risk.weekly_pnl),
         `dd ${fmt.pct(risk.weekly_drawdown || 0)} of ${fmt.pct(risk.weekly_limit || 0.05)}`),
    tile('Monthly P&L', fmt.money(risk.monthly_pnl), sign(risk.monthly_pnl),
         `dd ${fmt.pct(risk.monthly_drawdown || 0)} of ${fmt.pct(risk.monthly_limit || 0.10)}`),
    tile('Risk deployed', fmt.pct(risk.open_risk_pct || 0), 'neutral',
         `${d.open_positions || 0} position(s)`),
    tile('XAUUSD', fmt.num(d.price, 2), 'neutral', `spread ${fmt.num(d.spread_points, 0)} pts`),
    tile('Regime', d.regime || '—', 'neutral', `volatility ${d.vol_regime || '—'}`),
    tile('Bias / news', `${d.htf_bias || '—'}`, 'neutral', `news risk ${d.news_risk || '—'}`),
  ].join('');

  const halted = ks.active;
  $('#killstate').innerHTML = halted
    ? `<div class="tile"><div class="label">Kill switch</div>
         <div class="value sm neg">HALTED</div>
         <div class="meta">${(ks.reasons || []).map((r) => r.reason + ': ' + r.detail).join('<br>')}</div></div>`
    : `<div class="tile"><div class="label">Kill switch</div>
         <div class="value sm pos">CLEAR</div>
         <div class="meta">no blocking conditions</div></div>`;

  const c = d.candidate;
  $('#candidate').innerHTML = c ? candidateHtml(c)
    : '<div class="empty">No trade candidate at the moment. The default state is NO TRADE.</div>';
  if (c && c.score_breakdown) scoreBars($('#cand-score'), c.score_breakdown);
  else $('#cand-score').innerHTML = '';
}

function tile(label, value, cls, meta) {
  return `<div class="panel"><div class="tile">
    <div class="label">${label}</div>
    <div class="value ${cls}">${value}</div>
    <div class="meta">${meta || ''}</div></div></div>`;
}

function candidateHtml(c) {
  const dirClass = c.direction === 'LONG' ? 'long' : 'short';
  const cls = c.classification === 'A_PLUS' ? 'aplus' : c.classification === 'A' ? 'a' : 'no';
  return `<dl class="kv">
    <dt>Classification</dt><dd><span class="badge ${cls}">${c.classification}</span></dd>
    <dt>Direction</dt><dd><span class="badge ${dirClass}">${c.direction || '—'}</span></dd>
    <dt>Setup score</dt><dd>${fmt.num(c.score, 1)} / 100</dd>
    <dt>Model probability</dt><dd>${c.probability != null ? fmt.pct(c.probability, 1) : 'no model'}</dd>
    <dt>Entry</dt><dd>${fmt.num(c.entry)}</dd>
    <dt>Stop loss</dt><dd>${fmt.num(c.sl)}</dd>
    <dt>Take profit</dt><dd>${fmt.num(c.tp)}</dd>
    <dt>Reward:risk</dt><dd>${fmt.num(c.rr)}</dd>
    <dt>Position size</dt><dd>${fmt.num(c.lots, 2)} lots</dd>
    <dt>Risk</dt><dd>${fmt.pct(c.risk_pct || 0)}</dd>
    <dt>Invalidation</dt><dd style="white-space:normal">${c.invalidation || '—'}</dd>
  </dl>`;
}

function renderDecisions() {
  const rows = state.decisions.map((d) => {
    const cls = d.classification === 'A_PLUS' ? 'aplus' : d.classification === 'A' ? 'a' : 'no';
    const dir = d.direction === 'LONG' ? 'long' : d.direction === 'SHORT' ? 'short' : '';
    return `<tr class="clickable" data-id="${d.id}">
      <td class="mono">${fmt.time(d.ts)}</td>
      <td><span class="badge ${cls}">${d.classification}</span></td>
      <td>${d.strategy || '—'}</td>
      <td>${d.direction ? `<span class="badge ${dir}">${d.direction}</span>` : '—'}</td>
      <td class="num">${fmt.num(d.score, 1)}</td>
      <td class="num">${d.probability != null ? fmt.pct(d.probability, 0) : '—'}</td>
      <td class="num">${fmt.num(d.rr)}</td>
      <td class="mono" style="color:var(--ink-3)">${d.blocking_gate || ''}</td>
    </tr>`;
  }).join('');
  $('#decisions-body').innerHTML = rows ||
    '<tr><td colspan="8" class="empty">No decisions recorded.</td></tr>';
  $$('#decisions-body tr.clickable').forEach((tr) =>
    tr.addEventListener('click', () => showDecision(tr.dataset.id)));
}

async function showDecision(id) {
  const d = await api(`/api/decisions/${id}`);
  const gates = (d.gate_trace || []).map((g) => `
    <div class="gate ${g.passed ? 'pass' : 'fail'}">
      <div class="mark">${g.passed ? 'OK' : 'X'}</div>
      <div class="name">${g.gate}</div>
      <div class="detail">${g.passed ? '' : `observed ${JSON.stringify(g.observed)} · required ${JSON.stringify(g.threshold)}`}</div>
    </div>`).join('');
  $('#detail-title').textContent =
    `${d.classification} · ${d.strategy || 'no candidate'} · ${fmt.time(d.ts)}`;
  $('#detail-gates').innerHTML = gates || '<div class="empty">No gates recorded.</div>';
  $('#detail-for').innerHTML = (d.reasons_for || []).map((r) => `<li>${r}</li>`).join('') ||
    '<li style="color:var(--ink-4)">none recorded</li>';
  $('#detail-against').innerHTML = (d.reasons_against || []).map((r) => `<li>${r}</li>`).join('') ||
    '<li style="color:var(--ink-4)">none recorded</li>';
  scoreBars($('#detail-score'), d.score_breakdown);
  $('#detail-meta').innerHTML = `<dl class="kv">
    <dt>Probability</dt><dd>${d.probability != null ? fmt.pct(d.probability, 1) : 'no model'}</dd>
    <dt>Model</dt><dd>${d.model_id || '—'} (${d.model_health || 'n/a'})</dd>
    <dt>Entry / SL / TP</dt><dd>${fmt.num(d.entry)} / ${fmt.num(d.sl)} / ${fmt.num(d.tp2 || d.tp1)}</dd>
    <dt>Reward:risk</dt><dd>${fmt.num(d.rr)}</dd>
    <dt>Size</dt><dd>${fmt.num(d.lots, 2)} lots</dd>
    <dt>Invalidation</dt><dd style="white-space:normal">${d.invalidation || '—'}</dd>
    <dt>Config hash</dt><dd>${d.config_hash || '—'}</dd>
    <dt>Cycle latency</dt><dd>${d.latency_ms} ms</dd>
  </dl>`;
  $('#detail').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderPerformance() {
  const p = state.performance;
  if (!p) return;
  const m = p.metrics || {};
  $('#perf-tiles').innerHTML = [
    tile('Trades', fmt.int(m.trades), 'neutral',
         `${m.wins || 0}W / ${m.losses || 0}L / ${m.breakevens || 0}BE`),
    tile('Win rate', fmt.pct(m.win_rate || 0, 1), 'neutral',
         `95% lower bound ${fmt.pct(m.win_rate_wilson_lower_95 || 0, 1)}`),
    tile('Expectancy', fmt.r(m.expectancy_r), sign(m.expectancy_r),
         `profit factor ${fmt.num(m.profit_factor)}`),
    tile('Max drawdown', fmt.pct(m.max_drawdown_pct || 0), 'neg',
         `${fmt.num(m.max_drawdown_r, 1)}R · ${m.max_consecutive_losses || 0} consecutive losses`),
    tile('Average win', fmt.r(m.avg_win_r), 'pos', `average loss ${fmt.r(-(m.avg_loss_r || 0))}`),
    tile('Payoff ratio', fmt.num(m.avg_rr_realised), 'neutral',
         `planned RR ${fmt.num(m.avg_rr_planned)} · travelled ${fmt.num(m.avg_rr_travelled)}`),
    tile('Sharpe', fmt.num(m.sharpe), sign(m.sharpe), `Sortino ${fmt.num(m.sortino)}`),
    tile('Risk of ruin', fmt.pct(m.risk_of_ruin || 0, 2), 'neutral',
         `${fmt.num(m.trades_per_month, 1)} trades/month`),
  ].join('');

  equityChart($('#equity'), p.equity_curve || []);
  rDistribution($('#rdist'), p.trades || []);

  $('#by-session').innerHTML = groupTable(m.by_session, 'Session');
  $('#by-class').innerHTML = groupTable(m.by_class, 'Classification');
  $('#by-regime').innerHTML = groupTable(m.by_regime, 'Regime');

  $('#trades-body').innerHTML = (p.trades || []).slice().reverse().map((t) => `
    <tr>
      <td class="mono">${fmt.time(t.closed_at)}</td>
      <td>${t.strategy}</td>
      <td><span class="badge ${t.direction === 'LONG' ? 'long' : 'short'}">${t.direction}</span></td>
      <td><span class="badge ${t.classification === 'A_PLUS' ? 'aplus' : 'a'}">${t.classification}</span></td>
      <td class="num">${fmt.num(t.entry)}</td>
      <td class="num">${fmt.num(t.exit)}</td>
      <td class="num ${sign(t.r)}">${fmt.r(t.r)}</td>
      <td class="num ${sign(t.pnl)}">${fmt.money(t.pnl)}</td>
      <td class="mono" style="color:var(--ink-3)">${t.reason}</td>
    </tr>`).join('') || '<tr><td colspan="9" class="empty">No closed trades.</td></tr>';
}

function groupTable(group, header) {
  if (!group || !Object.keys(group).length) return '<div class="empty">No data.</div>';
  const rows = Object.entries(group).map(([k, v]) => `
    <tr><td>${k}</td>
      <td class="num">${v.trades}</td>
      <td class="num">${fmt.pct(v.win_rate, 0)}</td>
      <td class="num" style="color:var(--ink-4)">${fmt.pct(v.win_rate_lower_95, 0)}</td>
      <td class="num ${sign(v.expectancy_r)}">${fmt.r(v.expectancy_r)}</td>
    </tr>`).join('');
  return `<table><thead><tr><th>${header}</th><th class="num">N</th>
    <th class="num">Win</th><th class="num">95% low</th><th class="num">Exp</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderRejections() {
  const r = state.rejections;
  if (!r) return;
  $('#rej-summary').innerHTML = [
    tile('Evaluations', fmt.int(r.total_evaluations), 'neutral', `last ${r.window_hours}h`),
    tile('Selectivity', fmt.pct(r.selectivity, 3), 'neutral', 'share that became a trade'),
    tile('A trades', fmt.int((r.classifications || {}).A || 0), 'neutral', '1% risk cap'),
    tile('A+ trades', fmt.int((r.classifications || {}).A_PLUS || 0), 'neutral', '2% risk cap'),
  ].join('');
  ledgerBars($('#ledger'), r.ledger);
}

function renderIntelligence() {
  const d = state.data || {};
  const s = d.snapshot || {};
  $('#intel-mtf').innerHTML = Object.keys(s.biases || {}).length
    ? `<table><thead><tr><th>Timeframe</th><th>Bias</th></tr></thead><tbody>` +
      Object.entries(s.biases).map(([tf, b]) =>
        `<tr><td class="mono">${tf}</td><td>${b}</td></tr>`).join('') +
      '</tbody></table>'
    : '<div class="empty">No structure data.</div>';

  const dr = s.dealing_range;
  $('#intel-range').innerHTML = dr ? `<dl class="kv">
    <dt>Range high</dt><dd>${fmt.num(dr.high)}</dd>
    <dt>Equilibrium</dt><dd>${fmt.num(dr.equilibrium)}</dd>
    <dt>Range low</dt><dd>${fmt.num(dr.low)}</dd>
    <dt>Position</dt><dd>${fmt.pct(dr.position, 1)} — ${dr.zone}</dd>
  </dl>` : '<div class="empty">No dealing range established.</div>';

  $('#intel-liq').innerHTML = zoneTable(s.liquidity, ['kind', 'price', 'resting', 'touches']);
  $('#intel-fvg').innerHTML = zoneTable(s.fvgs, ['tf', 'direction', 'top', 'bottom', 'state']);
  $('#intel-ob').innerHTML = zoneTable(s.order_blocks, ['tf', 'kind', 'top', 'bottom', 'state']);
  $('#intel-sr').innerHTML = zoneTable(s.sr_levels, ['tf', 'kind', 'price', 'touches', 'importance']);
  $('#intel-sweeps').innerHTML = zoneTable(s.sweeps, ['ts', 'kind', 'price', 'direction', 'quality']);

  const macro = s.macro || {};
  const news = s.news || {};
  $('#intel-macro').innerHTML = `<dl class="kv">
    <dt>Macro bias (gold)</dt><dd>${macro.bias || '—'}</dd>
    <dt>DXY</dt><dd>${fmt.num(macro.dxy)}</dd>
    <dt>US 10y</dt><dd>${fmt.num(macro.us10y)}</dd>
    <dt>10y real yield</dt><dd>${fmt.num(macro.real10y)}</dd>
    <dt>Data stale</dt><dd>${macro.stale ? 'YES' : 'no'}</dd>
    <dt>News risk</dt><dd>${news.risk || '—'}</dd>
    <dt>Blackout</dt><dd>${news.blackout ? 'ACTIVE — ' + (news.reason || '') : 'no'}</dd>
    <dt>Next event</dt><dd>${news.next_event || '—'}${
      news.minutes_to_event != null ? ` in ${Math.round(news.minutes_to_event)} min` : ''}</dd>
  </dl>`;
}

function zoneTable(rows, cols) {
  if (!rows || !rows.length) return '<div class="empty">None detected.</div>';
  const head = cols.map((c) => `<th class="${c === 'price' ? 'num' : ''}">${c}</th>`).join('');
  const body = rows.slice(0, 30).map((r) => `<tr>${cols.map((c) => {
    let v = r[c];
    if (typeof v === 'number') v = c === 'quality' || c === 'importance' ? v.toFixed(2) : v.toFixed(2);
    if (typeof v === 'boolean') v = v ? 'yes' : 'no';
    if (c === 'ts') v = fmt.time(v);
    return `<td class="mono">${v ?? '—'}</td>`;
  }).join('')}</tr>`).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function switchTab(name) {
  $$('nav.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  if (name === 'performance' && !state.performance) refreshPerformance();
  if (name === 'rejections') refreshRejections();
  if (name === 'system') {
    if (!jobs.catalogue.length) loadJobCatalogue();
    refreshJobHistory();
  }
}

/** Show a failure in the panel rather than leaving it blank.
 *
 * An empty panel is indistinguishable from "no data", which on a trading dashboard is
 * the difference between "the bot is being selective" and "the bot is broken". Every
 * refresh reports its own failure where the operator will see it.
 */
function panelError(selector, err) {
  const el = $(selector);
  if (el) {
    el.innerHTML = `<div class="empty" style="color:var(--short)">` +
      `Failed to load: ${String(err && err.message ? err.message : err)}</div>`;
  }
  console.error(selector, err);
}

async function refreshState() {
  try {
    state.data = await api('/api/state');
    renderCommandCentre();
    renderIntelligence();
    setConnected(true);
  } catch (e) {
    setConnected(false);
    panelError('#candidate', e);
  }
}

async function refreshDecisions() {
  try {
    state.decisions = await api('/api/decisions?limit=200');
    renderDecisions();
  } catch (e) {
    panelError('#decisions-body', e);
  }
}

async function refreshPerformance() {
  try {
    state.performance = await api('/api/performance?days=365');
    renderPerformance();
  } catch (e) {
    panelError('#perf-tiles', e);
  }
}

async function refreshRejections() {
  try {
    state.rejections = await api('/api/rejections?hours=24');
    renderRejections();
  } catch (e) {
    panelError('#ledger', e);
  }
}

function setConnected(ok) {
  const dot = $('#conn-dot');
  dot.className = 'dot ' + (ok ? 'ok' : 'bad');
  $('#conn-text').textContent = ok ? 'engine connected' : 'engine unreachable';
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  // The handshake cannot carry a header, so the token goes in the query string.
  const t = authToken();
  const qs = t ? `?token=${encodeURIComponent(t)}` : '';
  try { ws = new WebSocket(`${proto}://${location.host}/ws${qs}`); }
  catch (e) { return; }
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state') { state.data = msg.data; renderCommandCentre(); renderIntelligence(); setConnected(true); }
      if (msg.type === 'decision') refreshDecisions();
    } catch (e) { /* ignore malformed frames */ }
  };
  ws.onclose = () => setTimeout(connectWs, 4000);
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
}

async function sendCommand(name) {
  const reason = prompt(`Reason for ${name}? (recorded in the audit log)`);
  if (!reason) return;
  const r = await fetch(`/api/commands/${name.toLowerCase()}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ reason, operator: 'dashboard' }),
  });
  if (!r.ok) {
    // Never report a safety command as sent when it was not.
    alert(`${name} was NOT queued (${r.status}). Check the terminal directly.`);
    return;
  }
  const cmd = await r.json();
  alert(`${name} queued as #${cmd.id}. The engine executes it on its next poll; the `
      + `dashboard never touches the broker directly.`);
}

// ---------------------------------------------------------------------------
// System tab: the operations that used to need a command line.
// ---------------------------------------------------------------------------

const jobs = { catalogue: [], watching: null, timer: null };

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadJobCatalogue() {
  try {
    jobs.catalogue = await api('/api/jobs/catalogue');
  } catch (e) {
    panelError('#job-catalogue', e);
    return;
  }
  $('#job-catalogue').innerHTML = jobs.catalogue.map((j) => `
    <div class="job-card">
      <h3>${esc(j.title)}</h3>
      <p>${esc(j.description)}</p>
      <div class="params">
        ${Object.entries(j.params || {}).map(([name, p]) => `
          <label>${esc(name)}
            <input type="number" data-job="${esc(j.key)}" data-param="${esc(name)}"
                   value="${esc(p.default)}" min="${esc(p.min)}" max="${esc(p.max)}">
          </label>`).join('')}
      </div>
      <button class="btn" data-run="${esc(j.key)}">Run</button>
    </div>`).join('');

  $$('#job-catalogue button[data-run]').forEach((b) =>
    b.addEventListener('click', () => startJob(b.dataset.run)));
}

async function startJob(key) {
  const params = {};
  $$(`#job-catalogue input[data-job="${key}"]`).forEach((i) => {
    params[i.dataset.param] = Number(i.value);
  });

  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ key, params }),
  });
  if (r.status === 409) {
    // Something else is already running; say which rather than failing silently.
    const d = await r.json().catch(() => ({}));
    $('#job-busy').textContent = d.detail || 'another operation is already running';
    return;
  }
  if (!r.ok) {
    $('#job-busy').textContent = `could not start (${r.status})`;
    return;
  }
  const job = await r.json();
  jobs.watching = job.id;
  $('#job-busy').textContent = '';
  $('#job-output').textContent = 'starting…';
  pollJob();
}

async function pollJob() {
  clearTimeout(jobs.timer);
  if (jobs.watching == null) return;
  let job;
  try {
    job = await api(`/api/jobs/${jobs.watching}`);
  } catch (e) {
    panelError('#job-history', e);
    return;
  }

  $('#job-current-title').textContent =
    `${job.title} — ${job.status}` + (job.exit_code != null ? ` (exit ${job.exit_code})` : '');
  // textContent, not innerHTML: this is program output, not markup.
  $('#job-output').textContent = (job.output || []).join('\n') || '(no output yet)';
  const pre = $('#job-output');
  pre.scrollTop = pre.scrollHeight;

  refreshJobHistory();
  if (job.running) jobs.timer = setTimeout(pollJob, 1500);
}

async function refreshJobHistory() {
  let data;
  try {
    data = await api('/api/jobs');
  } catch (e) {
    return;
  }
  $('#job-busy').textContent = data.busy ? 'an operation is running' : '';
  $('#job-history').innerHTML = data.jobs.length
    ? data.jobs.map((j) => `
        <div class="job-row" data-open="${esc(j.id)}">
          <span>${esc(j.title)}</span>
          <span>
            <span class="when">${esc((j.started_at || '').replace('T', ' ').slice(0, 19))}</span>
            <span class="job-status ${esc(j.status)}">${esc(j.status)}</span>
          </span>
        </div>`).join('')
    : '<div class="empty">Nothing has been run yet.</div>';

  $$('#job-history .job-row').forEach((row) =>
    row.addEventListener('click', () => { jobs.watching = Number(row.dataset.open); pollJob(); }));
}

function init() {
  $$('nav.tabs button').forEach((b) =>
    b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $('#btn-halt').addEventListener('click', () => sendCommand('HALT'));
  $('#btn-flatten').addEventListener('click', () => sendCommand('FLATTEN'));
  $('#refresh').addEventListener('click', () => {
    refreshState(); refreshDecisions(); refreshPerformance(); refreshRejections();
  });

  api('/api/config').then((c) => {
    state.config = c;
    $('#mode-chip').textContent = c.mode;
    $('#mode-chip').className = 'mode-chip' + (c.live_trading ? ' live' : '');
    $('#cfg-hash').textContent = c.config_hash;
  }).catch(() => {});

  refreshState(); refreshDecisions(); refreshRejections();
  connectWs();
  setInterval(refreshState, 5000);
  setInterval(refreshDecisions, 15000);
  window.addEventListener('resize', () => { if (state.performance) renderPerformance(); });
}

document.addEventListener('DOMContentLoaded', init);
