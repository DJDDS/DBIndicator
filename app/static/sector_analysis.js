/* DBIndicator Sector Analysis v2: sector list -> stocks, in one view. Data from cached authenticated APIs. */
'use strict';
(() => {
const $ = id => document.getElementById(id);
const PERIODS = [['today','Today'],['1W','1W'],['1M','1M'],['3M','3M'],['6M','6M'],['52W','1Y'],['QTD','QTD']];
const PLABEL = Object.fromEntries(PERIODS);
const RANGES = [['3M',63],['6M',126],['1Y',252],['3Y',756]];
const VARIANT = /midsmall|25\/50|ex bank|nifty500|reits/i;
const S = {data:null, period:'1M', sectorId:null, detail:null, seq:0, sview:'list', variants:false,
           sort:{key:'ret', dir:-1}, filter:'all', open:null, range:'6M', cmode:'price', highlight:null};
const charts = new Map();

const ok = n => typeof n === 'number' && Number.isFinite(n);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (n, d=2) => ok(n) ? n.toLocaleString('en-IN', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
const pct = n => ok(n) ? `${n > 0 ? '+' : ''}${fmt(n)}%` : '—';
const cls = n => ok(n) ? (n > 0 ? 'pos' : n < 0 ? 'neg' : '') : 'na';
const cpct = n => `<span class="${cls(n)}">${pct(n)}</span>`;
const short = name => String(name).replace(/^nifty\s*/i, '').replace(/\s*index$/i, '').trim() || name;
const ret = (r, p = S.period) => r?.returns?.[p];

async function getJSON(url) {
  const r = await fetch(url, {cache: 'no-store'});
  if (!r.ok) { let m = `Request failed (${r.status})`; try { m = (await r.json()).error || m; } catch {} throw new Error(m); }
  return r.json();
}
function chart(id) {
  const el = $(id); if (!el || !window.echarts) return null;
  let c = charts.get(id);
  if (!c || c.getDom() !== el) { c = echarts.init(el, null, {renderer: 'canvas'}); charts.set(id, c); }
  return c;
}
window.addEventListener('resize', () => charts.forEach(c => c.resize()));

/* ---------- status ---------- */
function showStatus() {
  const s = S.data?.status || {}; const el = $('status'); const parts = [];
  if (s.running) parts.push(`Loading price history ${s.done}/${s.total}`);
  parts.push(`Daily data: ${s.updated_at ? s.updated_at.replace('T', ' ').slice(0, 16) : 'not loaded'}`);
  parts.push(`Live quotes: ${s.quote_at ? s.quote_at.replace('T', ' ').slice(11, 16) : 'none'}`);
  if (s.error) parts.push('⚠ ' + s.error);
  el.textContent = parts.join(' · ');
  el.classList.toggle('warn', !!s.error || !s.updated_at);
}

/* ---------- period chips ---------- */
function renderPeriods() {
  $('periods').innerHTML = PERIODS.map(([k, l]) => `<button data-period="${k}" aria-pressed="${k === S.period}">${l}</button>`).join('');
}

/* ---------- sector list / heat map ---------- */
function visibleSectors() {
  const rows = (S.data?.sectors || []).filter(r => S.variants || !VARIANT.test(r.name) || r.id === S.sectorId);
  return rows.sort((a, b) => {
    const av = ret(a), bv = ret(b);
    if (!ok(av)) return ok(bv) ? 1 : a.name.localeCompare(b.name);
    if (!ok(bv)) return -1;
    return bv - av;
  });
}
function renderSectors() {
  const rows = visibleSectors();
  const max = Math.max(1, ...rows.map(r => Math.abs(ret(r) ?? 0)));
  $('sector-list').innerHTML = rows.map(r => {
    const v = ret(r), w = ok(v) ? Math.abs(v) / max * 50 : 0;
    const bar = ok(v) ? `<i style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%;background:${v >= 0 ? 'var(--green)' : 'var(--red)'}"></i>` : '';
    const b50 = r.breadth?.['50']?.pct, ad = r.advance_decline;
    const adTxt = ad && ad.covered ? `${ad.advances}↑ ${ad.declines}↓` : '';
    return `<button class="srow" data-sector="${esc(r.id)}" aria-current="${r.id === S.sectorId}">
      <span class="nm" title="${esc(r.name)}">${esc(short(r.name))}</span><span class="rt ${cls(v)}">${pct(v)}</span>
      <span class="meta"><span>${r.members_list?.length ?? r.coverage?.total ?? 0} stocks</span><span class="bar">${bar}</span>
      <span title="Stocks above their 50-day average">${ok(b50) ? Math.round(b50) + '% >50D' : ''}</span><span>${adTxt}</span></span></button>`;
  }).join('') || '<p class="muted">No sector data yet.</p>';
  $('sector-heat').innerHTML = rows.map(r => {
    const v = ret(r), a = ok(v) ? Math.min(1, Math.abs(v) / max) : 0;
    const bg = !ok(v) ? '#223246' : v >= 0 ? `rgba(31,170,110,${0.25 + 0.75 * a})` : `rgba(220,70,95,${0.25 + 0.75 * a})`;
    return `<button class="tile" data-sector="${esc(r.id)}" aria-current="${r.id === S.sectorId}" style="background:${bg}"><b>${esc(short(r.name))}</b><span>${pct(v)}</span></button>`;
  }).join('');
  $('sector-list').hidden = S.sview !== 'list'; $('sector-heat').hidden = S.sview !== 'heat';
}

/* ---------- search ---------- */
function runSearch(q) {
  const box = $('search-results'); q = q.trim().toUpperCase();
  if (q.length < 2 || !S.data) { box.hidden = true; return; }
  const sectors = S.data.sectors, stocks = new Map();
  for (const s of sectors) for (const m of s.members_list || []) {
    if (m.symbol.includes(q) || String(m.name).toUpperCase().includes(q)) {
      if (!stocks.has(m.symbol)) stocks.set(m.symbol, {m, in: []});
      stocks.get(m.symbol).in.push(s);
    }
  }
  const secHits = sectors.filter(s => s.name.toUpperCase().includes(q)).slice(0, 5);
  const items = [
    ...secHits.map(s => `<button data-sector="${esc(s.id)}"><b>${esc(s.name)}</b><span>Sector · ${s.members_list?.length ?? 0} stocks · ${PLABEL[S.period]} ${pct(ret(s))}</span></button>`),
    ...[...stocks.values()].sort((a, b) => (a.m.symbol.startsWith(q) ? 0 : 1) - (b.m.symbol.startsWith(q) ? 0 : 1)).slice(0, 12).map(({m, in: secs}) => {
      const main = secs.find(s => !VARIANT.test(s.name)) || secs[0];
      return `<button data-sector="${esc(main.id)}" data-stock="${esc(m.symbol)}"><b>${esc(m.symbol)}</b> <span>${esc(m.name)} — in ${secs.map(s => esc(short(s.name))).join(', ')}</span></button>`;
    })];
  box.innerHTML = items.join('') || `<button disabled><b>No match</b><span>“${esc(q)}” is not in any Nifty sector index.</span></button>`;
  box.hidden = false;
}

/* ---------- detail ---------- */
async function selectSector(id, stock = null, user = false) {
  if (!id) return;
  S.sectorId = id; S.open = null; S.highlight = stock; S.filter = 'all';
  history.replaceState(null, '', '#' + encodeURIComponent(id));
  renderSectors();
  const row = S.data.sectors.find(r => r.id === id);
  $('detail-empty').hidden = true; $('detail-body').hidden = false;
  renderHead(row);
  // Show the stock names immediately from the overview list, then fill in numbers.
  S.detail = {sector: row, members: (row?.members_list || []).map(m => ({...m, returns: {}, vs_sector: {}, vs_nifty: {}})), loading: true};
  renderStocks(); renderSectorChart();
  if (user && window.matchMedia('(max-width:900px)').matches) $('detail').scrollIntoView({behavior: 'smooth', block: 'start'});
  const seq = ++S.seq;
  try {
    const d = await getJSON(`/api/sector-analysis/${encodeURIComponent(id)}?window=20`);
    if (seq !== S.seq) return;
    S.detail = d; renderHead(d.sector); renderStocks(); renderSectorChart();
    if (stock) { const tr = document.querySelector(`tr.row[data-sym="${CSS.escape(stock)}"]`); if (tr) { tr.scrollIntoView({block: 'center'}); toggleStock(stock); } }
  } catch (e) {
    if (seq !== S.seq) return;
    S.detail.loading = false; S.detail.error = e.message; renderStocks(); renderSectorChart();
  }
}

function renderHead(r) {
  if (!r) return;
  const v = ret(r), b = r.breadth || {}, ad = r.advance_decline || {};
  $('d-name').textContent = r.name;
  $('d-sub').textContent = `${r.members_list?.length ?? r.coverage?.total ?? 0} stocks · official Nifty list${r.retrieved_at ? ' as of ' + String(r.retrieved_at).slice(0, 10) : ''}`;
  $('d-price').textContent = fmt(r.price);
  $('d-ret').innerHTML = `${cpct(v)} <span class="muted small">${PLABEL[S.period]}</span>`;
  const kpi = (l, v) => `<div class="kpi"><small>${l}</small><strong>${v}</strong></div>`;
  const br = n => ok(b[n]?.pct) ? `${Math.round(b[n].pct)}%` : '—';
  $('d-kpis').innerHTML = [
    kpi(`vs NIFTY · ${PLABEL[S.period]}`, cpct(r.vs_nifty?.[S.period])),
    kpi('Today', cpct(r.returns?.today)),
    kpi('Up / down today', ad.covered ? `<span class="pos">${ad.advances}</span> / <span class="neg">${ad.declines}</span>` : '—'),
    kpi('Above 20-day avg', br('20')), kpi('Above 50-day avg', br('50')), kpi('Above 200-day avg', br('200')),
  ].join('');
}

const BASE = {
  sym: {key: 'sym', label: 'Stock', val: m => m.symbol},
  price: {key: 'price', label: 'Price', val: m => m.price},
  ret: {key: 'ret', label: () => PLABEL[S.period], val: m => m.returns?.[S.period], pct: true, hi: true},
  vss: {key: 'vss', label: 'vs sector', val: m => m.vs_sector?.[S.period], pct: true},
  vsn: {key: 'vsn', label: 'vs NIFTY', val: m => m.vs_nifty?.[S.period], pct: true},
  h52: {key: 'h52', label: 'vs 52W high', val: m => ok(m.price) && ok(m.high52) && m.high52 > 0 ? (m.price / m.high52 - 1) * 100 : null, pct: true},
  trend: {key: 'trend', label: 'Trend 20·50·200', val: m => trendScore(m)},
};
const OTHER = [['today', 'Today'], ['1W', '1W'], ['1M', '1M'], ['3M', '3M']].map(([p, l]) => ({key: 'p_' + p, label: l, val: m => m.returns?.[p], pct: true, period: p}));
// Selected period sits right after price; the other short periods follow.
function columns() { return [BASE.sym, BASE.price, BASE.ret, BASE.vss, ...OTHER.filter(c => c.period !== S.period), BASE.vsn, BASE.h52, BASE.trend]; }
function trendScore(m) { if (!ok(m.price) || !m.sma) return null; return ['20', '50', '200'].reduce((s, n) => s + (ok(m.sma[n]) && m.price > m.sma[n] ? 1 : 0), 0); }
function dots(m) {
  if (!ok(m.price) || !m.sma) return '<span class="na">—</span>';
  return `<span class="dots" title="Price above 20 / 50 / 200-day average">${['20', '50', '200'].map(n => `<i class="${!ok(m.sma[n]) ? '' : m.price > m.sma[n] ? 'on' : 'off'}"></i>`).join('')}</span>`;
}

function renderStocks() {
  const d = S.detail; if (!d) return;
  // Hide duplicate columns when the selected period is already shown.
  const cols = columns();
  let rows = [...(d.members || [])];
  if (S.filter === 'up') rows = rows.filter(m => ok(m.returns?.[S.period]) && m.returns[S.period] > 0);
  if (S.filter === 'down') rows = rows.filter(m => ok(m.returns?.[S.period]) && m.returns[S.period] < 0);
  const sk = cols.some(c => c.key === S.sort.key) ? S.sort.key : 'ret';
  const col = cols.find(c => c.key === sk);
  rows.sort((a, b) => {
    const av = col.val(a), bv = col.val(b);
    if (col.key === 'sym') return String(av).localeCompare(String(bv)) * -S.sort.dir;
    if (!ok(av)) return ok(bv) ? 1 : a.symbol.localeCompare(b.symbol);
    if (!ok(bv)) return -1;
    return (av - bv) * S.sort.dir;
  });
  const ups = (d.members || []).filter(m => ok(m.returns?.[S.period]) && m.returns[S.period] > 0).length;
  const priced = (d.members || []).filter(m => ok(m.returns?.[S.period])).length;
  $('stocks-title').innerHTML = `Stocks in ${esc(short(d.sector?.name || ''))} <span class="muted small">${d.members?.length || 0} total${priced ? ` · ${ups} up / ${priced - ups} down (${PLABEL[S.period]})` : ''}${d.loading ? ' · loading prices…' : ''}</span>`;
  document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.filter === S.filter)));
  const head = '<thead><tr>' + cols.map(c => `<th data-sort="${c.key}" class="${c.key === sk ? 'sorted' : ''}">${typeof c.label === 'function' ? c.label() : c.label}${c.key === sk ? (S.sort.dir < 0 ? ' ▼' : ' ▲') : ''}</th>`).join('') + '</tr></thead>';
  const span = cols.length;
  const body = rows.map(m => {
    const cells = cols.map(c => {
      if (c.hi) return `<td class="hi">${cpct(c.val(m))}</td>`;
      if (c.key === 'sym') return `<td><span class="sym">${esc(m.symbol)}</span><span class="cn">${esc(m.name || '')}</span></td>`;
      if (c.key === 'price') return `<td>${fmt(m.price)}</td>`;
      if (c.key === 'trend') return `<td>${dots(m)}</td>`;
      return `<td>${cpct(c.val(m))}</td>`;
    }).join('');
    const open = S.open === m.symbol;
    const exp = open ? `<tr class="expand"><td colspan="${span}">${expandHtml(m)}</td></tr>` : '';
    return `<tr class="row${open ? ' open' : ''}" data-sym="${esc(m.symbol)}" tabindex="0">${cells}</tr>${exp}`;
  }).join('');
  const msg = d.error ? `<tr><td colspan="${span}" class="neg">Could not load prices: ${esc(d.error)}. Stock names are still shown.</td></tr>` : '';
  $('stock-table').innerHTML = head + '<tbody>' + (body || `<tr><td colspan="${span}" class="muted">No stocks match this filter.</td></tr>`) + msg + '</tbody>';
  if (S.open) drawMini(S.open);
}

