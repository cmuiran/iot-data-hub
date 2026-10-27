// One dashboard: live widgets on a 12-column grid; the owner can rearrange and edit it.
//
// Layout (stored by the API as JSON): {version: 1, widgets: [{id, type, w, title?, ...}]}
//   value   {tag}            big live value, sparkline, quality
//   gauge   {tag, min?, max?} bar between the limits (tag min/max by default)
//   trend   {tags[], minutes} live trend of the last minutes
//   state   {device}         device connection state and key values
//   alarms  {area?}          active alarms (all visible, or one area)
//   command {tag}            opens the command dialog (only shown to users who may write)
//   note    {text}           free text for operators

import { api } from '../api.js';
import { t, fmtAgo, fmtNumber, fmtDate } from '../i18n.js';
import {
  h, mount, button, modal, field, input, select, checkbox, notice, errorText, toast, valueNode,
  tagLabel, stateBadge, severityBadge, qualityBadge, confirmDialog, empty, unitText, areaName, protoBadge,
} from '../ui.js';
import { sparkline, timeChart, legend, seriesClass } from '../chart.js';
import { store, bus, isNumeric, tagsOfDevice, severityRank, deviceLastSeen, canWriteTag } from '../store.js';
import { openWriteDialog } from '../control-dialog.js';

const TYPES = ['value', 'gauge', 'trend', 'state', 'alarms', 'command', 'note'];
const WIDTHS = [3, 4, 6, 8, 12];
const DEFAULT_W = { value: 3, gauge: 3, trend: 6, state: 3, alarms: 6, command: 3, note: 4 };

let nextId = 1;
const newId = (layout) => {
  const used = new Set(layout.widgets.map((w) => w.id));
  while (used.has(`w${nextId}`)) nextId += 1;
  return `w${nextId}`;
};

// --- widget renderers: (widget, body) -> {update(paths?) } ------------------------------------

function renderValue(w, body) {
  const tag = store.tags.get(w.tag);
  if (!tag) { mount(body, notice(t('dash.tag_missing', { tag: w.tag || '—' }), 'warn')); return {}; }
  const big = h('div', { class: 'big' });
  const meta = h('div', { class: 'row between' });
  const spark = h('div');
  mount(body, big, spark, meta);
  const update = () => {
    const cur = store.tags.get(w.tag);
    mount(big, valueNode(cur, cur.value));
    big.classList.toggle('stale', cur.quality !== 'good');
    if (isNumeric(cur)) mount(spark, sparkline(store.trail.get(w.tag) || [], { width: 240, height: 34 }));
    mount(meta, qualityBadge(cur.quality), h('span', { class: 'tiny faint' }, fmtAgo(cur.ts)));
  };
  update();
  return { update, tags: [w.tag] };
}

function renderGauge(w, body) {
  const tag = store.tags.get(w.tag);
  if (!tag || !isNumeric(tag)) { mount(body, notice(t('dash.tag_missing', { tag: w.tag || '—' }), 'warn')); return {}; }
  const lo = typeof w.min === 'number' ? w.min : tag.min ?? 0;
  const hi = typeof w.max === 'number' ? w.max : tag.max ?? 100;
  const big = h('div', { class: 'big' });
  const fill = h('div', { class: 'fill' });
  const bar = h('div', { class: 'bar', role: 'meter', 'aria-valuemin': lo, 'aria-valuemax': hi }, fill);
  mount(body, big, bar, h('div', { class: 'row between tiny faint mono' }, h('span', null, fmtNumber(lo)), h('span', null, fmtNumber(hi))));
  const update = () => {
    const cur = store.tags.get(w.tag);
    mount(big, valueNode(cur, cur.value));
    const v = typeof cur.value === 'number' ? cur.value : lo;
    const pct = Math.max(0, Math.min(100, ((v - lo) / (hi - lo || 1)) * 100));
    fill.style.width = `${pct}%`;
    fill.className = `fill${pct >= 95 ? ' bad' : pct >= 85 ? ' hi' : ''}`;
    bar.setAttribute('aria-valuenow', String(v));
  };
  update();
  return { update, tags: [w.tag] };
}

