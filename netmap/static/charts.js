'use strict';
// History charts for the details panel: traffic (down/up with peak bands) and wifi signal
// (colored by router, quality zones). Plain SVG; colors come from CSS custom properties.

const Charts = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const M = { top: 8, right: 34, bottom: 18, left: 38 };

  function node(tag, attrs = {}, parent) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (parent) parent.appendChild(el);
    return el;
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const escHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtBps(v) {
    if (!v) return '0';
    if (v < 1e3) return `${Math.round(v)}`;
    if (v < 1e6) return `${+(v / 1e3).toFixed(v < 1e4 ? 1 : 0)}k`;
    if (v < 1e9) return `${+(v / 1e6).toFixed(v < 1e7 ? 1 : 0)}M`;
    return `${+(v / 1e9).toFixed(1)}G`;
  }

  function niceMax(v) {
    if (!(v > 0)) return 1e3;
    const p = 10 ** Math.floor(Math.log10(v));
    return [1, 2, 2.5, 5, 10].map((m) => m * p).find((x) => x >= v);
  }

  // Time ticks aligned to local clock boundaries; about 3-5 per chart.
  function timeTicks(t0, t1) {
    const span = t1 - t0;
    const steps = [300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400];
    const step = steps.find((s) => span / s <= 5);
    const ticks = [];
    if (!step) {                                   // months
      const d = new Date(t0 * 1000);
      d.setDate(1); d.setHours(0, 0, 0, 0);
      const every = Math.ceil(span / (30 * 86400) / 5);
      for (d.setMonth(d.getMonth() + 1); d.getTime() / 1000 <= t1; d.setMonth(d.getMonth() + every)) {
        ticks.push([d.getTime() / 1000, d.toLocaleDateString(undefined, { month: 'short' })]);
      }
      return ticks;
    }
    const tz = new Date(t0 * 1000).getTimezoneOffset() * 60;   // align to local time
    for (let t = Math.ceil((t0 - tz) / step) * step + tz; t <= t1; t += step) {
      const d = new Date(t * 1000);
      ticks.push([t, step < 86400
        ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
        : d.toLocaleDateString(undefined, { day: 'numeric', month: 'numeric' })]);
    }
    return ticks;
  }

  function fmtTime(t, bucket) {
    const d = new Date(t * 1000);
    const day = d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'numeric' });
    if (bucket >= 3600) {
      const end = new Date((t + bucket) * 1000);
      return `${day} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}–${end.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
    }
    return `${day} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
  }

  // Split points into runs; a gap longer than 1.5 buckets (device offline) breaks the line.
  function runs(points, bucket, key = () => '') {
    const out = [];
    let cur = null;
    for (const p of points) {
      const k = key(p);
      if (!cur || p.t - cur.last > bucket * 1.5 || cur.key !== k) {
        cur = { key: k, pts: [], last: p.t };
        out.push(cur);
      }
      cur.pts.push(p);
      cur.last = p.t;
    }
    return out;
  }

  function frame(container, height, data, yMax, yMin, yTicks, yFmt) {
    container.innerHTML = '';
    const width = Math.max(200, container.clientWidth);
    const svg = node('svg', { width, height, viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'img' }, container);
    const pw = width - M.left - M.right, ph = height - M.top - M.bottom;
    const x = (t) => M.left + ((t - data.from) / (data.to - data.from)) * pw;
    const y = (v) => M.top + ph - ((v - yMin) / (yMax - yMin)) * ph;
    for (const v of yTicks) {
      node('line', { x1: M.left, x2: M.left + pw, y1: y(v), y2: y(v), class: 'grid' }, svg);
      node('text', { x: M.left - 5, y: y(v) + 3.5, class: 'tick', 'text-anchor': 'end' }, svg).textContent = yFmt(v);
    }
    for (const [t, label] of timeTicks(data.from, data.to)) {
      node('text', { x: x(t), y: height - 4, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = label;
    }
    node('line', { x1: M.left, x2: M.left + pw, y1: M.top + ph, y2: M.top + ph, class: 'axis' }, svg);
    return { svg, x, y, pw, ph, width };
  }

  function path(points, x, y, key) {
    return points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join('');
  }
  function band(points, x, y, lo, hi) {
    const top = points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p[hi]).toFixed(1)}`).join('');
    const bottom = points.slice().reverse().map((p) => `L${x(p.t).toFixed(1)},${y(p[lo]).toFixed(1)}`).join('');
    return `${top}${bottom}Z`;
  }

  // Crosshair + tooltip: nearest point by time.
  function hover(container, f, points, html) {
    const tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    container.appendChild(tip);
    const cross = node('line', { y1: M.top, y2: M.top + f.ph, class: 'cross', visibility: 'hidden' }, f.svg);
    const hit = node('rect', { x: M.left, y: 0, width: f.pw, height: M.top + f.ph, fill: 'transparent' }, f.svg);
    const move = (ev) => {
      if (!points.length) return;
      const r = f.svg.getBoundingClientRect();
      const px = ev.clientX - r.left;
      let best = points[0];
      for (const p of points) if (Math.abs(f.x(p.t) - px) < Math.abs(f.x(best.t) - px)) best = p;
      const cx = f.x(best.t);
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
      tip.innerHTML = html(best);
      tip.hidden = false;
      const left = cx + 10 + tip.offsetWidth > f.width ? cx - 10 - tip.offsetWidth : cx + 10;
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${M.top}px`;
    };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
  }

  function empty(container, height, text) {
    container.innerHTML = `<div class="chart-empty" style="height:${height}px">${escHtml(text)}</div>`;
  }

  // data.points rows: [t, down, up, down_max, up_max, sig, sig_min, ap]
  function traffic(container, data, opts = {}) {
    const pts = data.points.filter((r) => r[1] != null)
      .map((r) => ({ t: r[0], down: r[1], up: r[2], downMax: r[3], upMax: r[4] }));
    const height = opts.height || 120;
    if (!pts.length) return empty(container, height, 'No traffic history yet');
    const refs = opts.line ? [opts.line.down, opts.line.up] : [];
    const yMax = niceMax(Math.max(...pts.map((p) => Math.max(p.downMax, p.upMax)), ...refs));
    const f = frame(container, height, data, yMax, 0, [0, yMax / 2, yMax], fmtBps);
    const series = [['down', 'downMax', '--series-1'], ['up', 'upMax', '--series-2']];
    for (const run of runs(pts, data.bucket)) {
      for (const [avg, peak, color] of series) {
        node('path', { d: band(run.pts, f.x, f.y, avg, peak), fill: cssVar(color), 'fill-opacity': 0.14, stroke: 'none' }, f.svg);
      }
      for (const [avg, , color] of series) {
        node('path', { d: path(run.pts, f.x, f.y, avg), class: 'series', stroke: cssVar(color) }, f.svg);
      }
    }
    if (opts.line) {
      [[opts.line.down, '--series-1', 'line ↓'], [opts.line.up, '--series-2', 'line ↑']].forEach(([v, color, label]) => {
        node('line', { x1: M.left, x2: M.left + f.pw, y1: f.y(v), y2: f.y(v), class: 'ref', stroke: cssVar(color) }, f.svg);
        node('text', { x: M.left + f.pw + 3, y: f.y(v) + 3.5, class: 'tick' }, f.svg).textContent = label;
      });
    }
    hover(container, f, pts, (p) => `<b>${fmtTime(p.t, data.bucket)}</b>` +
      `<div><i class="key" style="background:${cssVar('--series-1')}"></i>↓ ${fmtBps(p.down)}bit/s <span>peak ${fmtBps(p.downMax)}</span></div>` +
      `<div><i class="key" style="background:${cssVar('--series-2')}"></i>↑ ${fmtBps(p.up)}bit/s <span>peak ${fmtBps(p.upMax)}</span></div>`);
  }

  // opts.apInfo(apId) -> {router, routerName, label, slot}
  function signal(container, data, opts) {
    const pts = data.points.filter((r) => r[5] != null)
      .map((r) => ({ t: r[0], sig: Math.max(-95, Math.min(-30, r[5])), sigMin: Math.max(-95, Math.min(-30, r[6])), raw: r[5], rawMin: r[6], ap: r[7] }));
    const height = opts.height || 110;
    if (!pts.length) return empty(container, height, 'No signal history yet');
    const f = frame(container, height, data, -30, -95, [-30, -65, -75, -95], (v) => `${v}`);
    const zones = [[-30, -65, '--good', 'good'], [-65, -75, '--fair', 'fair'], [-75, -95, '--weak', 'weak']];
    const grid = f.svg.querySelector('line.grid');
    for (const [hi, lo, color, label] of zones) {
      f.svg.insertBefore(node('rect', { x: M.left, y: f.y(hi), width: f.pw, height: f.y(lo) - f.y(hi), fill: cssVar(color), 'fill-opacity': 0.08 }), grid);
      node('text', { x: M.left + f.pw + 3, y: (f.y(hi) + f.y(lo)) / 2 + 3.5, class: 'tick' }, f.svg).textContent = label;
    }
    const color = (ap) => cssVar(`--series-${opts.apInfo(ap).slot}`);
    for (const run of runs(pts, data.bucket, (p) => opts.apInfo(p.ap).router)) {
      node('path', { d: band(run.pts, f.x, f.y, 'sig', 'sigMin'), fill: color(run.pts[0].ap), 'fill-opacity': 0.14, stroke: 'none' }, f.svg);
      node('path', { d: path(run.pts, f.x, f.y, 'sig'), class: 'series', stroke: color(run.pts[0].ap) }, f.svg);
    }
    hover(container, f, pts, (p) => {
      const ap = opts.apInfo(p.ap);
      return `<b>${fmtTime(p.t, data.bucket)}</b><div><i class="key" style="background:${color(p.ap)}"></i>` +
        `${Math.round(p.raw)} dBm <span>worst ${Math.round(p.rawMin)}</span></div><div>${escHtml(ap.routerName)} · ${escHtml(ap.label)}</div>`;
    });
    const seen = [...new Map(pts.map((p) => [opts.apInfo(p.ap).router, p.ap])).values()];
    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    legend.innerHTML = seen.map((ap) => `<span><i class="key" style="background:${color(ap)}"></i>${escHtml(opts.apInfo(ap).routerName)}</span>`).join('');
    container.appendChild(legend);
  }

  return { traffic, signal };
})();
