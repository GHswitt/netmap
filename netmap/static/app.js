'use strict';

const COLLAPSE_OVER = 8;
// Map geometry: routers are cards holding a grid of port/AP cards, which hold device pills.
const DEV_W = 200, DEV_H = 38, DEV_GAP = 6, PAD = 10, LABEL_H = 22, COLLAPSED_H = 40;
const COL_W = DEV_W + 2 * PAD + 26, ROW_GAP = 26, CARD_GAP = 90, ROUTER_PAD = 16;
// Uplink lines turn below the parent card, one lane per uplink; the gap to the next row of routers
// grows with the lanes so line labels never run into the child routers' titles.
const LANE0 = 30, LANE = 24, GAP_BASE = 100;
const levelGap = (lanes) => GAP_BASE + LANE * Math.max(1, lanes);

const $ = (s) => document.querySelector(s);
const state = {
  snap: null,
  selected: null,          // {type: 'device'|'group'|'router', id}
  collapsed: loadPref('collapsed', {}),
  sort: loadPref('sort', { key: 'status', dir: 1 }),
  q: '',
  fitted: false,
};
const prefs = loadPref('prefs', { maxAge: '7', traffic: false, histRange: '24h', theme: 'auto' });
const histCache = new Map();   // "series|range" -> {at, data}
const HIST_RANGES = ['1h', '24h', '7d', '30d', '1y'];
const filter = loadPref('filter', { off: [], solo: null });   // legend filter items switched off / soloed
let cy;