function renderTrend(w, body) {
  const paths = (w.tags || []).filter((p) => store.tags.has(p));
  if (!paths.length) { mount(body, notice(t('dash.pick_tags'), 'warn')); return {}; }
  const minutes = w.minutes || 30;
  const holder = h('div');
  const series = paths.map((p, i) => { const tag = store.tags.get(p); return { label: tagLabel(tag), unit: tag.unit, cls: seriesClass(i), step: tag.data_type === 'bool', points: [] }; });
  mount(body, legend(series), holder);
  let ready = false;
  const draw = () => {
    if (!ready || !holder.isConnected) return;
    const end = Date.now(); const start = end - minutes * 60e3;
    for (const sr of series) while (sr.points.length && sr.points[0].t < start) sr.points.shift();
    if (!series.some((sr) => sr.points.length)) { mount(holder, empty(t('trends.no_data'))); return; }
    timeChart(holder, { series, start, end, height: 220 });
  };
  Promise.all(paths.map((p) => api(`api/tags/${encodeURIComponent(p)}/history`, { query: { start: new Date(Date.now() - minutes * 60e3), max_points: 400 } })
    .catch(() => []))).then((results) => {
    results.forEach((pts, i) => {
      series[i].points = pts.map((pt) => { const raw = pt.avg ?? pt.value; const v = typeof raw === 'boolean' ? Number(raw) : raw; return { t: Date.parse(pt.ts), v: typeof v === 'number' ? v : null, min: pt.min ?? undefined, max: pt.max ?? undefined }; });
    });
    ready = true;
    draw();
  });
  let last = 0;
  const update = (samples) => {
    for (const smp of samples || []) {
      const i = paths.indexOf(smp.tag);
      const v = typeof smp.value === 'boolean' ? Number(smp.value) : smp.value;
      if (i >= 0 && typeof v === 'number') series[i].points.push({ t: Date.parse(smp.ts), v });
    }
    if (Date.now() - last > 2000) { last = Date.now(); draw(); }
  };
  return { update, tags: paths, raw: true, resize: draw };
}

function renderState(w, body) {
  const d = store.devices.get(w.device);
  if (!d) { mount(body, notice(t('dash.device_missing', { device: w.device || '—' }), 'warn')); return {}; }
  const update = () => {
    const dev = store.devices.get(w.device);
    const tags = tagsOfDevice(w.device).slice(0, 3);
    mount(body,
      h('div', { class: 'row between' }, stateBadge(dev.enabled ? dev.state : 'disabled'), protoBadge(dev.protocol)),
      h('div', { class: 'tiny faint' }, `${areaName(dev.area)} · ${fmtAgo(deviceLastSeen(w.device))}`),
      dev.last_error && dev.state !== 'online' ? h('div', { class: 'tiny mono' }, dev.last_error) : null,
      h('dl', { class: 'kv' }, tags.map((tag) => [h('dt', null, tagLabel(tag)), h('dd', { class: 'mono' }, valueNode(tag, tag.value))])));
    body.closest('.widget')?.classList.toggle('alarm-state', dev.state === 'error');
  };
  update();
  return { update, tags: tagsOfDevice(w.device).slice(0, 3).map((x) => x.tag), status: true };
}

function renderAlarms(w, body) {
  const update = () => {
    const list = store.alarms.filter((a) => !a.shelved && (!w.area || (a.tag_path || '').split('.')[1] === w.area
      || store.devices.get(a.device_id)?.area === w.area))
      .sort((a, b) => (Boolean(a.acked_at) - Boolean(b.acked_at)) || severityRank(b.severity) - severityRank(a.severity));
    mount(body, list.length ? h('div', { class: 'stack tight' }, list.slice(0, 6).map((a) => h('a', { class: 'row', href: '#/alarms' },
      severityBadge(a.severity), h('span', { class: ['grow', 'small'] }, a.message),
      h('span', { class: 'tiny faint nowrap' }, a.acked_at ? t('alarms.state_active_acked') : fmtDate(a.raised_at, 'time')))),
    list.length > 6 ? h('a', { class: 'small', href: '#/alarms' }, t('dash.more_alarms', { n: list.length - 6 })) : null)
      : h('div', { class: 'empty small' }, t('alarms.none')));
    body.closest('.widget')?.classList.toggle('alarm-state', list.some((a) => !a.acked_at && severityRank(a.severity) >= 3));
  };
  update();
  return { update, alarms: true };
}

function renderCommand(w, body) {
  const tag = store.tags.get(w.tag);
  if (!tag) { mount(body, notice(t('dash.tag_missing', { tag: w.tag || '—' }), 'warn')); return {}; }
  const big = h('div', { class: 'big' });
  const allowed = tag.access === 'read_write' && canWriteTag(tag);
  mount(body, big, allowed ? button(t('device.command'), () => openWriteDialog(w.tag), { kind: 'primary', iconName: 'control' })
    : h('span', { class: 'tiny faint' }, t('dash.no_command_right')));
  const update = () => { const cur = store.tags.get(w.tag); mount(big, valueNode(cur, cur.value)); };
  update();
  return { update, tags: [w.tag] };
}