function expandHtml(m) {
  const lv = m.levels || {};
  const L = (l, v) => `<span>${l} <b>${fmt(v)}</b></span>`;
  return `<div class="exp-inner"><div id="mini-chart" class="mini"></div>
    <div class="lv">${L('Prev high', lv.previous_high)}${L('Prev low', lv.previous_low)}${L('Swing high', lv.swing_high)}${L('Swing low', lv.swing_low)}${L('52W high', m.high52)}${L('52W low', m.low52)}
    <a href="/chart/${encodeURIComponent(m.symbol)}">Open full chart ↗</a></div></div>`;
}

async function toggleStock(sym) {
  S.open = S.open === sym ? null : sym; renderStocks();
}
const stockCache = new Map();
async function drawMini(sym) {
  const c = chart('mini-chart'); if (!c) return;
  c.showLoading({text: 'Loading chart…', color: '#6aa8ff', textColor: '#8d9cb0', maskColor: 'rgba(15,24,36,.6)'});
  try {
    let d = stockCache.get(sym);
    if (!d) { d = await getJSON(`/api/sector-analysis/stock/${encodeURIComponent(sym)}`); stockCache.set(sym, d); }
    if (S.open !== sym) return;
    const k = (d.chart?.candles || []).slice(-252);
    if (!k.length) { c.hideLoading(); c.setOption({title: {text: 'Price history not loaded yet', left: 'center', top: 'middle', textStyle: {color: '#8d9cb0', fontSize: 13}}}, true); return; }
    const t0 = k[0].time, sma = n => (d.chart.sma?.[n] || []).filter(p => p.time >= t0).map(p => [p.time, p.value]);
    c.hideLoading();
    c.setOption({
      backgroundColor: 'transparent', animation: false, grid: {left: 52, right: 14, top: 28, bottom: 26},
      legend: {top: 0, textStyle: {color: '#8d9cb0'}, data: [sym, '50D avg', '200D avg']},
      tooltip: {trigger: 'axis', backgroundColor: '#0d1624', borderColor: '#223246', textStyle: {color: '#e6edf6'}},
      xAxis: {type: 'time', axisLabel: {color: '#8d9cb0'}, axisLine: {lineStyle: {color: '#223246'}}},
      yAxis: {type: 'value', scale: true, axisLabel: {color: '#8d9cb0'}, splitLine: {lineStyle: {color: '#1a2737'}}},
      series: [
        {name: sym, type: 'line', showSymbol: false, data: k.map(b => [b.time, b.close]), color: '#6aa8ff', lineStyle: {width: 2}, areaStyle: {color: 'rgba(106,168,255,.08)'}},
        {name: '50D avg', type: 'line', showSymbol: false, data: sma('50'), color: '#f5bf5c', lineStyle: {width: 1.3}},
        {name: '200D avg', type: 'line', showSymbol: false, data: sma('200'), color: '#c4a0ff', lineStyle: {width: 1.3}},
      ]}, true);
  } catch (e) { c.hideLoading(); c.setOption({title: {text: 'Chart unavailable: ' + e.message, left: 'center', top: 'middle', textStyle: {color: '#f2667a', fontSize: 12}}}, true); }
}

