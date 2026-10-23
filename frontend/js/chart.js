// SVG charts: a time-series chart with min/max bands, limit lines, and a hover read-out;
// and a sparkline. Time always runs left to right (also in RTL pages), as on trend recorders.

import { h, s, clear, unitText } from './ui.js';
import { fmtNumber, fmtDate } from './i18n.js';

const COLORS = 8;
export function seriesClass(i) { return `c${(i % COLORS) + 1}`; }

function niceStep(range, target) {
  const raw = range / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
  return step * mag;
}

function niceTicks(lo, hi, count) {
  if (!(hi > lo)) { const pad = Math.abs(lo) * 0.1 || 1; lo -= pad; hi += pad; }
  const step = niceStep(hi - lo, count);
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
  return { lo: start, hi: end, ticks, step };
}

const TIME_STEPS = [1, 5, 15, 30, 60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
  172800, 604800, 2592000].map((x) => x * 1000);

function timeTicks(t0, t1, count) {
  const span = t1 - t0;
  const step = TIME_STEPS.find((st) => span / st <= count) || TIME_STEPS[TIME_STEPS.length - 1];
  // align to the step in local (plant) time by aligning to UTC + a fixed offset is not possible
  // in general; tick positions are aligned to UTC multiples, labels show local time.
  const ticks = [];
  for (let v = Math.ceil(t0 / step) * step; v <= t1; v += step) ticks.push(v);
  return { ticks, step };
}

function timeLabel(ms, step, span) {
  if (step >= 86400000) return fmtDate(new Date(ms), 'date');
  if (span > 86400000) return fmtDate(new Date(ms), 'daytime');
  return fmtDate(new Date(ms), step < 60000 ? 'timesec' : 'time');
}

/** Longest normal distance between points: 5x the median spacing (at least 30 s). */
export function gapLimit(points) {
  if (points.length < 3) return Infinity;
  const diffs = [];
  for (let i = 1; i < points.length; i += 1) diffs.push(points[i].t - points[i - 1].t);
  diffs.sort((a, b) => a - b);
  return Math.max(30_000, diffs[diffs.length >> 1] * 5);
}

/**
 * timeChart(container, {series: [{label, unit, points: [{t, v, min, max}], cls}],
 *   start, end, limits: [{v, label, kind: 'hi'|'lo'}], height})
 * Re-render by calling again with the same container. Points are sorted by t (ms).
 */
