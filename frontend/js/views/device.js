// One device: connection state and every tag with live value, trend line and commands.

import { api } from '../api.js';
import { t, fmtDate, fmtAgo, fmtInteger } from '../i18n.js';
import {
  h, mount, stateBadge, protoBadge, qualityBadge, valueNode, tagLabel, button, badge, notice,
  errorText, areaName, siteName, table,
} from '../ui.js';
import { sparkline } from '../chart.js';
import { store, bus, canTag, isNumeric, deviceLastSeen } from '../store.js';
import { openWriteDialog } from '../control-dialog.js';

export async function render(page, { params, navigate, setTitle }) {
  const id = params.id;
  let device;
  try {
    device = await api(`api/devices/${encodeURIComponent(id)}`);
  } catch (err) {
    mount(page, notice(errorText(err), 'bad'));
    return null;
  }
  setTitle(device.name);
  const live = store.devices.get(id);
  if (live) Object.assign(device, { state: live.state, last_seen: live.last_seen, last_error: live.last_error });

  // seed the sparklines with the last 30 minutes from the history API
  const since = new Date(Date.now() - 30 * 60_000);
  await Promise.all(device.tags.filter(isNumeric).map(async (tag) => {
    if ((store.trail.get(tag.tag) || []).length > 5) return;
    try {
      const pts = await api(`api/tags/${encodeURIComponent(tag.tag)}/history`, { query: { start: since, max_points: 120 } });
      const trail = pts.map((p) => ({ t: Date.parse(p.ts), v: p.avg ?? p.value })).filter((p) => typeof p.v === 'number');
      const existing = store.trail.get(tag.tag) || [];
      store.trail.set(tag.tag, [...trail.filter((p) => !existing.length || p.t < existing[0].t), ...existing]);
    } catch { /* history is optional here */ }
  }));

  const head = h('div', { class: 'card' });
  const rows = new Map();

  function drawHead() {
    mount(head, h('div', { class: 'row between top' },
      h('div', { class: 'stack tight' },
        h('div', { class: 'row' }, h('h1', null, device.name), protoBadge(device.protocol), stateBadge(device.enabled ? device.state : 'disabled')),
        h('div', { class: 'small muted' }, `${siteName(device.site)} › ${areaName(device.area)}`, ' · ', h('span', { class: 'mono' }, device.device_id)),
        device.description ? h('p', { class: 'small' }, device.description) : null),
      h('dl', { class: 'kv' },
        h('dt', null, t('device.last_seen')), h('dd', { title: fmtDate(deviceLastSeen(id), 'datetimesec') }, fmtAgo(deviceLastSeen(id) || device.last_seen)),
        h('dt', null, t('device.tags')), h('dd', null, fmtInteger(device.tags.length)))),
    device.last_error && device.state !== 'online' ? notice(`${t('device.last_error')}: ${device.last_error}`, 'bad') : null);
  }

  const cols = [
    { label: t('device.col_tag'), render: (tag) => h('div', null, h('div', null, tagLabel(tag)), h('div', { class: 'tagpath' }, tag.tag)) },
    { label: t('device.col_value'), cls: 'value', render: (tag) => { const c = h('span'); rows.set(tag.tag, { value: c }); fillValue(tag); return c; } },
    { label: t('device.col_trend'), render: (tag) => { const c = h('span'); rows.get(tag.tag).spark = c; fillSpark(tag); return c; } },
    { label: t('device.col_quality'), render: (tag) => { const c = h('span'); rows.get(tag.tag).quality = c; fillQuality(tag); return c; } },
    { label: t('device.col_time'), render: (tag) => { const c = h('span', { class: 'tiny faint num' }); rows.get(tag.tag).time = c; fillTime(tag); return c; } },
    { label: t('device.col_access'), render: (tag) => (tag.access === 'read_write' ? badge(t('access.read_write'), 'rw') : badge(t('access.read'), 'unknown')) },
    { label: '', cls: 'actions', render: (tag) => h('div', { class: 'row end' },
      isNumeric(tag) || tag.data_type === 'bool' ? button(t('device.trend'), () => navigate(`trends?tag=${encodeURIComponent(tag.tag)}`), { size: 'sm', iconName: 'trend' }) : null,
      tag.access === 'read_write' && canTag('tag.write', tag.tag) ? button(t('device.command'), () => openWriteDialog(tag.tag), { size: 'sm', kind: 'primary', iconName: 'control' }) : null) },
  ];

  const current = (tag) => store.tags.get(tag.tag) || tag;
  function fillValue(tag) { const c = rows.get(tag.tag)?.value; if (c) { mount(c, valueNode(tag, current(tag).value)); c.classList.remove('flash-val'); void c.offsetWidth; c.classList.add('flash-val'); } }
  function fillSpark(tag) { const c = rows.get(tag.tag)?.spark; if (c && isNumeric(tag)) mount(c, sparkline(store.trail.get(tag.tag) || [])); }
  function fillQuality(tag) { const c = rows.get(tag.tag)?.quality; if (c) mount(c, qualityBadge(current(tag).quality)); }
  function fillTime(tag) { const c = rows.get(tag.tag)?.time; if (c) c.textContent = fmtDate(current(tag).ts, 'timesec'); }

  drawHead();
  mount(page,
    h('div', { class: 'row' }, h('a', { href: '#/overview' }, `‹ ${t('nav.overview')}`)),
    head,
    h('div', { class: 'card flush' }, table(cols, device.tags, { emptyText: t('overview.no_tags') })));

  const byPath = new Map(device.tags.map((tag) => [tag.tag, tag]));
  const onSamples = (e) => {
    for (const sample of e.detail || []) {
      const tag = byPath.get(sample.tag);
      if (!tag) continue;
      fillValue(tag); fillSpark(tag); fillQuality(tag); fillTime(tag);
    }
  };
  const onStatus = () => {
    const d = store.devices.get(id);
    if (d) { Object.assign(device, { state: d.state, last_seen: d.last_seen, last_error: d.last_error }); drawHead(); }
  };
  bus.addEventListener('samples', onSamples);
  bus.addEventListener('status', onStatus);
  const tick = setInterval(drawHead, 15_000);
  return () => {
    bus.removeEventListener('samples', onSamples);
    bus.removeEventListener('status', onStatus);
    clearInterval(tick);
  };
}