function renderNote(w, body) {
  mount(body, h('p', { class: 'small' }, w.text || ''));
  return {};
}

const RENDER = { value: renderValue, gauge: renderGauge, trend: renderTrend, state: renderState, alarms: renderAlarms, command: renderCommand, note: renderNote };

const FIELDS = { value: ['tag'], gauge: ['tag', 'min', 'max'], trend: ['tags', 'minutes'], state: ['device'], alarms: ['area'], command: ['tag'], note: ['text'] };

/** Keep only the fields of the widget's type (switching type leaves others behind). */
function cleanWidget(w) {
  const out = { id: w.id, type: w.type, w: w.w || DEFAULT_W[w.type] };
  if (w.title) out.title = w.title;
  for (const k of FIELDS[w.type] || []) if (w[k] !== undefined && w[k] !== '') out[k] = w[k];
  return out;
}

function defaultTitle(w) {
  if (w.title) return w.title;
  if (w.type === 'trend') return (w.tags || []).map((p) => (store.tags.get(p) ? tagLabel(store.tags.get(p)) : p)).join('، ') || t('dash.type_trend');
  if (w.tag && store.tags.get(w.tag)) return tagLabel(store.tags.get(w.tag));
  if (w.device && store.devices.get(w.device)) return store.devices.get(w.device).name;
  if (w.type === 'alarms') return w.area ? `${t('nav.alarms')} · ${areaName(w.area)}` : t('nav.alarms');
  return t(`dash.type_${w.type}`);
}

// --- widget editor --------------------------------------------------------------------------------

function tagOptions(filter) {
  return [...store.tags.values()].filter(filter).map((tag) => ({ value: tag.tag, label: `${tagLabel(tag)} — ${tag.tag}` }));
}

function editWidget(widget, onSave) {
  const w = structuredClone(widget);
  const box = h('div', { class: 'stack' });
  const draw = () => {
    const typeSel = select(TYPES.map((x) => ({ value: x, label: t(`dash.type_${x}`) })), w.type, { onchange: (e) => { w.type = e.target.value; w.w = DEFAULT_W[w.type]; draw(); } });
    const title = input({ value: w.title || '', maxlength: 80, placeholder: t('dash.title_auto'), oninput: (e) => { w.title = e.target.value; } });
    const width = select(WIDTHS.map((x) => ({ value: x, label: t(`dash.width_${x}`) })), w.w || DEFAULT_W[w.type], { onchange: (e) => { w.w = Number(e.target.value); } });
    const parts = [field(t('dash.type'), typeSel), field(t('dash.widget_title'), title), field(t('dash.width'), width)];
    if (['value', 'gauge', 'command'].includes(w.type)) {
      const filter = w.type === 'gauge' ? isNumeric : w.type === 'command' ? (tag) => tag.access === 'read_write' : () => true;
      const opts = tagOptions(filter);
      if (!opts.some((o) => o.value === w.tag)) w.tag = opts[0]?.value;
      parts.push(field(t('dash.tag'), select(opts, w.tag, { onchange: (e) => { w.tag = e.target.value; } }), { wide: true }));
      if (w.type === 'gauge') {
        parts.push(field(t('dash.min'), input({ ltr: true, value: w.min ?? '', oninput: (e) => { w.min = e.target.value === '' ? undefined : Number(e.target.value); } }), { hint: t('dash.min_hint') }));
        parts.push(field(t('dash.max'), input({ ltr: true, value: w.max ?? '', oninput: (e) => { w.max = e.target.value === '' ? undefined : Number(e.target.value); } })));
      }
    } else if (w.type === 'trend') {
      w.tags = (w.tags || []).filter((p) => store.tags.has(p));
      const opts = tagOptions((tag) => isNumeric(tag) || tag.data_type === 'bool');
      const list = h('select', { class: 'input', multiple: true, size: 8 }, opts.map((o) => h('option', { value: o.value, selected: w.tags.includes(o.value) }, o.label)));
      list.addEventListener('change', () => { w.tags = [...list.selectedOptions].map((o) => o.value).slice(0, 6); });
      parts.push(field(t('dash.tags'), list, { wide: true, hint: t('dash.tags_hint') }));
      parts.push(field(t('dash.minutes'), select([10, 30, 60, 240, 720, 1440].map((m) => ({ value: m, label: t('alarms.minutes', { n: m }) })), w.minutes || 30, { onchange: (e) => { w.minutes = Number(e.target.value); } })));
    } else if (w.type === 'state') {
      const opts = [...store.devices.values()].map((d) => ({ value: d.device_id, label: `${d.name} — ${d.device_id}` }));
      if (!w.device) w.device = opts[0]?.value;
      parts.push(field(t('dash.device'), select(opts, w.device, { onchange: (e) => { w.device = e.target.value; } }), { wide: true }));
    } else if (w.type === 'alarms') {
      const areas = [...new Set([...store.devices.values()].map((d) => d.area))];
      parts.push(field(t('dash.area'), select([{ value: '', label: t('dash.all_areas') }, ...areas.map((a) => ({ value: a, label: areaName(a) }))], w.area || '', { onchange: (e) => { w.area = e.target.value || undefined; } })));
    } else if (w.type === 'note') {
      parts.push(field(t('dash.text'), h('textarea', { class: 'input', maxlength: 1000, rows: 4, value: w.text || '', oninput: (e) => { w.text = e.target.value; } }), { wide: true }));
    }
    mount(box, h('div', { class: 'form-grid' }, parts));
  };
  draw();
  modal({
    title: t('dash.edit_widget'), body: box, wide: true,
    actions: [{ label: t('common.cancel') }, { label: t('common.apply'), kind: 'primary', onClick: () => onSave(cleanWidget(w)) }],
  });
}

