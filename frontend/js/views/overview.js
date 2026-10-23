// Plant overview: summary, then sites -> areas -> device cards with live values.

import { t, fmtAgo, fmtInteger } from '../i18n.js';
import {
  h, mount, input, stateBadge, protoBadge, valueNode, tagLabel, empty, button, siteName, areaName,
} from '../ui.js';
import { store, bus, siteTree, tagsOfDevice, unackedAlarms, loadCatalogue, deviceLastSeen } from '../store.js';

const PREVIEW_TAGS = 4;

export async function render(page, { navigate, setTitle }) {
  setTitle(t('overview.title'));
  let filter = '';
  const kpis = h('div', { class: 'kpis' });
  const body = h('div', { class: 'stack loose' });
  const search = input({ type: 'search', placeholder: t('overview.search'), 'aria-label': t('overview.search') });
  search.addEventListener('input', () => { filter = search.value.trim().toLowerCase(); drawDevices(); });
  const valueCells = new Map();   // tag path -> element

  mount(page,
    h('div', { class: 'page-head' },
      h('div', null, h('h1', null, t('overview.title')), h('div', { class: 'sub' }, t('overview.subtitle'))),
      h('div', { class: 'row' }, search, button('', async () => { await loadCatalogue(); drawAll(); }, { iconName: 'refresh', title: t('common.refresh') }))),
    kpis, body);

  function drawKpis() {
    const devices = [...store.devices.values()];
    const count = (st) => devices.filter((d) => d.state === st).length;
    const active = store.alarms.filter((a) => !a.cleared_at && !a.shelved);
    const critical = active.filter((a) => a.severity === 'critical' || a.severity === 'high').length;
    const kpi = (label, value, cls, onclick) => h('div', { class: ['kpi', cls], onclick, role: onclick ? 'link' : undefined },
      h('span', { class: 'label' }, label), h('span', { class: 'value' }, fmtInteger(value)));
    mount(kpis,
      kpi(t('overview.devices_total'), devices.length),
      kpi(t('overview.online'), count('online')),
      kpi(t('overview.offline'), count('offline') + count('unknown'), count('offline') ? 'warn' : ''),
      kpi(t('overview.error'), count('error'), count('error') ? 'bad' : ''),
      kpi(t('overview.active_alarms'), active.length, critical ? 'bad' : active.length ? 'warn' : '', () => navigate('alarms')),
      kpi(t('overview.unacked'), unackedAlarms().length, unackedAlarms().length ? 'bad' : '', () => navigate('alarms')));
  }

  function matches(d) {
    if (!filter) return true;
    const hay = [d.device_id, d.name, d.site, d.area, d.protocol, areaName(d.area), tagsOfDevice(d.device_id).map((x) => x.name).join(' ')].join(' ').toLowerCase();
    return hay.includes(filter);
  }

  function deviceCard(d) {
    const tags = tagsOfDevice(d.device_id).slice(0, PREVIEW_TAGS);
    const vals = h('div', { class: 'vals' }, tags.map((tag) => {
      const v = h('span', { class: ['v', tag.quality && tag.quality !== 'good' && 'stale'] }, valueNode(tag, tag.value));
      valueCells.set(tag.tag, v);
      return h('div', { class: 'val' }, h('span', { class: 'k', title: tag.tag }, tagLabel(tag)), v);
    }));
    return h('article', {
      class: ['card', 'device-card', d.state], tabindex: '0', role: 'link',
      onclick: () => navigate(`device/${encodeURIComponent(d.device_id)}`),
      onkeydown: (e) => { if (e.key === 'Enter') navigate(`device/${encodeURIComponent(d.device_id)}`); },
    },
    h('div', { class: 'row between' }, h('h3', null, d.name), protoBadge(d.protocol)),
    h('div', { class: 'row between' }, stateBadge(d.enabled ? d.state : 'disabled'),
      h('span', { class: 'tiny faint' }, deviceLastSeen(d.device_id) ? fmtAgo(deviceLastSeen(d.device_id)) : '')),
    d.last_error && d.state !== 'online' ? h('div', { class: 'err-text' }, d.last_error) : null,
    tags.length ? vals : h('span', { class: 'tiny faint' }, t('overview.no_tags')));
  }

  function drawDevices() {
    valueCells.clear();
    const tree = siteTree();
    if (!tree.size) { mount(body, empty(t('overview.no_devices'))); return; }
    const blocks = [];
    for (const [site, areas] of tree) {
      for (const [area, devices] of areas) {
        const shown = devices.filter(matches);
        if (!shown.length) continue;
        const bad = shown.filter((d) => d.state === 'error' || d.state === 'offline').length;
        blocks.push(h('section', { class: 'site-block' },
          h('div', { class: 'area-head' },
            h('h2', null, t('overview.area_title', { area: areaName(area), site: siteName(site) })),
            h('span', { class: 'tagpath' }, `${site}.${area}`),
            bad ? h('span', { class: 'badge error' }, t('overview.n_problem', { n: bad })) : null),
          h('div', { class: 'grid' }, shown.map(deviceCard))));
      }
    }
    mount(body, ...(blocks.length ? blocks : [empty(t('overview.no_match'))]));
  }

  function drawAll() { drawKpis(); drawDevices(); }

  let pending = new Set();
  let frame = 0;
  const onSamples = (e) => {
    for (const s of e.detail || []) pending.add(s.tag);
    if (!frame) frame = requestAnimationFrame(() => {
      frame = 0;
      for (const path of pending) {
        const cell = valueCells.get(path);
        const tag = store.tags.get(path);
        if (cell && tag) {
          mount(cell, valueNode(tag, tag.value));
          cell.classList.toggle('stale', Boolean(tag.quality && tag.quality !== 'good'));
        }
      }
      pending = new Set();
    });
  };
  const onStatus = () => drawAll();
  const onAlarms = () => drawKpis();
  bus.addEventListener('samples', onSamples);
  bus.addEventListener('status', onStatus);
  bus.addEventListener('alarms', onAlarms);
  bus.addEventListener('catalogue', onStatus);
  const ticker = setInterval(drawKpis, 30_000);
  drawAll();

  return () => {
    bus.removeEventListener('samples', onSamples);
    bus.removeEventListener('status', onStatus);
    bus.removeEventListener('alarms', onAlarms);
    bus.removeEventListener('catalogue', onStatus);
    clearInterval(ticker);
    cancelAnimationFrame(frame);
  };
}