export function timeChart(container, opts) {
  clear(container);
  const width = Math.max(320, container.clientWidth || 800);
  const height = opts.height || 300;
  const pad = { top: 12, right: 14, bottom: 28, left: 56 };
  const series = opts.series.filter((sr) => sr.points.length);
  const t0 = opts.start ?? Math.min(...series.map((sr) => sr.points[0].t));
  const t1 = opts.end ?? Math.max(...series.map((sr) => sr.points[sr.points.length - 1].t));
  let lo = Infinity; let hi = -Infinity;
  for (const sr of series) for (const p of sr.points) {
    const a = p.min ?? p.v; const b = p.max ?? p.v;
    if (typeof a === 'number' && a < lo) lo = a;
    if (typeof b === 'number' && b > hi) hi = b;
  }
  for (const l of opts.limits || []) { lo = Math.min(lo, l.v); hi = Math.max(hi, l.v); }
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  const y = niceTicks(lo, hi, Math.max(3, Math.round(height / 55)));
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const X = (t) => pad.left + ((t - t0) / Math.max(1, t1 - t0)) * plotW;
  const Y = (v) => pad.top + (1 - (v - y.lo) / (y.hi - y.lo)) * plotH;

  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', 'aria-label': opts.label || '' });
  const g = s('g');
  for (const v of y.ticks) {
    g.append(s('line', { class: 'grid-line', x1: pad.left, x2: width - pad.right, y1: Y(v), y2: Y(v) }));
    g.append(s('text', { x: pad.left - 8, y: Y(v) + 4, 'text-anchor': 'end', direction: 'ltr' }, fmtNumber(v)));
  }
  const tt = timeTicks(t0, t1, Math.max(3, Math.floor(plotW / 110)));
  for (const v of tt.ticks) {
    g.append(s('line', { class: 'grid-line', x1: X(v), x2: X(v), y1: pad.top, y2: pad.top + plotH }));
    g.append(s('text', { x: X(v), y: height - 8, 'text-anchor': 'middle' }, timeLabel(v, tt.step, t1 - t0)));
  }
  g.append(s('line', { class: 'axis-line', x1: pad.left, x2: width - pad.right, y1: pad.top + plotH, y2: pad.top + plotH }));
  svg.append(g);

  for (const l of opts.limits || []) {
    const cls = l.kind === 'lo' ? 'c3' : 'c7';
    svg.append(s('line', { class: `limit ${cls}`, stroke: 'currentColor', x1: pad.left, x2: width - pad.right, y1: Y(l.v), y2: Y(l.v) }));
    svg.append(s('text', { class: cls, fill: 'currentColor', x: width - pad.right - 4, y: Y(l.v) - 4, 'text-anchor': 'end' }, l.label));
  }

  series.forEach((sr, i) => {
    const cls = sr.cls || seriesClass(i);
    const gap = gapLimit(sr.points);
    // min/max band, split where data is missing
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        const top = run.map((p) => `${X(p.t).toFixed(1)},${Y(p.max).toFixed(1)}`);
        const bottom = run.slice().reverse().map((p) => `${X(p.t).toFixed(1)},${Y(p.min).toFixed(1)}`);
        svg.append(s('polygon', { class: `band ${cls}`, fill: 'currentColor', 'fill-opacity': '0.14', points: [...top, ...bottom].join(' ') }));
      }
      run = [];
    };
    for (const p of sr.points) {
      if (typeof p.min !== 'number' || typeof p.max !== 'number') { flush(); continue; }
      if (run.length && p.t - run[run.length - 1].t > gap) flush();
      run.push(p);
    }
    flush();
    // the line breaks where there is no value or no data for a long time (hub or device down)
    let d = ''; let pen = false; let last = null;
    for (const p of sr.points) {
      if (typeof p.v !== 'number') { pen = false; continue; }
      if (last !== null && p.t - last > gap) pen = false;
      last = p.t;
      const px = X(p.t).toFixed(1); const py = Y(p.v).toFixed(1);
      if (!pen) { d += `M${px},${py}`; pen = true; } else if (sr.step) { d += `H${px}V${py}`; } else { d += `L${px},${py}`; }
    }
    svg.append(s('path', { class: `line ${cls}`, stroke: 'currentColor', d }));
    if (sr.points.length === 1 && typeof sr.points[0].v === 'number') {
      svg.append(s('circle', { class: cls, fill: 'currentColor', cx: X(sr.points[0].t), cy: Y(sr.points[0].v), r: 3 }));
    }
  });

  // hover read-out
  const cursor = s('line', { class: 'cursor', y1: pad.top, y2: pad.top + plotH, visibility: 'hidden' });
  const dots = series.map((sr, i) => s('circle', { class: sr.cls || seriesClass(i), fill: 'currentColor', r: 3.5, visibility: 'hidden' }));
  svg.append(cursor, ...dots);
  const tip = h('div', { class: 'chart-tip', hidden: true });
  const hit = s('rect', { x: pad.left, y: pad.top, width: plotW, height: plotH, fill: 'transparent' });
  svg.append(hit);
  const nearest = (pts, tm) => {
    let a = 0; let b = pts.length - 1;
    while (b - a > 1) { const m = (a + b) >> 1; if (pts[m].t < tm) a = m; else b = m; }
    return Math.abs(pts[a].t - tm) <= Math.abs(pts[b].t - tm) ? pts[a] : pts[b];
  };
  const move = (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * width;
    if (px < pad.left || px > width - pad.right) { leave(); return; }
    const tm = t0 + ((px - pad.left) / plotW) * (t1 - t0);
    cursor.setAttribute('x1', px); cursor.setAttribute('x2', px); cursor.setAttribute('visibility', 'visible');
    const rows = [];
    series.forEach((sr, i) => {
      const p = nearest(sr.points, tm);
      if (typeof p.v === 'number') {
        dots[i].setAttribute('cx', X(p.t)); dots[i].setAttribute('cy', Y(p.v)); dots[i].setAttribute('visibility', 'visible');
      }
      const range = typeof p.min === 'number' ? ` (${fmtNumber(p.min)}…${fmtNumber(p.max)})` : '';
      rows.push(h('div', { class: 'r' }, h('span', { class: `sw ${sr.cls || seriesClass(i)}` }), h('span', null, sr.label),
        h('span', { class: 'v' }, `${typeof p.v === 'number' ? fmtNumber(p.v) : (p.v ?? '—')} ${unitText(sr.unit)}${range}`)));
    });
    clear(tip).append(h('div', { class: 't' }, fmtDate(new Date(tm), 'datetimesec')), ...rows);
    tip.hidden = false;
    const left = (px / width) * rect.width;
    tip.style.left = `${Math.min(rect.width - tip.offsetWidth - 4, Math.max(4, left + 12))}px`;
    tip.style.top = '8px';
  };
  const leave = () => {
    cursor.setAttribute('visibility', 'hidden'); tip.hidden = true;
    dots.forEach((d) => d.setAttribute('visibility', 'hidden'));
  };
  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', leave);
  container.append(h('div', { class: 'chart-wrap' }, svg, tip));
}

export function legend(series) {
  return h('div', { class: 'legend' }, series.map((sr, i) => h('span', { class: 'item' },
    h('span', { class: `sw ${sr.cls || seriesClass(i)}` }), sr.label)));
}

/** Small trend line; points [{t, v}] */
export function sparkline(points, { width = 110, height = 28 } = {}) {
  const svg = s('svg', { class: 'spark', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  const pts = points.filter((p) => typeof p.v === 'number');
  if (pts.length < 2) return svg;
  let lo = Math.min(...pts.map((p) => p.v)); let hi = Math.max(...pts.map((p) => p.v));
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const t0 = pts[0].t; const t1 = pts[pts.length - 1].t;
  const X = (tm) => 2 + ((tm - t0) / Math.max(1, t1 - t0)) * (width - 4);
  const Y = (v) => height - 3 - ((v - lo) / (hi - lo)) * (height - 6);
  const line = pts.map((p) => `${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join(' ');
  svg.append(
    s('polygon', { class: 'area', points: `${X(t0).toFixed(1)},${height} ${line} ${X(t1).toFixed(1)},${height}` }),
    s('polyline', { class: 'line', points: line, 'vector-effect': 'non-scaling-stroke' }),
    s('circle', { class: 'end', cx: X(t1), cy: Y(pts[pts.length - 1].v), r: 2.2 }));
  return svg;
}