function renderRanges() {
  $('ranges').innerHTML = RANGES.map(([k]) => `<button data-range="${k}" aria-pressed="${k === S.range}">${k}</button>`).join('');
}
const AX = {
  tip: {trigger: 'axis', axisPointer: {type: 'cross', lineStyle: {color: '#41587a'}}, backgroundColor: '#0d1624', borderColor: '#223246', textStyle: {color: '#e6edf6', fontSize: 12}},
  x: data => ({type: 'category', data, boundaryGap: true, axisLabel: {color: '#8d9cb0', hideOverlap: true}, axisLine: {lineStyle: {color: '#223246'}}, axisTick: {show: false}}),
  y: {type: 'value', scale: true, position: 'right', axisLabel: {color: '#8d9cb0'}, splitLine: {lineStyle: {color: '#1a2737'}}},
};
function chartMessage(c, text) { c.clear(); c.setOption({title: {text, left: 'center', top: 'middle', textStyle: {color: '#8d9cb0', fontSize: 13, fontWeight: 400}}}, true); }
function renderSectorChart() {
  const d = S.detail, c = chart('sector-chart'); if (!c) return;
  document.querySelectorAll('[data-cmode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.cmode === S.cmode)));
  const name = short(d?.sector?.name || '');
  $('chart-title').textContent = S.cmode === 'price' ? `${name} — price with 20 / 50 / 200-day averages` : `${name} vs NIFTY 50 (both start at 100)`;
  if (!d || d.loading) { chartMessage(c, 'Loading sector chart…'); return; }
  const n = RANGES.find(r => r[0] === S.range)[1];
  const all = d.chart?.candles || [];
  const sk = all.slice(-n);
  if (!sk.length) { chartMessage(c, 'Sector price history is still loading on the server'); $('chart-note').textContent = ''; return; }
  const dates = sk.map(b => b.time), t0 = dates[0];
  if (S.cmode === 'price') {
    const smaMap = k => { const m = new Map((d.chart.sma?.[k] || []).map(p => [p.time, p.value])); return dates.map(t => m.get(t) ?? null); };
    const last = sk[sk.length - 1], s = {};
    ['20', '50', '200'].forEach(k => { const arr = smaMap(k); s[k] = arr[arr.length - 1]; });
    c.setOption({
      backgroundColor: 'transparent', animation: false,
      grid: {left: 10, right: 62, top: 34, bottom: 52},
      legend: {top: 0, left: 0, textStyle: {color: '#8d9cb0'}, data: ['Price', '20D avg', '50D avg', '200D avg']},
      tooltip: {...AX.tip, formatter: ps => {
        const k = ps.find(p => p.seriesName === 'Price'); const v = k?.data || [];
        const row = (l, x) => `${l}: <b>${ok(x) ? fmt(x) : '—'}</b>`;
        return `<b>${ps[0]?.axisValue}</b><br>` + (k ? [row('Open', v[1]), row('High', v[4]), row('Low', v[3]), row('Close', v[2])].join('<br>') : '') +
          ps.filter(p => p.seriesName !== 'Price').map(p => `<br>${p.marker}${p.seriesName}: <b>${ok(p.data) ? fmt(p.data) : '—'}</b>`).join('');
      }},
      xAxis: AX.x(dates), yAxis: AX.y,
      dataZoom: [{type: 'inside'}, {type: 'slider', height: 18, bottom: 6, borderColor: '#223246', textStyle: {color: '#8d9cb0'}, fillerColor: 'rgba(106,168,255,.12)'}],
      series: [
        {name: 'Price', type: 'candlestick', data: sk.map(b => [b.open, b.close, b.low, b.high]),
         itemStyle: {color: '#2fcf8f', color0: '#f2667a', borderColor: '#2fcf8f', borderColor0: '#f2667a'},
         markLine: {symbol: 'none', silent: true, label: {color: '#e6edf6', formatter: p => fmt(p.value)}, lineStyle: {color: '#6aa8ff', type: 'dashed'}, data: [{yAxis: last.close}]}},
        {name: '20D avg', type: 'line', showSymbol: false, data: smaMap('20'), color: '#6aa8ff', lineStyle: {width: 1.2}},
        {name: '50D avg', type: 'line', showSymbol: false, data: smaMap('50'), color: '#f5bf5c', lineStyle: {width: 1.4}},
        {name: '200D avg', type: 'line', showSymbol: false, data: smaMap('200'), color: '#c4a0ff', lineStyle: {width: 1.6}},
      ]}, true);
    const pos = k => ok(s[k]) ? `${last.close > s[k] ? 'above' : 'below'} ${k}D avg (${pct((last.close / s[k] - 1) * 100)})` : null;
    $('chart-note').textContent = `Last close ${fmt(last.close)} on ${last.time} · ` + ['20', '50', '200'].map(pos).filter(Boolean).join(' · ');
  } else {
    const bc = d.benchmark_chart?.candles || d.benchmark_chart || [];
    const bmap = new Map(bc.map(b => [b.time, b.close]));
    const s0 = sk[0].close, b0 = bmap.get(t0) ?? bc.find(b => b.time >= t0)?.close;
    const sec = sk.map(b => b.close / s0 * 100);
    const nif = ok(b0) ? dates.map(t => bmap.has(t) ? bmap.get(t) / b0 * 100 : null) : [];
    c.setOption({
      backgroundColor: 'transparent', animation: false, grid: {left: 10, right: 52, top: 34, bottom: 52},
      legend: {top: 0, left: 0, textStyle: {color: '#8d9cb0'}},
      tooltip: {...AX.tip, valueFormatter: v => ok(v) ? v.toFixed(1) : '—'},
      xAxis: AX.x(dates), yAxis: AX.y,
      dataZoom: [{type: 'inside'}, {type: 'slider', height: 18, bottom: 6, borderColor: '#223246', textStyle: {color: '#8d9cb0'}}],
      series: [
        {name: name, type: 'line', showSymbol: false, data: sec, color: '#6aa8ff', lineStyle: {width: 2.2}, areaStyle: {color: 'rgba(106,168,255,.07)'}},
        ...(nif.length ? [{name: 'NIFTY 50', type: 'line', showSymbol: false, connectNulls: true, data: nif, color: '#f5bf5c', lineStyle: {width: 1.6}}] : []),
      ]}, true);
    const ls = sec[sec.length - 1], ln = nif.filter(ok).pop();
    $('chart-note').textContent = ok(ln) ? `Over ${S.range}: ${name} ${pct(ls - 100)} vs NIFTY 50 ${pct(ln - 100)} → ${ls - ln >= 0 ? 'outperformed' : 'underperformed'} by ${fmt(Math.abs(ls - ln))} points.` : '';
  }
  c.resize();
}

/* ---------- events ---------- */
document.addEventListener('click', e => {
  const t = e.target.closest('button, th, tr.row'); if (!t) return;
  if (t.dataset.period) { S.period = t.dataset.period; renderPeriods(); renderSectors(); if (S.detail) { renderHead(S.detail.sector); renderStocks(); } return; }
  if (t.dataset.sview) { S.sview = t.dataset.sview; document.querySelectorAll('[data-sview]').forEach(b => b.setAttribute('aria-pressed', String(b === t))); renderSectors(); return; }
  if (t.dataset.filter) { S.filter = t.dataset.filter; renderStocks(); return; }
  if (t.dataset.range) { S.range = t.dataset.range; renderRanges(); renderSectorChart(); return; }
  if (t.dataset.cmode) { S.cmode = t.dataset.cmode; renderSectorChart(); return; }
  if (t.dataset.sort) { const k = t.dataset.sort; S.sort = {key: k, dir: S.sort.key === k ? -S.sort.dir : -1}; renderStocks(); return; }
  if (t.dataset.sector) { $('search-results').hidden = true; $('search').value = ''; selectSector(t.dataset.sector, t.dataset.stock || null, true); return; }
  if (t.matches('tr.row')) { toggleStock(t.dataset.sym); }
});
document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('tr.row')) toggleStock(e.target.dataset.sym); if (e.key === 'Escape') $('search-results').hidden = true; });
$('search').addEventListener('input', e => runSearch(e.target.value));
$('search').addEventListener('focus', e => runSearch(e.target.value));
document.addEventListener('click', e => { if (!e.target.closest('.search')) $('search-results').hidden = true; }, true);
$('show-variants').addEventListener('change', e => { S.variants = e.target.checked; renderSectors(); });

