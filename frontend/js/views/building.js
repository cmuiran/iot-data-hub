// Building model: tree of campus › building › floor › zone › room, the equipment in each place,
// one equipment with its live points, documents and maintenance history.

import { api } from '../api.js';
import { t, fmtDate, fmtAgo } from '../i18n.js';
import {
  h, mount, table, button, badge, notice, errorText, empty, loading, valueNode, qualityBadge,
  modal, field, input, select, dateTimeInput, toast,
} from '../ui.js';
import { store, bus, can, canWriteTag } from '../store.js';
import { openWriteDialog } from '../control-dialog.js';

function statusBadge(status) {
  const cls = { in_service: 'online', maintenance: 'sev-medium', out_of_service: 'offline', retired: 'unknown' }[status] || 'unknown';
  return badge(t(`equipment_status.${status}`), cls, true);
}

function qualityCell(point) {
  const live = store.tags.get(point.tag) || point;
  const q = qualityBadge(live.quality);
  const reason = live.reason || point.reason;
  return h('span', { class: 'row tight' }, q, reason ? h('span', { class: 'tiny faint' }, t(`quality_reason.${reason}`)) : null);
}

function safeLink(doc) {
  // only http(s) links become anchors; store paths are shown as text
  return /^https?:\/\//.test(doc.ref)
    ? h('a', { href: doc.ref, target: '_blank', rel: 'noopener noreferrer' }, doc.title)
    : h('span', null, doc.title, ' — ', h('span', { class: 'mono tiny' }, doc.ref));
}