// ---------- helpers ----------
function loadPref(key, fallback) {
  try { return { ...fallback, ...JSON.parse(localStorage.getItem('netmap.' + key) || '{}') }; } catch { return { ...fallback }; }
}
function savePref(key, value) {
  try { localStorage.setItem('netmap.' + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function groupBy(list, fn) {
  const out = {};
  for (const x of list) (out[fn(x)] ||= []).push(x);
  return out;
}
function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }
function ipKey(ip) { return ip ? ip.split('.').map((n) => n.padStart(3, '0')).join('.') : '~'; }

const routerById = () => Object.fromEntries(state.snap.routers.map((r) => [r.id, r]));
const groupById = () => Object.fromEntries(state.snap.groups.map((g) => [g.id, g]));

function locLabel(d, groups = groupById(), routers = routerById()) {
  const g = groups[d.group];
  return g ? `${routers[g.router]?.name ?? g.router} › ${g.label}` : '';
}

// ---------- filtering ----------
// Each device has legend items (wired/wifi, its tags or "untagged", guest, new, offline).
// It is hidden when any of its items is switched off, or, with an item soloed, when it lacks that item.
function deviceItems(d) {
  const items = [];
  if (d.kind === 'wired' || d.kind === 'wifi') items.push(d.kind);
  for (const t of d.tags || []) items.push('tag:' + t);
  if (!d.tags?.length) items.push('untagged');
  if (d.guest) items.push('guest');
  if (d.new) items.push('new');
  if (!d.online) items.push('offline');
  return items;
}
function withinAge() {
  const now = Date.now() / 1000;
  return state.snap.devices.filter((d) => d.online || prefs.maxAge === 'all' ||
    (d.last_seen && now - d.last_seen <= Number(prefs.maxAge) * 86400));
}
function passesFilter(d) {
  const items = deviceItems(d);
  return filter.solo ? items.includes(filter.solo) : !items.some((i) => filter.off.includes(i));
}
function saveFilter() { savePref('filter', filter); }
function matches(d, q, groups, routers) {
  if (!q) return true;
  return [d.name, d.ip, ...(d.ips || []), d.mac, d.vendor, d.hostname, ...(d.tags || []), locLabel(d, groups, routers)]
    .some((v) => v && String(v).toLowerCase().includes(q));
}

// ---------- icons ----------
const ICONS = {
  router: '<rect x="2" y="13" width="20" height="7" rx="2"/><path d="M6 13V6M18 13V6M6.5 16.5h.01M10 16.5h.01"/>',
  ap: '<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1"/>',
  port: '<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M9 19v-4h6v4M8 5v3M12 5v3M16 5v3"/>',
  wired: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
};
ICONS.wifi = ICONS.ap;
function icon(name, color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${color}" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

// ---------- colors (from config.yaml "colors:", these are the fallbacks) ----------
let COLORS = {
  router: '#3b82f6', port: '#14b8a6', ap: '#8b5cf6', wired: '#22c55e', wifi: '#f97316',
  guest: '#eab308', new: '#ec4899', uplink: '#3b82f6',
};
const deviceIcon = (d) => (d.kind === 'wifi' ? 'wifi' : 'wired');
// ---------- traffic ----------
let BANDS = [[5, '#22c55e'], [25, '#f59e0b'], [60, '#ef4444']];   // heat % -> color, from config
function fmtRate(bps) {
  if (!bps) return '0';
  if (bps < 1e3) return '<1k';
  if (bps < 1e6) return `${Math.round(bps / 1e3)}k`;
  if (bps < 1e9) return `${(bps / 1e6).toFixed(bps < 1e7 ? 1 : 0)}M`;
  return `${(bps / 1e9).toFixed(1)}G`;
}
function fmtPct(x) { return x < 0.1 ? '<0.1' : x < 10 ? x.toFixed(1) : Math.round(x); }
function heatBand(t) {
  if (!t || t.shared || t.heat == null) return 0;
  let band = 0;
  BANDS.forEach(([at], i) => { if (t.heat * 100 >= at) band = i + 1; });
  return band;
}
const heatColor = (t) => (heatBand(t) ? BANDS[heatBand(t) - 1][1] : '');
function heatClass(t) { return prefs.traffic ? `tmode${heatBand(t) ? ' heat' + heatBand(t) : ''}` : ''; }
function rateText(t) {
  if (!t) return '';
  if (t.shared) return 'shared port';
  if ((t.down || 0) + (t.up || 0) < 1e3) return 'idle';
  const pct = t.line_pct != null ? ` · ${fmtPct(t.line_pct)}% line` : t.ap_pct != null ? ` · ${fmtPct(t.ap_pct)}% AP`
    : t.link_pct != null ? ` · ${fmtPct(t.link_pct)}% link` : '';
  return `↓${fmtRate(t.down)} ↑${fmtRate(t.up)}${pct}`;
}
function groupRateText(g) {
  const t = g.traffic;
  if (!t) return '';
  if (g.kind === 'ap') return `↓${fmtRate(t.down)} ↑${fmtRate(t.up)} · ${t.clients || 0} active`;
  return rateText(t);
}

const tagColors = () => Object.fromEntries((state.snap?.tags || []).map((t) => [t.name, t.color]));
const dot = (color) => 'data:image/svg+xml;utf8,' + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4.5" fill="${color}"/></svg>`);

// ---------- map ----------
const ICON = 20;

function cyStyle() {
  const C = COLORS, text = css('--text'), muted = css('--muted');
  const iconStyle = (name) => ({
    'background-image': icon(name, C[name]), 'background-width': ICON, 'background-height': ICON,
    'background-position-x': 8, 'background-position-y': '50%', 'background-clip': 'none',
    'background-image-opacity': 1, 'text-margin-x': 10,
  });
  const tint = (color, fill, border) => ({
    'background-color': color, 'background-opacity': fill, 'border-color': color, 'border-opacity': border,
  });
  return [
    { selector: 'node', style: {
      label: 'data(label)', 'font-family': css('--font'), 'font-size': 11, color: text,
      'text-wrap': 'wrap', 'text-valign': 'center', 'text-halign': 'center', shape: 'round-rectangle',
      'background-color': css('--surface'), 'border-width': 1, 'border-color': css('--border'),
    } },
    { selector: 'node[type="router"]', style: {
      width: COL_W, height: 52, 'font-size': 20, 'font-weight': 650, 'text-valign': 'top', 'text-margin-y': -10,
      'background-opacity': 0, 'border-width': 2, 'border-color': C.router, padding: ROUTER_PAD,
      'text-background-color': css('--bg'), 'text-background-opacity': 1, 'text-background-padding': 6,
      'background-image': icon('router', C.router), 'background-width': 24, 'background-height': 24,
      'background-position-x': 8, 'background-position-y': 6, 'background-clip': 'none', 'background-image-opacity': 1,
    } },
    { selector: 'node[type="router"].down', style: { 'border-color': css('--danger'), 'border-style': 'dashed' } },
    { selector: 'node[type="group"]', style: {
      width: DEV_W + 2 * PAD, height: 40, 'font-weight': 600, 'text-max-width': DEV_W - 24,
    } },
    { selector: 'node[type="group"][kind="port"]', style: { ...tint(C.port, 0.14, 0.7), ...iconStyle('port') } },
    { selector: 'node[type="group"][kind="ap"]', style: { ...tint(C.ap, 0.14, 0.7), ...iconStyle('ap') } },
    { selector: 'node[type="group"]:parent', style: {
      'text-valign': 'top', 'text-margin-y': -5, 'text-margin-x': 0, padding: PAD, 'background-opacity': 0.1,
      'background-image': 'none',
    } },
    { selector: 'node[type="device"]', style: {
      width: DEV_W, height: DEV_H, 'font-size': 10.5, 'text-max-width': DEV_W - 44,
    } },
    { selector: 'node[type="device"][kind="wifi"]', style: { ...tint(C.wifi, 0.16, 0.55), ...iconStyle('wifi') } },
    { selector: 'node[type="device"][kind!="wifi"]', style: { ...tint(C.wired, 0.16, 0.55), ...iconStyle('wired') } },
    // Traffic mode: neutral background, colored by heat band instead of kind
    { selector: 'node.tmode', style: {
      'background-color': css('--surface'), 'background-opacity': 1, 'border-color': css('--border'), 'border-opacity': 1,
    } },
    ...BANDS.map(([, c], i) => ({ selector: `node.heat${i + 1}`, style: {
      'background-color': c, 'background-opacity': 0.22 + 0.14 * i, 'border-color': c, 'border-width': 2,
    } })),
    { selector: 'node.offline', style: { opacity: 0.5, 'border-style': 'dashed' } },
    { selector: 'node.guest', style: { 'border-color': C.guest, 'border-opacity': 1, 'border-width': 2 } },
    { selector: 'node.new', style: { 'border-color': C.new, 'border-opacity': 1, 'border-width': 2 } },
    { selector: 'node.match', style: { 'border-color': css('--accent'), 'border-opacity': 1, 'border-width': 3 } },
    { selector: 'node:selected', style: {
      'border-color': css('--accent'), 'border-opacity': 1, 'border-width': 3,
      'background-color': css('--selected'), 'background-opacity': 1,
    } },
    { selector: 'edge', style: {
      width: 1.5, 'line-color': css('--edge'), 'curve-style': 'taxi', 'taxi-direction': 'downward',
      label: 'data(label)', 'font-size': 10, color: muted, 'text-background-color': css('--bg'),
      'text-background-opacity': 1, 'text-background-padding': 2,
    } },
    { selector: 'edge[type="uplink"]', style: {
      'source-label': 'data(ulabel)', 'source-text-offset': 'data(offset)',
      width: 3, 'line-color': C.uplink, 'font-size': 11, 'taxi-turn': 'data(turn)', 'z-compound-depth': 'bottom',
    } },
    ...BANDS.map(([, c], i) => ({ selector: `edge.heat${i + 1}`, style: { 'line-color': c, width: 4 + 2 * i } })),
  ];
}

function filterItems() {
  const C = COLORS;
  return [
    { id: 'wired', label: 'Wired', img: icon('wired', C.wired) },
    { id: 'wifi', label: 'Wireless', img: icon('wifi', C.wifi) },
    ...(state.snap?.tags || []).map((t) => ({ id: 'tag:' + t.name, label: t.name, dot: t.color })),
    { id: 'untagged', label: 'Untagged', dot: '' },
    { id: 'guest', label: 'Guest', box: C.guest },
    { id: 'new', label: 'New', box: C.new },
    { id: 'offline', label: 'Offline', box: css('--muted'), dashed: true },
  ];
}

function renderLegend(base = []) {
  const C = COLORS;
  const counts = {};
  for (const d of base) for (const i of deviceItems(d)) counts[i] = (counts[i] || 0) + 1;
  const key = (img, label) => `<span class="key"><img src="${img}" alt="">${esc(label)}</span>`;
  const mark = (it) => (it.img ? `<img src="${it.img}" alt="">`
    : it.dot !== undefined ? `<i class="tagdot" style="${it.dot ? `background:${esc(it.dot)};border-color:${esc(it.dot)}` : ''}"></i>`
      : `<i class="${it.dashed ? 'dashed' : ''}" style="border-color:${esc(it.box)}"></i>`);
  const on = (id) => (filter.solo ? filter.solo === id : !filter.off.includes(id));
  $('#legend').innerHTML =
    key(icon('router', C.router), 'Router') + key(icon('port', C.port), 'Ethernet port') +
    key(icon('ap', C.ap), 'Access point') + '<span class="sep"></span>' +
    filterItems().map((it) => `<button type="button" class="filter${on(it.id) ? '' : ' off'}${filter.solo === it.id ? ' solo' : ''}"` +
      ` data-f="${esc(it.id)}" aria-pressed="${on(it.id)}" title="Click: show/hide · double-click: show only this">` +
      `${mark(it)}${esc(it.label)}<b>${counts[it.id] || 0}</b></button>`).join('');
}

function applyBands(bands) {
  if (!bands || JSON.stringify(bands) === JSON.stringify(BANDS)) return;
  BANDS = bands;
  cy.style(cyStyle());
}

function applyColors(colors) {
  if (!colors || JSON.stringify(colors) === JSON.stringify(COLORS)) return;
  COLORS = { ...COLORS, ...colors };
  document.documentElement.style.setProperty('--guest', COLORS.guest);
  document.documentElement.style.setProperty('--new', COLORS.new);
  cy.style(cyStyle());
}

function hiddenNote(g) {
  const n = state.hiddenBy?.[g.id] || 0;
  return n ? `${n} hidden` : '';
}

function isCollapsed(g, count, forceOpen) {
  if (forceOpen.has(g.id)) return false;
  return state.collapsed[g.id] ?? count > COLLAPSE_OVER;
}

function buildLayout(devices, forceOpen, cols) {
  const snap = state.snap;
  const els = [];
  const groupsBy = groupBy(snap.groups, (g) => g.router);
  const devBy = groupBy(devices.filter((d) => d.group), (d) => d.group);
  const childOf = groupBy(snap.routers.filter((r) => r.parent_group), (r) => r.parent_group);
  const shown = (g) => devBy[g.id]?.length || childOf[g.id] || g.kind === 'ap';
  const open = (g) => (devBy[g.id] || []).length > 0 && !isCollapsed(g, devBy[g.id].length, forceOpen);
  const groupH = (g) => open(g) ? LABEL_H + 2 * PAD + devBy[g.id].length * (DEV_H + DEV_GAP) - DEV_GAP : COLLAPSED_H;

  // Routers by depth, children ordered like their parent's ports.
  const byId = Object.fromEntries(snap.routers.map((r) => [r.id, r]));
  const gIndex = Object.fromEntries(snap.groups.map((g, i) => [g.id, i]));
  const depth = (r) => (r.parent && byId[r.parent] && r.parent_group ? depth(byId[r.parent]) + 1 : 0);
  const levels = [];
  for (const r of snap.routers) (levels[depth(r)] ||= []).push(r);
  for (const lv of levels) lv.sort((a, b) => (gIndex[a.parent_group] ?? -1) - (gIndex[b.parent_group] ?? -1));
  const parentX = {};
  const cardCenter = {}, uplinkEdges = [];   // child router id -> x of its parent port, so uplink lines don't cross

  // Card size per router for this column count.
  const cards = {};
  for (const r of snap.routers) {
    // Ports leading to another router go last, so their uplink lines leave the card without crossing cards.
    const all = (groupsBy[r.id] || []).filter(shown);
    const gs = [...all.filter((g) => !childOf[g.id]), ...all.filter((g) => childOf[g.id])];
    const n = Math.max(1, Math.min(cols, gs.length));
    const rows = [];
    for (let i = 0; i < gs.length; i += n) rows.push(gs.slice(i, i + n));
    const rowH = rows.map((row) => Math.max(...row.map(groupH)));
    const w = n * COL_W - (COL_W - DEV_W - 2 * PAD);
    const h = rowH.reduce((s, x) => s + x, 0) + ROW_GAP * Math.max(0, rows.length - 1);
    cards[r.id] = { gs: rows, rowH, w, h: Math.max(h, COLLAPSED_H) };
  }
  const levelW = levels.map((lv) => lv.reduce((s, r) => s + cards[r.id].w, 0) + CARD_GAP * (lv.length - 1));
  const levelH = levels.map((lv) => Math.max(...lv.map((r) => cards[r.id].h)));
  const width = Math.max(...levelW) + 2 * ROUTER_PAD;
  const uplinkText = (g, c) => (c.uplink_port ? `→ ${c.uplink_port}` : '') +
    (prefs.traffic && g.traffic ? ` · ↓${fmtRate(g.traffic.down)} ↑${fmtRate(g.traffic.up)}` : '');

  // Pass 1, horizontal only: card positions, port x, children ordered under their parent ports.
  const cardLeft = {}, portX = {};
  levels.forEach((lv, li) => {
    if (li > 0) lv.sort((a, b) => (parentX[a.id] ?? Infinity) - (parentX[b.id] ?? Infinity));
    let left = (width - levelW[li]) / 2;
    for (const r of lv) {
      cardLeft[r.id] = left;
      cardCenter[r.id] = left + cards[r.id].w / 2;
      cards[r.id].gs.forEach((row) => row.forEach((g, ci) => {
        portX[g.id] = left + ci * COL_W + DEV_W / 2 + PAD;
        for (const c of childOf[g.id] || []) parentX[c.id] = portX[g.id];
      }));
      left += cards[r.id].w + CARD_GAP;
    }
  });

  // Lanes: uplinks below a level share a height unless their horizontal runs (with label) overlap.
  const laneOf = {};
  const lanes = levels.map((lv) => {
    const runs = [];
    for (const r of lv) for (const row of cards[r.id].gs) for (const g of row) for (const c of childOf[g.id] || []) {
      const x1 = portX[g.id], x2 = cardCenter[c.id] ?? x1, mid = (x1 + x2) / 2;
      const half = Math.max(Math.abs(x2 - x1) / 2, uplinkText(g, c).length * 3.6 + 10) + 14;
      runs.push({ key: `${g.id}>${c.id}`, from: mid - half, to: mid + half });
    }
    const ends = [];
    for (const run of runs.sort((a, b) => a.from - b.from)) {
      let k = ends.findIndex((end) => end < run.from);
      if (k < 0) k = ends.push(-Infinity) - 1;
      ends[k] = run.to;
      laneOf[run.key] = k;
    }
    return ends.length;
  });
  const gaps = levels.map((lv, li) => levelGap(lanes[li]));
  const height = levelH.reduce((s, x, li) => s + x + 2 * ROUTER_PAD + gaps[li], 0);

  // Pass 2: everything with its vertical position.
  let top = 0;
  levels.forEach((lv, li) => {
    const turnY = top + levelH[li] + ROUTER_PAD + LANE0;   // first lane below this level's cards
    for (const r of lv) {
      const card = cards[r.id], left = cardLeft[r.id];
      const rid = 'r:' + r.id;
      const label = `${r.name}${r.board ? ' · ' + r.board : ''}${r.online ? '' : ' · unreachable'}`;
      els.push({ data: { id: rid, type: 'router', label }, classes: r.online ? '' : 'down',
        ...(card.gs.length ? {} : { position: { x: left + card.w / 2, y: top + COLLAPSED_H / 2 } }) });
      let y = top;
      card.gs.forEach((row, ri) => {
        row.forEach((g) => {
          const x = portX[g.id];
          const gid = 'g:' + g.id;
          const devs = devBy[g.id] || [];
          const traffic = prefs.traffic ? groupRateText(g) : '';
          if (open(g)) {
            const notes = [hiddenNote(g), traffic].filter(Boolean).map((x) => ' · ' + x).join('');
            els.push({ data: { id: gid, parent: rid, type: 'group', kind: g.kind, label: g.label + notes },
              classes: heatClass(g.traffic) });
            devs.sort((a, b) => (b.online - a.online) || a.name.localeCompare(b.name)).forEach((d, i) => {
              const sig = d.wifi?.signal != null ? ` · ${d.wifi.signal} dBm` : '';
              const line2 = prefs.traffic ? (d.online ? rateText(d.traffic) || '–' : 'offline') : `${d.ip || d.mac}${sig}`;
              els.push({
                data: { id: 'd:' + d.mac, parent: gid, type: 'device', kind: d.kind || '', tags: d.tags || [],
                  label: `${d.name}\n${line2}` },
                position: { x, y: y + LABEL_H + PAD + DEV_H / 2 + i * (DEV_H + DEV_GAP) },
                classes: [d.online ? '' : 'offline', d.guest ? 'guest' : '', d.new ? 'new' : '', heatClass(d.traffic)].join(' '),
              });
            });
          } else {
            const more = devs.length ? `${devs.length} device${devs.length > 1 ? 's' : ''} ▸` : '';
            const notes = [more, hiddenNote(g), traffic].filter(Boolean).join(' · ');
            els.push({ data: { id: gid, parent: rid, type: 'group', kind: g.kind, label: g.label + (notes ? '\n' + notes : '') },
              position: { x, y: y + COLLAPSED_H / 2 }, classes: heatClass(g.traffic) });
          }
          for (const c of childOf[g.id] || []) {
            const srcY = open(g) ? y + (LABEL_H + groupH(g)) / 2 : y + COLLAPSED_H / 2;
            const srcBottom = open(g) ? y + groupH(g) : y + COLLAPSED_H;
            const lane = turnY + LANE * (laneOf[`${g.id}>${c.id}`] || 0);
            const edge = { data: { id: `e:${g.id}>${c.id}`, source: gid, target: 'r:' + c.id, type: 'uplink',
              turn: lane - srcY, label: '', ulabel: uplinkText(g, c), offset: 0 },
              classes: prefs.traffic ? `heat${heatBand(g.traffic)}` : '' };
            uplinkEdges.push({ edge, child: c.id, x, down: lane - srcBottom });
            els.push(edge);
          }
        });
        y += card.rowH[ri] + ROW_GAP;
      });
    }
    top += levelH[li] + 2 * ROUTER_PAD + gaps[li];
  });
  // Label each uplink in the middle of its horizontal run, measured along the line from the port;
  // Cytoscape's default midpoint would sit on the final drop, right above the child's title.
  for (const u of uplinkEdges) u.edge.data.offset = u.down + Math.abs((cardCenter[u.child] ?? u.x) - u.x) / 2;
  return { els, width, height };
}

// Pick the column count whose layout fits the map area best.
function buildElements(devices, forceOpen) {
  const maxCols = Math.max(1, ...Object.values(groupBy(state.snap.groups, (g) => g.router)).map((x) => x.length));
  const cw = cy.width() || 1200, ch = cy.height() || 600;
  let best = null;
  for (let c = 1; c <= maxCols; c++) {
    const l = buildLayout(devices, forceOpen, c);
    const scale = Math.min(cw / (l.width + 60), ch / (l.height + 60));
    if (!best || scale > best.scale * 1.02) best = { ...l, scale, cols: c };
  }
  return best;
}

function renderMap(devices, forceOpen, matched) {
  const layout = buildElements(devices, forceOpen);
  cy.batch(() => {
    cy.elements().remove();
    cy.add(layout.els);
    if (state.q) cy.nodes().filter((n) => matched.has(n.id())).addClass('match');
    // Tag dots: extra background images stacked at the right edge of the device pill.
    const colors = tagColors();
    cy.nodes('[type="device"]').forEach((n) => {
      const tags = n.data('tags').slice(0, 3);
      if (!tags.length) return;
      const kind = n.data('kind') === 'wifi' ? 'wifi' : 'wired';
      const ys = { 1: ['50%'], 2: ['30%', '70%'], 3: ['18%', '50%', '82%'] }[tags.length];
      n.style({
        'background-image': [icon(kind, COLORS[kind]), ...tags.map((t) => dot(colors[t] || '#94a3b8'))],
        'background-width': [ICON, ...tags.map(() => 8)], 'background-height': [ICON, ...tags.map(() => 8)],
        'background-position-x': [8, ...tags.map(() => DEV_W - 15)], 'background-position-y': ['50%', ...ys],
        'background-clip': ['none', ...tags.map(() => 'none')], 'background-image-opacity': [1, ...tags.map(() => 1)],
        'text-max-width': DEV_W - 62, 'text-margin-x': 3,
      });
    });
    if (state.selected) cy.getElementById(nodeId(state.selected)).select();
  });
  if (!state.fitted || state.cols !== layout.cols) { cy.fit(undefined, 30); state.fitted = true; }
  state.cols = layout.cols;
}

function nodeId(sel) {
  return { device: 'd:', group: 'g:', router: 'r:' }[sel.type] + sel.id;
}

// ---------- table ----------
const SORTERS = {
  status: (d) => [d.online ? 0 : 1, d.name.toLowerCase()],
  name: (d) => [d.name.toLowerCase()],
  ip: (d) => [ipKey(d.ip)],
  mac: (d) => [d.mac],
  location: (d) => [d._loc || '~', d.name.toLowerCase()],
  signal: (d) => [d.wifi?.signal != null ? -d.wifi.signal : 999],
  traffic: (d) => [-((d.traffic?.down || 0) + (d.traffic?.up || 0)), d.name.toLowerCase()],
  last_seen: (d) => [d.online ? 0 : 1, -(d.last_seen || 0)],
};
function cmp(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

function renderTable(devices) {
  const { key, dir } = state.sort;
  devices.sort((a, b) => dir * cmp(SORTERS[key](a), SORTERS[key](b)));
  const sel = state.selected?.type === 'device' ? state.selected.id : null;
  const colors = tagColors();
  $('#rows').innerHTML = devices.map((d) => {
    const badges = (d.new ? '<span class="badge new">new</span>' : '') +
      (d.guest ? '<span class="badge guest">guest</span>' : '') +
      (d.tags || []).map((t) => `<span class="badge" style="color:${esc(colors[t] || '')}">${esc(t)}</span>`).join('');
    const vendor = d.private ? '<span class="badge private">private</span>' : (d.vendor ? `<small>${esc(d.vendor)}</small>` : '');
    return `<tr data-mac="${esc(d.mac)}" class="${d.online ? '' : 'offline'} ${d.mac === sel ? 'sel' : ''}">
      <td><span class="dot ${d.online ? '' : 'off'}" title="${d.online ? 'online' : 'offline'}"></span></td>
      <td><img class="kicon" src="${icon(deviceIcon(d), COLORS[deviceIcon(d)])}" alt="">${esc(d.name)}${badges}</td>
      <td class="mono">${esc(d.ip || '')}</td>
      <td><span class="mono">${esc(d.mac)}</span>${vendor}</td>
      <td>${esc(d._loc)}</td>
      <td>${d.wifi?.signal != null ? esc(d.wifi.signal) + ' dBm' : ''}</td>
      <td class="traffic">${d.traffic ? `<span class="dot" style="background:${heatColor(d.traffic) || 'transparent'}"></span>` +
        esc(d.traffic.shared ? 'shared port' : rateText({ down: d.traffic.down, up: d.traffic.up })) : ''}</td>
      <td>${d.online ? 'now' : esc(ago(d.last_seen))}</td></tr>`;
  }).join('');
  document.querySelectorAll('th').forEach((th) => {
    th.classList.toggle('asc', th.dataset.key === key && dir === 1);
    th.classList.toggle('desc', th.dataset.key === key && dir === -1);
  });
  const online = devices.filter((d) => d.online).length;
  const hidden = Object.values(state.hiddenBy || {}).reduce((a, b) => a + b, 0) + (state.hiddenUnplaced || 0);
  $('#count').textContent = `${devices.length} devices shown · ${online} online · ${hidden} hidden by filter · ` +
    `${state.snap.devices.length} known`;
}

// ---------- details ----------
// A value that copies itself to the clipboard on click/Enter; several values (e.g. IPs) each on their own.
const copyable = (...values) => ({ copy: values.flat().filter((v) => v !== undefined && v !== null && v !== '') });
const copySpan = (v) => `<span class="copy" role="button" tabindex="0" title="Click to copy" data-copy="${esc(v)}">${esc(v)}</span>`;

function dl(pairs) {
  const empty = (v) => v === undefined || v === null || v === '' || (v.copy && !v.copy.length);
  const rows = pairs.filter(([, v]) => !empty(v));
  const cell = (v) => (v.copy ? v.copy.map(copySpan).join(', ') : esc(v));
  return rows.length ? `<dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${cell(v)}</dd>`).join('')}</dl>` : '';
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback, e.g. inside Home Assistant's iframe, which doesn't grant clipboard access.
    const ta = Object.assign(document.createElement('textarea'), { value: text, readOnly: true });
    Object.assign(ta.style, { position: 'fixed', top: '-1000px', opacity: '0' });
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* not supported */ }
    ta.remove();
    return ok;
  }
}

async function copyFromElement(el) {
  if (el.dataset.busy) return;
  el.dataset.busy = '1';
  const ok = await copyText(el.dataset.copy);
  const width = el.offsetWidth;
  el.textContent = ok ? '✓ copied' : 'copy failed';
  el.classList.add(ok ? 'copied' : 'copy-failed');
  el.style.minWidth = `${width}px`;
  setTimeout(() => {
    el.textContent = el.dataset.copy;
    el.classList.remove('copied', 'copy-failed');
    el.style.minWidth = '';
    delete el.dataset.busy;
  }, 1500);
}

function renderDetails() {
  const box = $('#details');
  const sel = state.selected;
  // While a name is being typed, leave the panel alone (the 10 s refresh would wipe the input).
  if (state.editingName && sel?.type === 'device' && sel.id === state.editingName && box.querySelector('.name-edit')) return;
  if (state.editingName && sel?.id !== state.editingName) state.editingName = null;
  if (!sel || !state.snap) { box.hidden = true; return; }
  const groups = groupById(), routers = routerById();
  let html = '';
  if (sel.type === 'device') {
    const d = state.snap.devices.find((x) => x.mac === sel.id);
    if (!d) { box.hidden = true; return; }
    const w = d.wifi || {}, l = d.lease || {};
    html = `<h2 class="name-row">${copySpan(d.name)}` +
      `<button class="edit-name" type="button" title="Rename" aria-label="Rename">✎</button></h2>` +
      `<div class="muted">${d.online ? 'online' : 'offline · last seen ' + esc(ago(d.last_seen))}</div>` +
      dl([['Name from', d.name_source], ['IP', copyable(d.ips?.length ? d.ips : [d.ip])],
        ['MAC', copyable(d.mac)], ['Vendor', d.private ? 'private (randomized) MAC' : d.vendor], ['Host name', copyable(d.hostname)],
        ['Location', locLabel(d, groups, routers)], ['Guest', d.guest ? 'yes' : '']]) +
      (d.traffic ? '<h3>Traffic</h3>' + (d.traffic.shared
        ? '<p class="muted">Shares its port with other devices, so its own traffic can\'t be measured. See the port.</p>'
        : dl([['Download', fmtRate(d.traffic.down) + 'bit/s'], ['Upload', fmtRate(d.traffic.up) + 'bit/s'],
          ['Of the line', d.traffic.line_pct != null ? fmtPct(d.traffic.line_pct) + ' %' : ''],
          ['Of the AP', d.traffic.ap_pct != null ? fmtPct(d.traffic.ap_pct) + ' %' : ''],
          ['Of the link', d.traffic.link_pct != null && d.traffic.line_pct == null ? fmtPct(d.traffic.link_pct) + ' %' : ''],
          ['Heat', d.traffic.line_pct != null ? 'absolute (WAN line)' : Math.round(d.traffic.heat * 100) + ' % of the busiest device']])) : '') +
      chartsHtml('d:' + d.mac, d.kind === 'wifi' || !!d.wifi) +
      (d.tags?.length ? '<h3>Tags</h3>' + dl(d.tags.map((t) => [t, 'matched ' + (d.tag_reasons?.[t] || '')])) : '') +
      (d.wifi ? '<h3>Wifi</h3>' + dl([['SSID', copyable(w.ssid)], ['Band', w.band],
        ['Signal', w.signal != null ? w.signal + ' dBm' : ''], ['TX rate', w.tx_rate], ['RX rate', w.rx_rate],
        ['Connected', w.uptime]]) : '') +
      (d.lease ? '<h3>DHCP lease</h3>' + dl([['Server', l.server], ['Status', l.status],
        ['Type', l.dynamic ? 'dynamic' : 'static'], ['Address', l.address], ['Comment', l.comment],
        ['Expires in', l['expires-after']], ['Last seen', l['last-seen']]]) : '') +
      (d.lldp ? '<h3>Neighbor discovery</h3>' + dl(Object.entries(d.lldp)) : '') +
      '<h3>History</h3>' + dl([['First seen', when(d.first_seen)], ['Last seen', d.online ? 'now' : when(d.last_seen)]]) +
      (d.online ? '' : d.forgettable
        ? '<p><button class="forget" type="button">Forget device…</button></p>'
        : `<p class="muted">Can't be forgotten while the routers still know it: ${esc((d.held_by || []).join(', '))}.</p>`);
  } else if (sel.type === 'group') {
    const g = groups[sel.id];
    if (!g) { box.hidden = true; return; }
    const devs = state.snap.devices.filter((d) => d.group === g.id);
    html = `<h2>${esc(g.label)}</h2><div class="muted">${esc(routers[g.router]?.name)} · ${g.kind === 'ap' ? 'access point' : 'port'}</div>` +
      dl([['Interface', g.iface], ['Comment', g.comment], ['SSID', g.ssid], ['Band', g.band],
        ['Managed by', g.via === 'capsman' ? 'CAPsMAN' : ''], ['Guest', g.guest ? 'yes' : ''],
        ['Devices', `${devs.filter((d) => d.online).length} online / ${devs.length} known`]]) +
      (g.link ? '<h3>Link</h3>' + dl([['Speed', g.link.speed || (g.link.status === 'link-ok' ? '?' : 'no link')],
        ['Port supports', g.link.max ? (g.link.max >= 1e9 ? `${g.link.max / 1e9}G` : `${g.link.max / 1e6}M`) : ''],
        ['Status', g.link.status], ['Note', g.link.note]]) +
        (g.link.warning ? `<p class="warn">⚠ ${esc(g.link.warning)}</p>` : '') : '') +
      (g.traffic ? '<h3>Traffic</h3>' + dl([['Download', fmtRate(g.traffic.down) + 'bit/s'], ['Upload', fmtRate(g.traffic.up) + 'bit/s'],
        ['Of the line', g.traffic.line_pct != null ? fmtPct(g.traffic.line_pct) + ' %' : ''],
        ['Of the link', g.traffic.link_pct != null && g.traffic.line_pct == null ? fmtPct(g.traffic.link_pct) + ' %' : ''],
        ['Active clients', g.kind === 'ap' ? g.traffic.clients : '']]) : '') +
      chartsHtml('g:' + g.id, false) +
      (devs.length ? `<p><button id="toggle" type="button">${isCollapsed(g, devs.length, new Set()) ? 'Expand' : 'Collapse'} on map</button></p>` : '');
  } else if (sel.type === 'certs') {
    const certs = state.snap.certs || [];
    const day = (ts) => new Date(ts * 1000).toLocaleDateString();
    html = '<h2>Certificates</h2>' +
      (certs.length ? `<div class="muted">checked ${esc(ago(Math.min(...certs.map((c) => c.checked))))}</div>` : '') +
      dl(certs.map((c) => [`${c.warn ? '⚠ ' : ''}${c.name}`,
        `${c.ok ? `valid until ${day(c.not_after)} (${c.days} days)` : c.error} · ${c.target}`])) +
      (state.snap.ca ? '<p><a href="ca.crt" download="home-ca.crt">Download the CA certificate</a> ' +
        'to install it on a device.</p>' : '');
  } else {
    const r = routers[sel.id];
    if (!r) { box.hidden = true; return; }
    html = `<h2>${esc(r.name)}</h2><div class="muted">${r.online ? 'reachable' : 'unreachable'}</div>` +
      dl([['Host', r.id], ['Identity', r.identity], ['Model', r.board], ['RouterOS', r.version],
        ['Uptime', r.uptime], ['CPU load', r.cpu_load != null ? r.cpu_load + ' %' : ''],
        ['Uplink', r.parent ? `${r.uplink_port || '?'} → ${routers[r.parent]?.name} ${groups[r.parent_group]?.iface || ''}` : ''],
        ['Error', r.error]]);
  }
  const keep = box.querySelector('.charts');
  box.innerHTML = '<button class="close" type="button" title="Close">×</button>' + html;
  box.hidden = false;
  const slot = box.querySelector('.charts');
  if (slot && keep && keep.dataset.series === slot.dataset.series && keep.dataset.wifi === slot.dataset.wifi) {
    slot.replaceWith(keep);          // keep the drawn charts (and an open tooltip) across refreshes
    loadHistory(keep, false);
  } else if (slot) {
    wireCharts(slot);
  }
  box.querySelector('.close').onclick = () => select(null);
  const edit = box.querySelector('.edit-name');
  if (edit) edit.onclick = () => startRename(box, sel.id);
  const forget = box.querySelector('.forget');
  if (forget) forget.onclick = () => forgetDevice(sel.id);
  const toggle = box.querySelector('#toggle');
  if (toggle) toggle.onclick = () => {
    const g = groups[sel.id];
    const count = state.snap.devices.filter((d) => d.group === g.id).length;
    setCollapsed(g.id, !isCollapsed(g, count, new Set()));
  };
}

// ---------- history charts ----------
const zoomBtn = '<button class="chart-zoom" type="button" title="Enlarge" aria-label="Enlarge charts">⤢</button>';

function chartsHtml(series, wifi) {
  const key = (v) => `<i class="key" style="background:var(${v})"></i>`;
  return `<div class="charts" data-series="${esc(series)}" data-wifi="${wifi ? 1 : ''}"><h3>Trends</h3>` +
    `<div class="ranges">${HIST_RANGES.map((r) => `<button type="button" data-range="${r}">${r}</button>`).join('')}</div>` +
    `<div class="chart-title">Traffic <span class="chart-legend">${key('--series-1')}↓ download ${key('--series-2')}↑ upload</span></div>` +
    `<div class="chart-wrap">${zoomBtn}<div class="chart-box" data-chart="traffic"></div></div>` +
    (wifi ? `<div class="chart-title">Signal (dBm)</div><div class="chart-wrap">${zoomBtn}<div class="chart-box" data-chart="signal"></div></div>` : '') + '</div>';
}

function wireCharts(el) {
  el.querySelectorAll('.chart-zoom').forEach((b) => { b.onclick = () => openChartModal(el); });
  el.querySelectorAll('[data-range]').forEach((b) => {
    b.setAttribute('aria-pressed', b.dataset.range === prefs.histRange);
    b.onclick = () => {
      prefs.histRange = b.dataset.range;
      savePref('prefs', prefs);
      el.querySelectorAll('[data-range]').forEach((x) => x.setAttribute('aria-pressed', x === b));
      loadHistory(el, true);
    };
  });
  loadHistory(el, true);
}

async function loadHistory(el, drawCached) {
  const series = el.dataset.series, range = prefs.histRange, key = `${series}|${range}`;
  const cached = histCache.get(key);
  if (cached && drawCached) drawCharts(el, cached.data);
  if (cached && Date.now() - cached.at < 30000) return;
  try {
    const res = await fetch(`api/history?id=${encodeURIComponent(series)}&range=${range}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    histCache.set(key, { at: Date.now(), data });
    if (el.isConnected && el.dataset.series === series && prefs.histRange === range) drawCharts(el, data);
  } catch (e) {
    el.querySelectorAll('.chart-box').forEach((b) => { b.innerHTML = `<div class="chart-empty">History unavailable: ${esc(e.message)}</div>`; });
  }
}

function drawCharts(el, data) {
  const snap = state.snap, groups = groupById(), routers = routerById();
  const wan = snap.traffic?.wan;
  const id = el.dataset.series.slice(2);
  const onWan = wan && (id === wan.group || snap.devices.find((d) => d.mac === id)?.group === wan.group);
  const big = el.dataset.big === '1';
  Charts.traffic(el.querySelector('[data-chart="traffic"]'), data,
    { height: big ? 300 : 120, ...(onWan ? { line: { down: wan.down_mbps * 1e6, up: wan.up_mbps * 1e6 } } : {}) });
  const sig = el.querySelector('[data-chart="signal"]');
  if (sig) {
    const order = snap.routers.map((r) => r.id);
    Charts.signal(sig, data, {
      height: big ? 260 : 110,
      apInfo: (ap) => {
        const g = groups[ap] || {};
        const router = g.router || String(ap || '').split('|')[0];
        return { router, routerName: routers[router]?.name || router, label: g.label || String(ap || '').split('|')[1] || '',
          slot: Math.min(3, Math.max(0, order.indexOf(router)) + 1) };
      },
    });
  }
}

// Large view of the history charts, on top of everything; same data, cache and range as the panel.
function openChartModal(panelCharts) {
  closeChartModal();
  const title = ($('#details h2 .copy') || $('#details h2'))?.textContent || '';
  const modal = document.createElement('div');
  modal.id = 'chartModal';
  modal.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)} history">` +
    `<button class="close" type="button" title="Close (Esc)">×</button><h2>${esc(title)}</h2>` +
    chartsHtml(panelCharts.dataset.series, panelCharts.dataset.wifi === '1').replace(/<button class="chart-zoom"[^>]*>⤢<\/button>/g, '') +
    '</div>';
  document.body.appendChild(modal);
  const el = modal.querySelector('.charts');
  el.dataset.big = '1';
  modal.querySelector('.close').onclick = closeChartModal;
  modal.addEventListener('click', (e) => { if (e.target === modal) closeChartModal(); });
  wireCharts(el);
}

function closeChartModal() {
  const modal = $('#chartModal');
  if (!modal) return;
  modal.remove();
  const panel = $('#details .charts');      // the range may have changed in the large view
  if (panel) {
    panel.querySelectorAll('[data-range]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.range === prefs.histRange));
    loadHistory(panel, true);
  }
}

// ---------- renaming (stored in netmap, routers untouched) ----------
const pendingNames = {};   // mac -> {name, source, until}: saved, not yet in a fetched snapshot

function applyPendingNames() {
  for (const [mac, p] of Object.entries(pendingNames)) {
    const d = state.snap?.devices.find((x) => x.mac === mac);
    if (!d || Date.now() > p.until || (d.name === p.name && d.name_source === p.source)) delete pendingNames[mac];
    else Object.assign(d, { name: p.name, name_source: p.source });
  }
}

const pendingForgets = {};   // mac -> until: forgotten, maybe still in a snapshot on its way
function applyPendingForgets() {
  for (const [mac, until] of Object.entries(pendingForgets)) {
    if (Date.now() > until || !state.snap?.devices.some((x) => x.mac === mac)) delete pendingForgets[mac];
    else state.snap.devices = state.snap.devices.filter((x) => x.mac !== mac);
  }
}

async function forgetDevice(mac) {
  const d = state.snap.devices.find((x) => x.mac === mac);
  if (!d || !confirm(`Forget "${d.name}"?

Deletes the stored device, a name given in netmap and its ` +
    'traffic and signal history. If it shows up again, it returns as a new device.')) return;
  try {
    const res = await fetch('api/forget', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Netmap': '1' },
      body: JSON.stringify({ mac }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    pendingForgets[mac] = Date.now() + 30000;
    applyPendingForgets();
    select(null);
  } catch (err) {
    alert(`Not forgotten: ${err.message}`);
  }
}

function startRename(box, mac) {
  const d = state.snap.devices.find((x) => x.mac === mac);
  if (!d) return;
  state.editingName = mac;
  const current = d.name_source === 'netmap (UI)' ? d.name : '';
  const fallback = d.name_source === 'netmap (UI)' ? `${d.auto_name} (${d.auto_name_source})` : `${d.name} (${d.name_source})`;
  box.querySelector('.name-row').outerHTML =
    `<form class="name-edit"><input type="text" maxlength="64" value="${esc(current)}" placeholder="${esc(d.name)}" aria-label="Name">` +
    `<button type="submit">Save</button><button type="button" class="cancel">Cancel</button>` +
    `<div class="muted hint">Empty = automatic name: ${esc(fallback)}</div><div class="warn" hidden></div></form>`;
  const form = box.querySelector('.name-edit'), input = form.querySelector('input');
  input.focus();
  input.select();
  const stop = () => { state.editingName = null; render(); };
  form.querySelector('.cancel').onclick = stop;
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); stop(); } });
  form.onsubmit = async (e) => {
    e.preventDefault();
    const name = input.value.trim().replace(/\s+/g, ' ');
    try {
      const res = await fetch('api/name', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Netmap': '1' },
        body: JSON.stringify({ mac, name }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      // A snapshot already on its way may still carry the old name: hold the new one until the
      // server's snapshot confirms it.
      pendingNames[mac] = { name: body.name, source: body.name_source, until: Date.now() + 30000 };
      applyPendingNames();
      stop();
    } catch (err) {
      const w = form.querySelector('.warn');
      w.textContent = `Not saved: ${err.message}`;
      w.hidden = false;
    }
  };
}

// ---------- header ----------
function renderHeader() {
  const snap = state.snap;
  $('#routers').innerHTML = snap.routers.map((r) =>
    `<button class="chip" type="button" data-router="${esc(r.id)}" title="${esc(r.error || r.id)}">` +
    `<span class="dot ${r.online ? '' : 'err'}"></span>${esc(r.name)}</button>`).join('');
  const w = snap.traffic?.wan;
  const wan = $('#wan');
  wan.hidden = !w;
  if (w) {
    const t = { heat: w.pct / 100 };
    wan.innerHTML = `<span class="dot" style="background:${heatColor(t) || 'var(--offline)'}"></span>WAN ` +
      `↓ ${(w.down / 1e6).toFixed(1)}/${w.down_mbps} ↑ ${(w.up / 1e6).toFixed(1)}/${w.up_mbps} Mbit/s`;
    wan.title = `${fmtPct(w.pct)} % of the line`;
    wan.dataset.group = w.group;
  }
  const certs = snap.certs || [];
  const cb = $('#certs');
  cb.hidden = !certs.length;
  if (certs.length) {
    const bad = certs.filter((c) => !c.ok).length, soon = certs.filter((c) => c.ok && c.warn).length;
    cb.innerHTML = `<span class="dot ${bad ? 'err' : soon ? 'warn' : ''}"></span>Certificates`;
    cb.title = bad ? `${bad} not trusted or unreachable` : soon ? `${soon} expiring soon` : 'all valid';
  }
}
function tick() {
  if (state.snap?.generated) $('#updated').textContent = `updated ${ago(state.snap.generated)}`;
}

// ---------- orchestration ----------
function render() {
  if (!state.snap) return;
  const groups = groupById(), routers = routerById();
  const q = state.q.trim().toLowerCase();
  const base = withinAge();
  const devices = base.filter(passesFilter);
  const hiddenDevs = base.filter((d) => !passesFilter(d));
  state.hiddenBy = {};
  for (const d of hiddenDevs) if (d.group) state.hiddenBy[d.group] = (state.hiddenBy[d.group] || 0) + 1;
  state.hiddenUnplaced = hiddenDevs.filter((d) => !d.group).length;
  devices.forEach((d) => { d._loc = locLabel(d, groups, routers); });
  const hits = devices.filter((d) => matches(d, q, groups, routers));
  const forceOpen = new Set(q ? hits.map((d) => d.group).filter(Boolean) : []);
  if (state.selected?.type === 'device') {
    const d = state.snap.devices.find((x) => x.mac === state.selected.id);
    if (d?.group) forceOpen.add(d.group);
  }
  renderHeader();
  renderLegend(base);
  renderMap(devices, forceOpen, new Set(q ? hits.map((d) => 'd:' + d.mac) : []));
  renderTable(hits);
  renderDetails();
  const modalCharts = $('#chartModal .charts');
  if (modalCharts) loadHistory(modalCharts, false);   // keep the large view current
  tick();
}

function select(sel, center = false) {
  state.selected = sel;
  render();
  if (sel && center) {
    const n = cy.getElementById(nodeId(sel));
    if (n.nonempty()) cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 0.9) }, { duration: 300 });
  }
}

function setCollapsed(gid, value) {
  state.collapsed[gid] = value;
  savePref('collapsed', state.collapsed);
  render();
}

async function refresh() {
  try {
    const res = await fetch('api/snapshot', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const snap = await res.json();
    if (!snap.routers) throw new Error('no data yet');
    state.snap = snap;
    applyPendingNames();
    applyPendingForgets();
    applyColors(snap.colors);
    applyBands(snap.traffic?.bands);
    render();
  } catch (e) {
    $('#updated').textContent = `update failed: ${e.message}`;
  }
  setTimeout(refresh, (state.snap?.poll_interval || 10) * 1000);
}

// ---------- theme: auto (follows the OS) / light / dark ----------
function applyTheme() {
  const t = prefs.theme || 'auto';
  if (t === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const b = $('#themeBtn');
  b.textContent = { auto: '◐', light: '☀', dark: '☾' }[t];
  b.title = `Theme: ${t} (click: auto → light → dark)`;
  b.setAttribute('aria-label', b.title);
}

// Colors are read from CSS when the map style and the charts are built, so rebuild both.
function themeChanged() {
  cy.style(cyStyle());
  $('#details .charts')?.remove();
  redrawModalCharts();
  render();
}

function redrawModalCharts() {
  const el = $('#chartModal .charts');
  const cached = el && histCache.get(`${el.dataset.series}|${prefs.histRange}`);
  if (cached) drawCharts(el, cached.data);
}

function init() {
  applyTheme();
  cy = cytoscape({
    container: $('#map'), style: cyStyle(), minZoom: 0.1, maxZoom: 2.5,
    autoungrabify: true, boxSelectionEnabled: false,
  });
  cy.on('tap', 'node', (e) => {
    const id = e.target.id(), type = { d: 'device', g: 'group', r: 'router' }[id[0]];
    const sel = { type, id: id.slice(2) };
    if (type === 'group' && state.selected?.type === 'group' && state.selected.id === sel.id) {
      const g = groupById()[sel.id];
      const count = state.snap.devices.filter((d) => d.group === g.id).length;
      if (count) return setCollapsed(g.id, !isCollapsed(g, count, new Set()));
    }
    select(sel);
  });
  cy.on('tap', (e) => { if (e.target === cy) select(null); });

  $('#q').addEventListener('input', (e) => { state.q = e.target.value; render(); });
  $('#maxAge').value = prefs.maxAge;
  $('#maxAge').addEventListener('change', (e) => { prefs.maxAge = e.target.value; savePref('prefs', prefs); render(); });
  // Legend filter: click toggles an item, double-click shows only that item (again: back to normal).
  let clickTimer = null;
  $('#legend').addEventListener('click', (e) => {
    const id = e.target.closest('[data-f]')?.dataset.f;
    if (!id) return;
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {
      if (filter.solo) filter.solo = null;
      else filter.off = filter.off.includes(id) ? filter.off.filter((x) => x !== id) : [...filter.off, id];
      saveFilter();
      render();
    }, 230);
  });
  $('#legend').addEventListener('dblclick', (e) => {
    const id = e.target.closest('[data-f]')?.dataset.f;
    if (!id) return;
    clearTimeout(clickTimer);
    filter.solo = filter.solo === id ? null : id;
    saveFilter();
    render();
  });
  $('#rows').addEventListener('click', (e) => {
    const tr = e.target.closest('tr');
    if (tr) select({ type: 'device', id: tr.dataset.mac }, true);
  });
  $('#routers').addEventListener('click', (e) => {
    const b = e.target.closest('[data-router]');
    if (b) select({ type: 'router', id: b.dataset.router }, true);
  });
  document.querySelector('thead').addEventListener('click', (e) => {
    const key = e.target.closest('th')?.dataset.key;
    if (!key) return;
    state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
    savePref('sort', state.sort);
    render();
  });
  $('#fit').onclick = () => cy.fit(undefined, 30);
  const trafficBtn = $('#trafficBtn');
  trafficBtn.setAttribute('aria-pressed', prefs.traffic);
  trafficBtn.onclick = () => {
    prefs.traffic = !prefs.traffic;
    trafficBtn.setAttribute('aria-pressed', prefs.traffic);
    savePref('prefs', prefs);
    render();
  };
  $('#wan').onclick = (e) => select({ type: 'group', id: e.currentTarget.dataset.group }, true);
  $('#certs').onclick = () => select({ type: 'certs', id: 'certs' });
  $('#expand').onclick = () => {
    for (const g of state.snap?.groups || []) state.collapsed[g.id] = false;
    savePref('collapsed', state.collapsed);
    render();
  };
  $('#collapse').onclick = () => {
    for (const g of state.snap?.groups || []) state.collapsed[g.id] = true;
    savePref('collapsed', state.collapsed);
    render();
  };
  initListPane();
  // Copyable values in the details panel (click or Enter/Space).
  $('#details').addEventListener('click', (e) => {
    const el = e.target.closest('.copy');
    if (el) copyFromElement(el);
  });
  $('#details').addEventListener('keydown', (e) => {
    const el = e.target.closest('.copy');
    if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); copyFromElement(el); }
  });
  renderLegend();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', themeChanged);
  $('#themeBtn').onclick = () => {
    const order = ['auto', 'light', 'dark'];
    prefs.theme = order[(order.indexOf(prefs.theme) + 1) % order.length];
    savePref('prefs', prefs);
    applyTheme();
    themeChanged();
  };
  setInterval(tick, 1000);
  addEventListener('keydown', (e) => { if (e.key === 'Escape') closeChartModal(); });
  addEventListener('resize', redrawModalCharts);
  refresh();
}