// --- page ---------------------------------------------------------------------------------------

export async function render(page, { params, query, navigate, setTitle }) {
  let board;
  try { board = await api(`api/dashboards/${encodeURIComponent(params.id)}`); } catch (err) { mount(page, notice(errorText(err), 'bad')); return null; }
  const owner = board.owner_id === store.me.user_id;
  let layout = board.layout && Array.isArray(board.layout.widgets) ? structuredClone(board.layout) : { version: 1, widgets: [] };
  let editing = owner && query.get('edit') === '1';
  let dirty = false;
  let live = [];
  setTitle(board.name);

  const head = h('div', { class: 'page-head' });
  const grid = h('div', { class: 'dash-grid' });
  mount(page, head, grid);

  function drawHead() {
    mount(head,
      h('div', null, h('h1', null, board.name),
        h('div', { class: 'sub' }, owner ? (board.shared ? t('dash.shared_mine') : t('dash.private')) : t('dash.by', { owner: board.owner }))),
      h('div', { class: 'row' },
        h('a', { class: 'btn ghost', href: '#/dashboards' }, t('dash.all')),
        owner && !editing ? button(t('dash.edit'), () => { editing = true; drawAll(); }, { iconName: 'edit' }) : null,
        editing ? button(t('dash.add_widget'), () => editWidget({ id: newId(layout), type: 'value', w: 3 }, (w) => { layout.widgets.push(w); dirty = true; drawAll(); }), { iconName: 'plus' }) : null,
        editing ? button(t('dash.settings'), settings, { iconName: 'system' }) : null,
        editing ? button(t('common.save'), save, { kind: 'primary', iconName: 'check' }) : null,
        editing ? button(t('common.cancel'), async () => {
          if (dirty && !(await confirmDialog(t('dash.discard_title'), t('dash.discard_text')))) return;
          layout = structuredClone(board.layout || { version: 1, widgets: [] }); dirty = false; editing = false; drawAll();
        }) : null));
  }

  async function save() {
    try {
      board = await api(`api/dashboards/${board.dashboard_id}`, { method: 'PUT', body: { name: board.name, layout, shared: board.shared, is_default: board.is_default } });
      dirty = false; editing = false;
      history.replaceState(null, '', `#/dashboards/${board.dashboard_id}`);
      toast(t('dash.saved'), 'ok');
      drawAll();
    } catch (err) { toast(errorText(err), 'bad'); }
  }

  function settings() {
    const name = input({ value: board.name, maxlength: 100 });
    const shared = checkbox(t('dash.share'), board.shared);
    const def = checkbox(t('dash.make_default'), board.is_default);
    modal({
      title: t('dash.settings'),
      body: h('div', { class: 'stack' }, field(t('dash.name'), name), shared, h('span', { class: 'tiny faint' }, t('dash.share_hint')), def),
      actions: [
        { label: t('dash.delete'), kind: 'danger', onClick: async ({ close }) => {
          close();
          if (!(await confirmDialog(t('dash.delete'), t('dash.delete_text', { name: board.name }), { danger: true, confirmLabel: t('dash.delete') }))) return;
          await api(`api/dashboards/${board.dashboard_id}`, { method: 'DELETE' });
          if (store.home === `dashboards/${board.dashboard_id}`) store.home = 'overview';
          navigate('dashboards');
        } },
        { label: t('common.cancel') },
        { label: t('common.apply'), kind: 'primary', onClick: async () => {
          if (!name.value.trim()) throw new Error(t('dash.name_required'));
          board = await api(`api/dashboards/${board.dashboard_id}`, { method: 'PUT', body: { name: name.value.trim(), layout, shared: shared.input.checked, is_default: def.input.checked } });
          store.home = board.is_default ? `dashboards/${board.dashboard_id}` : (store.home === `dashboards/${board.dashboard_id}` ? 'overview' : store.home);
          dirty = false;
          setTitle(board.name);
          drawHead();
        } },
      ],
    });
  }

  let dragFrom = null;
  function widgetCard(w, index) {
    const body = h('div', { class: 'stack tight' });
    const tools = editing ? h('div', { class: 'w-tools' },
      button('', () => { if (index > 0) { layout.widgets.splice(index - 1, 0, layout.widgets.splice(index, 1)[0]); dirty = true; drawAll(); } }, { kind: 'ghost', size: 'sm', iconName: 'up', title: t('dash.move_earlier') }),
      button('', () => { if (index < layout.widgets.length - 1) { layout.widgets.splice(index + 1, 0, layout.widgets.splice(index, 1)[0]); dirty = true; drawAll(); } }, { kind: 'ghost', size: 'sm', iconName: 'down', title: t('dash.move_later') }),
      button('', () => editWidget(w, (nw) => { layout.widgets[index] = nw; dirty = true; drawAll(); }), { kind: 'ghost', size: 'sm', iconName: 'edit', title: t('dash.edit_widget') }),
      button('', () => { layout.widgets.splice(index, 1); dirty = true; drawAll(); }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.remove') })) : null;
    const card = h('section', {
      class: ['card', 'widget', `w${w.w || DEFAULT_W[w.type] || 4}`, editing && 'editing'],
      draggable: editing ? 'true' : undefined,
      ondragstart: editing ? (e) => { dragFrom = index; e.dataTransfer.effectAllowed = 'move'; } : undefined,
      ondragover: editing ? (e) => { e.preventDefault(); card.classList.add('drag-over'); } : undefined,
      ondragleave: editing ? () => card.classList.remove('drag-over') : undefined,
      ondrop: editing ? (e) => {
        e.preventDefault(); card.classList.remove('drag-over');
        if (dragFrom === null || dragFrom === index) return;
        layout.widgets.splice(index, 0, layout.widgets.splice(dragFrom, 1)[0]); dragFrom = null; dirty = true; drawAll();
      } : undefined,
    }, h('div', { class: 'row between' }, h('div', { class: 'w-title' }, defaultTitle(w)), tools), body);
    grid.append(card);
    const handle = (RENDER[w.type] || renderNote)(w, body);
    return handle;
  }

  function drawAll() {
    drawHead();
    mount(grid);
    live = layout.widgets.map(widgetCard);
    if (!layout.widgets.length) grid.append(h('div', { class: 'card widget w12' }, empty(owner ? t('dash.empty_owner') : t('dash.empty'))));
  }

  const onSamples = (e) => {
    const changed = new Set((e.detail || []).map((x) => x.tag));
    for (const handle of live) {
      if (!handle.update || !handle.tags) continue;
      if (handle.raw) handle.update(e.detail.filter((x) => handle.tags.includes(x.tag)));
      else if (handle.tags.some((p) => changed.has(p))) handle.update();
    }
  };
  const onAlarms = () => live.forEach((hd) => hd.alarms && hd.update());
  const onStatus = () => live.forEach((hd) => hd.status && hd.update());
  const onResize = () => live.forEach((hd) => hd.resize?.());
  bus.addEventListener('samples', onSamples);
  bus.addEventListener('alarms', onAlarms);
  bus.addEventListener('status', onStatus);
  window.addEventListener('resize', onResize);
  const tick = setInterval(() => live.forEach((hd) => { if (hd.update && !hd.raw && !editing) hd.update(); }), 30_000);
  drawAll();

  return () => {
    bus.removeEventListener('samples', onSamples);
    bus.removeEventListener('alarms', onAlarms);
    bus.removeEventListener('status', onStatus);
    window.removeEventListener('resize', onResize);
    clearInterval(tick);
  };
}
