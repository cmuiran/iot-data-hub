// Calculated tags: list with live results, and an editor that previews a formula against the
// live values before it is published (publishing = saving the calc device through the
// validated configuration API; needs config.edit + calc.publish).

import { api } from '../api.js';
import { t, fmtDate } from '../i18n.js';
import {
  h, mount, table, button, badge, notice, errorText, empty, loading, valueNode, qualityBadge,
  modal, field, input, select, toast,
} from '../ui.js';
import { store, bus, can } from '../store.js';

const SEGMENT = /^[a-z][a-z0-9_]{0,63}$/;
const ALIAS = /^[a-z][a-z0-9_]{0,31}$/;

function qualityCell(row) {
  const live = store.tags.get(row.tag) || row;
  return h('span', { class: 'row tight' }, qualityBadge(live.quality),
    live.reason ? h('span', { class: 'tiny faint' }, t(`quality_reason.${live.reason}`)) : null);
}

export async function render(page, { setTitle }) {
  setTitle(t('calc.title'));
  mount(page, loading());
  const mayPublish = can('calc.publish') && can('config.edit');
  let rows;
  try {
    rows = await api('api/calc/tags');
  } catch (err) {
    mount(page, notice(errorText(err), 'bad'));
    return null;
  }
  const valueCells = new Map();
  const cols = [
    { label: t('calc.col_tag'), render: (r) => h('div', null, h('div', null, r.description || r.name), h('div', { class: 'tagpath' }, r.tag)) },
    { label: t('calc.col_formula'), render: (r) => h('code', { class: 'mono small ltr' }, r.formula) },
    { label: t('device.col_value'), cls: 'value', render: (r) => { const c = h('span'); valueCells.set(r.tag, c); fill(r); return c; } },
    { label: t('device.col_quality'), render: qualityCell },
    { label: t('calc.col_version'), render: (r) => h('span', { class: 'mono tiny' }, r.version) },
    { label: '', cls: 'actions', render: (r) => h('div', { class: 'row end' },
      button(t('calc.versions'), () => showVersions(r), { size: 'sm' }),
      mayPublish ? button(t('common.edit'), () => openEditor(r), { size: 'sm', iconName: 'edit' }) : null) },
  ];
  function fill(r) {
    const c = valueCells.get(r.tag);
    const live = store.tags.get(r.tag);
    if (c) mount(c, valueNode(live || { ...r, data_type: r.data_type }, (live || r).value));
  }
  mount(page,
    h('div', { class: 'row between' }, h('h1', null, t('calc.title')),
      mayPublish ? button(t('calc.new'), () => openEditor(null), { kind: 'primary', iconName: 'plus' }) : null),
    h('p', { class: 'small muted' }, t('calc.intro')),
    h('div', { class: 'card flush' }, rows.length ? table(cols, rows) : empty(t('calc.empty'))));

  const onSamples = (e) => {
    for (const sample of e.detail || []) {
      const r = rows.find((x) => x.tag === sample.tag);
      if (r) fill(r);
    }
  };
  bus.addEventListener('samples', onSamples);
  return () => bus.removeEventListener('samples', onSamples);

  async function showVersions(r) {
    let versions = [];
    try { versions = await api(`api/calc/tags/${encodeURIComponent(r.tag)}/versions`); } catch (err) { toast(errorText(err), 'bad'); return; }
    modal({
      title: t('calc.versions_of', { tag: r.name }),
      wide: true,
      body: table([
        { label: t('calc.valid_from'), render: (v) => fmtDate(v.valid_from, 'datetimesec') },
        { label: t('calc.col_version'), render: (v) => h('span', { class: 'mono tiny' }, v.version) },
        { label: t('calc.col_formula'), render: (v) => h('code', { class: 'mono small ltr' }, v.definition.formula) },
        { label: t('calc.unit'), render: (v) => v.definition.unit || '—' },
      ], versions, { emptyText: t('calc.no_versions') }),
    });
  }

  function openEditor(existing) {
    const [site0, area0, device0, name0] = existing ? existing.tag.split('.') : ['', '', '', ''];
    const site = input({ value: site0 || [...store.devices.values()][0]?.site || '', ltr: true });
    const area = input({ value: area0 || 'energy', ltr: true });
    const device = input({ value: device0 || 'calc_main', ltr: true });
    const name = input({ value: name0, ltr: true });
    const description = input({ value: existing?.description || '' });
    const formula = h('textarea', { class: 'input mono ltr', rows: 3, maxlength: 2000 });
    formula.value = existing?.formula || '';
    const unit = input({ value: existing?.unit || '', ltr: true, placeholder: 'kW, kWh, %, K, kWh/m3 …' });
    const dataType = select([{ value: 'float64', label: t('calc.number') }, { value: 'bool', label: t('calc.condition') }], existing?.data_type === 'bool' ? 'bool' : 'float64');
    const missing = select([{ value: 'bad', label: t('calc.missing_bad') }, { value: 'skip', label: t('calc.missing_skip') }], 'bad');
    const inputsBox = h('div', { class: 'stack tight' });
    const inputs = Object.entries(existing?.inputs || {}).map(([alias, path]) => ({ alias, path }));
    const result = h('div', { class: 'stack tight' });
    const tagOptions = [...store.tags.values()].filter((tg) => tg.data_type !== 'string')
      .map((tg) => ({ value: tg.tag, label: tg.tag }));

    function drawInputs() {
      mount(inputsBox, inputs.map((row, i) => {
        const aliasIn = input({ value: row.alias, ltr: true, placeholder: 'p1' });
        aliasIn.addEventListener('input', () => { row.alias = aliasIn.value.trim(); });
        const pathSel = select([{ value: '', label: '—' }, ...tagOptions], row.path);
        pathSel.addEventListener('change', () => { row.path = pathSel.value; });
        return h('div', { class: 'row' }, aliasIn, pathSel,
          button('', () => { inputs.splice(i, 1); drawInputs(); }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.delete') }));
      }), button(t('calc.add_input'), () => { inputs.push({ alias: `p${inputs.length + 1}`, path: '' }); drawInputs(); }, { size: 'sm', iconName: 'plus' }));
    }
    drawInputs();

    const definition = () => ({
      formula: formula.value.trim(),
      inputs: Object.fromEntries(inputs.filter((r) => r.alias && r.path).map((r) => [r.alias, r.path])),
      unit: unit.value.trim() || null,
      data_type: dataType.value,
      missing: missing.value,
    });

    async function runPreview() {
      const tagPath = [site.value, area.value, device.value, name.value].join('.');
      try {
        const res = await api('api/calc/preview', { method: 'POST', body: { ...definition(), tag: tagPath } });
        if (!res.valid) {
          mount(result, notice(`${t('calc.invalid')}: ${res.error}${res.error_position !== null && res.error_position !== undefined ? ` (${t('calc.at_char', { n: res.error_position + 1 })})` : ''}`, 'bad'));
          return false;
        }
        mount(result,
          h('div', { class: 'row' }, h('strong', null, t('calc.preview_result')), ' ',
            valueNode({ data_type: definition().data_type, unit: res.unit }, res.value), qualityBadge(res.quality),
            res.reason ? badge(t(`quality_reason.${res.reason}`), 'sev-medium') : null),
          res.warnings.map((w) => notice(w, 'warn')),
          table([
            { label: t('calc.col_tag'), render: (x) => h('span', { class: 'mono tiny' }, x.tag) },
            { label: t('device.col_value'), render: (x) => String(x.value ?? '—') },
            { label: t('device.col_quality'), render: (x) => qualityBadge(x.quality) },
          ], res.inputs));
        return true;
      } catch (err) {
        mount(result, notice(errorText(err), 'bad'));
        return false;
      }
    }

    async function publish() {
      const ids = [site.value, area.value, device.value, name.value];
      if (!ids.every((x) => SEGMENT.test(x))) { toast(t('calc.bad_ids'), 'bad'); return false; }
      if (!inputs.every((r) => !r.alias || ALIAS.test(r.alias))) { toast(t('calc.bad_alias'), 'bad'); return false; }
      if (!(await runPreview())) return false;
      const tag = { name: name.value, description: description.value.trim() || undefined, ...definition() };
      if (!tag.unit) delete tag.unit;
      let current = null;
      try { current = await api(`api/config/devices/${encodeURIComponent(device.value)}`); } catch { current = null; }
      try {
        if (current) {
          if (current.protocol !== 'calc') { toast(t('calc.not_calc_device'), 'bad'); return false; }
          const tags = current.tags.filter((x) => x.name !== tag.name).concat([tag]);
          await api(`api/config/devices/${encodeURIComponent(device.value)}`, { method: 'PUT', body: { ...current, tags } });
        } else {
          await api('api/config/devices', { method: 'POST', body: { device_id: device.value, name: device.value, site: site.value, area: area.value, protocol: 'calc', tags: [tag] } });
        }
      } catch (err) {
        const details = err.body?.issues?.map((i) => `${i.location || ''} ${i.message}`).join(' · ') || err.body?.errors?.map((e) => e.msg).join(' · ');
        mount(result, notice(details ? `${errorText(err)}: ${details}` : errorText(err), 'bad'));
        return false;
      }
      toast(t('calc.published'), 'ok');
      setTimeout(() => location.reload(), 300);
      return true;
    }

    modal({
      title: existing ? t('calc.edit_title', { tag: existing.name }) : t('calc.new'),
      wide: true,
      body: h('div', { class: 'stack' },
        h('div', { class: 'form-grid' },
          field(t('calc.site'), site), field(t('calc.area'), area), field(t('calc.device'), device), field(t('calc.name'), name),
          field(t('calc.description'), description, { wide: true }),
          field(t('calc.formula'), formula, { wide: true, hint: t('calc.formula_hint') }),
          field(t('calc.unit'), unit), field(t('calc.data_type'), dataType), field(t('calc.missing'), missing)),
        h('h3', null, t('calc.inputs')), inputsBox, result),
      actions: [
        { label: t('common.cancel') },
        { label: t('calc.preview'), onClick: async () => { await runPreview(); return false; } },
        { label: t('calc.publish'), kind: 'primary', onClick: publish },
      ],
    });
  }
}