// ---------- resizable / hideable device list ----------
const pane = loadPref('pane', { listH: null, hidden: false });
const LIST_MIN = 80, MAP_MIN = 160;

function clampListH(h) {
  const room = $('main').clientHeight - $('#splitter').offsetHeight - $('#mapbar').offsetHeight - MAP_MIN;
  return Math.round(Math.max(LIST_MIN, Math.min(h, room)));
}

function applyPane(rerender = true) {
  document.body.classList.toggle('list-hidden', pane.hidden);
  $('#toggleList').textContent = pane.hidden ? 'Show list' : 'Hide list';
  $('#list').style.height = pane.listH && !pane.hidden ? clampListH(pane.listH) + 'px' : '';
  cy.resize();
  if (rerender) render();   // column count of the map adapts to its new shape
}

function initListPane() {
  const splitter = $('#splitter');
  let startY = 0, startH = 0;
  const move = (e) => {
    pane.listH = clampListH(startH - (e.clientY - startY));
    $('#list').style.height = pane.listH + 'px';
    cy.resize();
  };
  const stop = () => {
    splitter.classList.remove('dragging');
    document.body.classList.remove('dragging');
    splitter.removeEventListener('pointermove', move);
    savePref('pane', pane);
    render();
  };
  splitter.addEventListener('pointerdown', (e) => {
    startY = e.clientY;
    startH = $('#list').offsetHeight;
    splitter.setPointerCapture(e.pointerId);
    splitter.classList.add('dragging');
    document.body.classList.add('dragging');
    splitter.addEventListener('pointermove', move);
    splitter.addEventListener('pointerup', stop, { once: true });
  });
  splitter.addEventListener('dblclick', () => { pane.listH = null; savePref('pane', pane); applyPane(); });
  splitter.addEventListener('keydown', (e) => {
    const step = { ArrowUp: 24, ArrowDown: -24 }[e.key];
    if (!step) return;
    e.preventDefault();
    pane.listH = clampListH($('#list').offsetHeight + step);
    savePref('pane', pane);
    applyPane();
  });
  $('#toggleList').onclick = () => { pane.hidden = !pane.hidden; savePref('pane', pane); applyPane(); };
  addEventListener('resize', () => applyPane(false));
  applyPane(false);
}

init();