export async function render(page, { query, navigate, setTitle }) {
  setTitle(t('building.title'));
  mount(page, loading());
  let locations;
  try {
    locations = await api('api/model/locations');
  } catch (err) {
    mount(page, notice(errorText(err), 'bad'));
    return null;
  }
  if (!locations.length) {
    mount(page, h('div', { class: 'card' }, h('h1', null, t('building.title')), empty(t('building.empty'))));
    return null;
  }
  const byParent = new Map();
  for (const loc of locations) {
    const key = loc.parent || '';
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(loc);
  }
  const cleanupFns = [];
  const selectedLoc = query.get('loc') || locations[0].id;
  const selectedEq = query.get('eq');

  function treeNode(loc) {
    const kids = byParent.get(loc.id) || [];
    return h('li', null,
      h('a', { href: `#/building?loc=${encodeURIComponent(loc.id)}`, class: ['tree-item', loc.id === selectedLoc && !selectedEq && 'active'] },
        h('span', { class: 'tiny faint' }, t(`location_kind.${loc.kind}`)), ' ', loc.name,
        loc.equipment ? h('span', { class: 'tiny faint' }, ` (${loc.equipment})`) : null),
      kids.length ? h('ul', { class: 'tree' }, kids.map(treeNode)) : null);
  }
  const roots = (byParent.get('') || []).concat(locations.filter((l) => l.parent && !locations.some((p) => p.id === l.parent)));
  const tree = h('nav', { class: 'card', 'aria-label': t('building.tree') },
    h('h2', null, t('building.tree')), h('ul', { class: 'tree' }, roots.map(treeNode)));
  const detail = h('div', { class: 'stack' }, loading());
  mount(page, h('div', { class: 'split' }, tree, detail));

  if (selectedEq) await showEquipment(selectedEq);
  else await showLocation(selectedLoc);

  async function showLocation(id) {
    let data;
    try {
      data = await api(`api/model/locations/${encodeURIComponent(id)}`);
    } catch (err) {
      mount(detail, notice(errorText(err), 'bad'));
      return;
    }
    const loc = data.location;
    const cols = [
      { label: t('building.col_equipment'), render: (e) => h('a', { href: `#/building?eq=${encodeURIComponent(e.id)}` }, e.name) },
      { label: t('building.col_type'), render: (e) => e.type },
      { label: t('building.col_place'), render: (e) => h('span', { class: 'small muted' }, e.location_path.slice(1).join(' › ')) },
      { label: t('building.col_status'), render: (e) => statusBadge(e.status) },
    ];
    mount(detail,
      h('div', { class: 'card' },
        h('div', { class: 'small muted' }, data.path.map((p) => p.name).join(' › ')),
        h('h1', null, loc.name, ' ', badge(t(`location_kind.${loc.kind}`), 'accent')),
        h('dl', { class: 'kv' },
          loc.site ? [h('dt', null, t('building.site')), h('dd', { class: 'mono' }, loc.site)] : null,
          loc.organization ? [h('dt', null, t('building.organization')), h('dd', { class: 'mono' }, loc.organization)] : null,
          loc.level !== null && loc.level !== undefined ? [h('dt', null, t('building.level')), h('dd', null, String(loc.level))] : null,
          loc.area_m2 ? [h('dt', null, t('building.area_m2')), h('dd', null, String(loc.area_m2))] : null,
          loc.brick_class ? [h('dt', null, 'Brick'), h('dd', { class: 'mono' }, loc.brick_class)] : null),
        loc.description ? h('p', { class: 'small' }, loc.description) : null,
        data.children.length ? h('div', { class: 'row' }, data.children.map((c) => button(c.name, () => navigate(`building?loc=${encodeURIComponent(c.id)}`), { size: 'sm' }))) : null),
      h('div', { class: 'card flush' }, table(cols, data.equipment, {
        emptyText: t('building.no_equipment'),
        onRow: (e) => navigate(`building?eq=${encodeURIComponent(e.id)}`),
      })));
  }

  async function showEquipment(id) {
    let eq;
    let records = [];
    try {
      [eq, records] = await Promise.all([
        api(`api/model/equipment/${encodeURIComponent(id)}`),
        api(`api/model/equipment/${encodeURIComponent(id)}/maintenance`),
      ]);
    } catch (err) {
      mount(detail, notice(errorText(err), 'bad'));
      return;
    }
    setTitle(eq.name);
    const valueCells = new Map();
    const cols = [
      { label: t('building.col_role'), render: (p) => h('div', null, h('div', null, p.role), h('div', { class: 'tagpath' }, p.tag)) },
      { label: t('device.col_value'), cls: 'value', render: (p) => { const c = h('span'); valueCells.set(p.tag, c); fillValue(p); return c; } },
      { label: t('device.col_quality'), render: (p) => qualityCell(p) },
      { label: t('building.col_received'), render: (p) => h('span', { class: 'tiny faint num', title: fmtDate(p.ts, 'datetimesec') }, fmtAgo((store.tags.get(p.tag) || p).received_at || p.received_at)) },
      { label: t('device.col_access'), render: (p) => (p.access === 'read_write'
        ? badge(t(`write_class.${p.write_class || 'command'}`), 'rw') : badge(t('access.read'), 'unknown')) },
      { label: '', cls: 'actions', render: (p) => {
        const tag = store.tags.get(p.tag);
        return tag && tag.access === 'read_write' && canWriteTag(tag)
          ? button(t('device.command'), () => openWriteDialog(p.tag), { size: 'sm', kind: 'primary', iconName: 'control' }) : null;
      } },
    ];
    function fillValue(p) {
      const c = valueCells.get(p.tag);
      const live = store.tags.get(p.tag);
      if (c) mount(c, valueNode(live || { ...p, unit: p.unit }, (live || p).value));
    }
    const recordCols = [
      { label: t('building.performed_at'), render: (r) => fmtDate(r.performed_at, 'datetime') },
      { label: t('building.kind'), render: (r) => t(`maintenance_kind.${r.kind}`) },
      { label: t('building.description'), render: (r) => r.description },
      { label: t('building.performed_by'), render: (r) => r.performed_by || '—' },
      { label: t('building.work_order'), render: (r) => r.work_order || '—' },
      { label: t('building.entered_by'), render: (r) => h('span', { class: 'small muted' }, r.created_by_name || '') },
    ];
    const canLog = eq.points.some((p) => can('maintenance.write', ...p.tag.split('.').slice(0, 2))) || (!eq.points.length && can('maintenance.write'));
    mount(detail,
      h('div', { class: 'card' },
        h('div', { class: 'small muted' }, h('a', { href: `#/building?loc=${encodeURIComponent(eq.location)}` }, eq.location_path.join(' › '))),
        h('div', { class: 'row between top' },
          h('h1', null, eq.name, ' ', statusBadge(eq.status)),
          eq.device ? button(t('building.open_device'), () => navigate(`device/${encodeURIComponent(eq.device)}`), { size: 'sm', iconName: 'devices' }) : null),
        h('dl', { class: 'kv' },
          h('dt', null, t('building.col_type')), h('dd', null, eq.type),
          eq.manufacturer ? [h('dt', null, t('building.manufacturer')), h('dd', null, eq.manufacturer)] : null,
          eq.model ? [h('dt', null, t('building.model')), h('dd', null, eq.model)] : null,
          eq.serial_number ? [h('dt', null, t('building.serial_number')), h('dd', { class: 'mono' }, eq.serial_number)] : null,
          eq.installed_on ? [h('dt', null, t('building.installed_on')), h('dd', null, fmtDate(eq.installed_on, 'date'))] : null,
          eq.warranty_until ? [h('dt', null, t('building.warranty_until')), h('dd', null, fmtDate(eq.warranty_until, 'date'))] : null,
          eq.system ? [h('dt', null, t('building.system')), h('dd', { class: 'mono' }, eq.system)] : null,
          eq.brick_class ? [h('dt', null, 'Brick'), h('dd', { class: 'mono' }, eq.brick_class)] : null,
          eq.haystack.length ? [h('dt', null, 'Haystack'), h('dd', { class: 'mono' }, eq.haystack.join(' '))] : null),
        eq.description ? h('p', { class: 'small' }, eq.description) : null,
        eq.documents.length ? h('div', { class: 'stack tight' }, h('h3', null, t('building.documents')), eq.documents.map((d) => h('div', null, safeLink(d)))) : null),
      h('div', { class: 'card flush' }, h('h2', { class: 'pad' }, t('building.points')),
        table(cols, eq.points, { emptyText: t('building.no_points') })),
      h('div', { class: 'card flush' },
        h('div', { class: 'row between pad' }, h('h2', null, t('building.maintenance')),
          canLog ? button(t('building.add_record'), () => addRecord(eq), { size: 'sm', kind: 'primary', iconName: 'plus' }) : null),
        table(recordCols, records, { emptyText: t('building.no_records') })));

    const onSamples = (e) => {
      for (const sample of e.detail || []) {
        const p = eq.points.find((x) => x.tag === sample.tag);
        if (p) fillValue(p);
      }
    };
    bus.addEventListener('samples', onSamples);
    cleanupFns.push(() => bus.removeEventListener('samples', onSamples));
  }

  function addRecord(eq) {
    const when = dateTimeInput(new Date(), { withTime: true });
    const kind = select(['inspection', 'preventive', 'corrective', 'calibration', 'replacement', 'note']
      .map((k) => ({ value: k, label: t(`maintenance_kind.${k}`) })), 'inspection');
    const text = h('textarea', { class: 'input', rows: 4, maxlength: 2000 });
    const by = input({ maxlength: 200 });
    const wo = input({ maxlength: 100, ltr: true });
    modal({
      title: t('building.add_record'),
      body: h('div', { class: 'form-grid' },
        field(t('building.performed_at'), when), field(t('building.kind'), kind),
        field(t('building.description'), text, { wide: true }),
        field(t('building.performed_by'), by), field(t('building.work_order'), wo)),
      actions: [{ label: t('common.cancel') }, {
        label: t('common.save'), kind: 'primary', onClick: async () => {
          const performed = when.getDate();
          if (!performed || !text.value.trim()) { toast(t('building.record_incomplete'), 'bad'); return false; }
          try {
            await api(`api/model/equipment/${encodeURIComponent(eq.id)}/maintenance`, {
              method: 'POST',
              body: { performed_at: performed, kind: kind.value, description: text.value.trim(), performed_by: by.value.trim() || null, work_order: wo.value.trim() || null },
            });
            toast(t('building.record_saved'), 'ok');
            await showEquipment(eq.id);
          } catch (err) {
            toast(errorText(err), 'bad');
            return false;
          }
          return true;
        },
      }],
    });
  }

  return () => cleanupFns.forEach((fn) => fn());
}
