// Trends: up to 8 tags over a time range, with min/max bands, live follow and CSV export.

import { api, download } from '../api.js';
import { t, fmtDate } from '../i18n.js';
import {
  h, mount, button, select, segmented, notice, errorText, dateTimeInput, field, tagLabel, empty,
  unitText, toast,
} from '../ui.js';
import { timeChart, legend, seriesClass } from '../chart.js';
import { store, bus, can, isNumeric } from '../store.js';
import { startOfDay } from '../jalali.js';
import { timeZone } from '../i18n.js';

const MAX_TAGS = 8;
const PRESETS = [
  { id: '1h', ms: 3600e3 }, { id: '8h', ms: 8 * 3600e3 }, { id: '24h', ms: 24 * 3600e3 },
  { id: 'today' }, { id: '7d', ms: 7 * 86400e3 }, { id: '30d', ms: 30 * 86400e3 }, { id: 'custom' },
];

export async function render(page, { query, setTitle }) {
  setTitle(t('trends.title'));
  const selected = query.getAll('tag').filter((p) => store.tags.has(p)).slice(0, MAX_TAGS);
  let preset = query.get('range') || '1h';
  let follow = true;
  let data = new Map();          // path -> points
  let limits = [];
  let busy = 0;

  const chips = h('div', { class: 'row' });
  const picker = h('select', { class: 'input', 'aria-label': t('trends.add_tag') });
  const startIn = dateTimeInput(new Date(Date.now() - 86400e3));
  const endIn = dateTimeInput(new Date());
  const customRow = h('div', { class: 'row' }, field(t('trends.from'), startIn), field(t('trends.to'), endIn),
    button(t('trends.show'), () => load(), { kind: 'primary' }));
  const followBox = h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: follow, onchange: (e) => { follow = e.target.checked; } }), t('trends.follow'));
  const chartBox = h('div', { class: 'stack' });
  const info = h('div', { class: 'tiny faint' });

  function fillPicker() {
    const groups = new Map();
    for (const tag of store.tags.values()) {
      if (selected.includes(tag.tag) || !(isNumeric(tag) || tag.data_type === 'bool')) continue;
      const dev = tag.tag.split('.')[2];
      if (!groups.has(dev)) groups.set(dev, []);
      groups.get(dev).push(tag);
    }
    mount(picker, h('option', { value: '' }, t('trends.add_tag')),
      [...groups].map(([dev, tags]) => h('optgroup', { label: store.devices.get(dev)?.name || dev },
        tags.map((tag) => h('option', { value: tag.tag }, `${tagLabel(tag)} — ${tag.tag}`)))));
    picker.disabled = selected.length >= MAX_TAGS;
  }
  picker.addEventListener('change', () => {
    if (picker.value && !selected.includes(picker.value)) { selected.push(picker.value); syncUrl(); load(); }
  });

  function drawChips() {
    mount(chips, selected.map((path, i) => {
      const tag = store.tags.get(path);
      return h('span', { class: 'badge accent' }, h('span', { class: `sw ${seriesClass(i)}`, style: { width: '10px', height: '3px', display: 'inline-block' } }),
        tagLabel(tag), h('button', { type: 'button', class: 'btn ghost sm icon-only', 'aria-label': t('common.remove'), onclick: () => { selected.splice(i, 1); syncUrl(); load(); } }, '×'));
    }));
    fillPicker();
  }

  function syncUrl() {
    const q = new URLSearchParams();
    selected.forEach((p) => q.append('tag', p));
    q.set('range', preset);
    history.replaceState(null, '', `#/trends?${q}`);
  }

  function range() {
    const now = new Date();
    if (preset === 'custom') return { start: startIn.getDate(), end: endIn.getDate() };
    if (preset === 'today') return { start: startOfDay(now, timeZone()), end: now };
    return { start: new Date(now.getTime() - PRESETS.find((p) => p.id === preset).ms), end: now };
  }

  const toPoint = (p, bool) => {
    const raw = p.avg ?? p.value;
    const v = typeof raw === 'boolean' ? Number(raw) : raw;
    return { t: Date.parse(p.ts), v: typeof v === 'number' ? v : null, min: bool ? undefined : p.min ?? undefined, max: bool ? undefined : p.max ?? undefined };
  };

  async function load() {
    drawChips();
    customRow.hidden = preset !== 'custom';
    followBox.hidden = preset === 'custom';
    if (!selected.length) { mount(chartBox, empty(t('trends.pick_hint'))); info.textContent = ''; return; }
    const { start, end } = range();
    if (!start || !end || start >= end) { mount(chartBox, notice(t('trends.bad_range'), 'warn')); return; }
    const token = ++busy;
    mount(chartBox, empty(t('common.loading')));
    const width = Math.max(400, chartBox.clientWidth || 900);
    try {
      const results = await Promise.all(selected.map((path) => api(`api/tags/${encodeURIComponent(path)}/history`,
        { query: { start, end, max_points: Math.min(2000, Math.round(width / 1.5)) } })));
      if (token !== busy) return;
      data = new Map(selected.map((path, i) => [path, results[i].map((p) => toPoint(p, store.tags.get(path)?.data_type === 'bool'))]));
      limits = [];
      if (selected.length === 1 && can('config.edit')) {
        try {
          const cfg = await api('api/config/alarms');
          const rule = cfg.rules.find((r) => r.tag === selected[0]);
          if (rule) {
            for (const k of ['hihi', 'hi', 'lo', 'lolo']) if (typeof rule[k] === 'number') limits.push({ v: rule[k], label: t(`limits.${k}`), kind: k.startsWith('lo') ? 'lo' : 'hi' });
          }
        } catch { /* limits are a bonus */ }
      }
      draw();
    } catch (err) {
      if (token === busy) mount(chartBox, notice(errorText(err), 'bad'));
    }
  }

  function draw() {
    const { start, end } = range();
    const effectiveEnd = preset !== 'custom' && follow ? new Date() : end;
    const series = selected.map((path, i) => {
      const tag = store.tags.get(path);
      return { label: `${tagLabel(tag)}${tag.unit ? ` (${unitText(tag.unit)})` : ''}`, unit: tag.unit, cls: seriesClass(i), step: tag.data_type === 'bool', points: data.get(path) || [] };
    });
    const total = series.reduce((n, sr) => n + sr.points.length, 0);
    if (!total) { mount(chartBox, empty(t('trends.no_data'))); info.textContent = ''; return; }
    const holder = h('div');
    mount(chartBox, legend(series), holder);
    timeChart(holder, { series, start: start.getTime(), end: effectiveEnd.getTime(), limits, height: 360, label: t('trends.title') });
    info.textContent = t('trends.points', { n: total, from: fmtDate(start), to: fmtDate(effectiveEnd) });
  }

  // live follow: append streamed samples for the shown tags and redraw at most every 2 s
  let dirty = false;
  const onSamples = (e) => {
    if (!follow || preset === 'custom') return;
    for (const smp of e.detail || []) {
      const pts = data.get(smp.tag);
      if (!pts) continue;
      const v = typeof smp.value === 'boolean' ? Number(smp.value) : smp.value;
      if (typeof v === 'number') { pts.push({ t: Date.parse(smp.ts), v }); dirty = true; }
    }
  };
  const redraw = setInterval(() => {
    if (!dirty) return;
    dirty = false;
    const { start } = range();
    for (const pts of data.values()) { while (pts.length && pts[0].t < start.getTime()) pts.shift(); }
    draw();
  }, 2000);
  bus.addEventListener('samples', onSamples);
  const onResize = () => { if (data.size) draw(); };
  window.addEventListener('resize', onResize);

  const exportCsv = async () => {
    const { start, end } = range();
    if (!selected.length || !start || !end) return;
    try {
      for (const path of selected) {
        await download(`api/tags/${encodeURIComponent(path)}/history.csv`, { query: { start, end } }, `${path}.csv`);
      }
    } catch (err) { toast(errorText(err), 'bad'); }
  };

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('trends.title')), h('div', { class: 'sub' }, t('trends.subtitle'))),
      h('div', { class: 'row' }, button(t('trends.export_csv'), exportCsv, { iconName: 'download' }))),
    h('div', { class: 'card stack' },
      h('div', { class: 'row between' },
        segmented(PRESETS.map((p) => ({ value: p.id, label: t(`trends.range_${p.id}`) })), preset, (v) => { preset = v; syncUrl(); load(); }),
        followBox),
      customRow,
      h('div', { class: 'row' }, chips, h('div', { class: 'grow' }), picker)),
    h('div', { class: 'card stack' }, chartBox, info));
  load();

  return () => {
    bus.removeEventListener('samples', onSamples);
    window.removeEventListener('resize', onResize);
    clearInterval(redraw);
  };
}