/* ---------- boot ---------- */
async function load() {
  try {
    S.data = await getJSON('/api/sector-analysis'); lastLive = Date.now();
    showStatus(); renderSectors();
    $('methodology').innerHTML = Object.entries(S.data.methodology || {}).map(([k, v]) => `<p><b>${esc(k)}</b> · ${esc(v)}</p>`).join('');
    if (!S.sectorId) {
      const want = decodeURIComponent(location.hash.slice(1));
      const first = S.data.sectors.find(r => r.id === want) || visibleSectors()[0];
      if (first) selectSector(first.id);
    }
  } catch (e) { $('status').textContent = 'Could not load sectors: ' + e.message; $('status').classList.add('warn'); }
}
renderPeriods(); renderRanges(); load();
/* ---------- live refresh every 180 s ---------- */
const LIVE_MS = 180000;
let lastLive = Date.now(), refreshing = false;
async function liveRefresh(manual = false) {
  if (refreshing || (document.hidden && !manual)) return;
  refreshing = true;
  try {
    S.data = await getJSON('/api/sector-analysis');
    showStatus(); renderSectors();
    if (S.sectorId) {
      const d = await getJSON(`/api/sector-analysis/${encodeURIComponent(S.sectorId)}?window=20`);
      S.detail = d; renderHead(d.sector); renderStocks(); renderSectorChart();
      stockCache.delete(S.open);               // re-draw the open stock with the newest candle
      if (S.open) drawMini(S.open);
    }
    lastLive = Date.now();
  } catch (e) { $('status').textContent = 'Live refresh failed: ' + e.message; $('status').classList.add('warn'); }
  finally { refreshing = false; tickLive(); }
}
function tickLive() {
  const el = $('live'); if (!el) return;
  const age = Math.round((Date.now() - lastLive) / 1000), next = Math.max(0, Math.round(LIVE_MS / 1000 - age));
  el.textContent = `● Live · updated ${new Date(lastLive).toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit', second: '2-digit'})} · next in ${next}s`;
  el.classList.toggle('stale', age > 400);
}
setInterval(liveRefresh, LIVE_MS);
setInterval(tickLive, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && Date.now() - lastLive > LIVE_MS) liveRefresh(); });
$('live').addEventListener('click', () => liveRefresh(true));
})();
