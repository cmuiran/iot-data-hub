// Alarm rules (limits per tag) and control policy (confirmation, rate, step, interlocks).

import { api } from '../api.js';
import { t, fmtNumber, fmtDuration } from '../i18n.js';
import {
  h, mount, table, button, tabs, modal, field, input, select, checkbox, notice, errorText, toast,
  confirmDialog, severityBadge, tagLabel, parseNumber, badge, valueText,
} from '../ui.js';
import { store, isNumeric } from '../store.js';

const SEVERITIES = ['low', 'medium', 'high', 'critical'];
const OPS = ['eq', 'ne', 'lt', 'le', 'gt', 'ge'];

const numOrNull = (text) => (String(text).trim() === '' ? null : parseNumber(text));
const tagName = (path) => { const tag = store.tags.get(path); return tag ? tagLabel(tag) : path; };

/** One readable message from a refused config change (the dialog shows it). */
function failure(err) {
  const parts = [errorText(err),
    ...(err.body?.issues || []).map((i) => i.message),
    ...(err.body?.errors || err.body?.details || []).map((e) => `${(e.loc || []).join('.')}: ${e.msg}`)];
  return new Error(parts.filter(Boolean).join(' — '));
}

function ruleDialog(rule, onSaved) {
  const r = rule ? structuredClone(rule) : { tag: [...store.tags.values()].find(isNumeric)?.tag, severity: 'medium', deadband: 0, delay_s: 0 };
  const tagSel = select([...store.tags.values()].map((tg) => ({ value: tg.tag, label: `${tagLabel(tg)} — ${tg.tag}` })), r.tag, { disabled: Boolean(rule) });
  const limits = {};
  const limitFields = h('div', { class: 'form-grid' });
  const equals = select([{ value: '', label: '—' }, { value: 'true', label: t('bool.true') }, { value: 'false', label: t('bool.false') }], r.equals === null || r.equals === undefined ? '' : String(r.equals));
  const drawLimits = () => {
    const tag = store.tags.get(tagSel.value);
    if (tag?.data_type === 'bool') { mount(limitFields, field(t('rules.equals'), equals, { hint: t('rules.equals_hint') })); return; }
    mount(limitFields, ['hihi', 'hi', 'lo', 'lolo'].map((k) => {
      limits[k] = input({ ltr: true, value: r[k] ?? '', inputmode: 'decimal' });
      return field(`${t(`limits.${k}`)}${tag?.unit ? ` (${tag.unit})` : ''}`, limits[k]);
    }));
  };
  tagSel.addEventListener('change', drawLimits);
  drawLimits();
  const deadband = input({ ltr: true, value: r.deadband ?? 0 });
  const delay = input({ ltr: true, value: r.delay_s ?? 0 });
  const severity = select(SEVERITIES.map((s) => ({ value: s, label: t(`severity.${s}`) })), r.severity);
  const message = input({ value: r.message || '', maxlength: 200, placeholder: t('rules.message_auto') });
  modal({
    title: rule ? t('rules.edit_rule') : t('rules.new_rule'), wide: true,
    body: h('div', { class: 'stack' }, field(t('rules.tag'), tagSel), limitFields,
      h('div', { class: 'form-grid' }, field(t('rules.deadband'), deadband, { hint: t('rules.deadband_hint') }), field(t('rules.delay'), delay, { hint: t('rules.delay_hint') }),
        field(t('rules.severity'), severity), field(t('rules.message'), message))),
    actions: [{ label: t('common.cancel') }, {
      label: t('common.save'), kind: 'primary',
      onClick: async () => {
        const tag = store.tags.get(tagSel.value);
        const body = { tag: tagSel.value, deadband: parseNumber(deadband.value || '0'), delay_s: parseNumber(delay.value || '0'), severity: severity.value, message: message.value.trim() || null };
        if (tag?.data_type === 'bool') body.equals = equals.value === '' ? null : equals.value === 'true';
        else for (const k of ['hihi', 'hi', 'lo', 'lolo']) body[k] = numOrNull(limits[k].value);
        if (Object.values(body).some((v) => typeof v === 'number' && Number.isNaN(v))) throw new Error(t('rules.bad_number'));
        try { await api('api/config/alarms/rules', { method: 'PUT', body }); } catch (err) { throw failure(err); }
        toast(t('cfg.applied'), 'ok');
        onSaved();
      },
    }],
  });
}

function policyDialog(path, current, onSaved) {
  const p = structuredClone(current);
  const confirm = checkbox(t('policy.confirm'), p.confirm);
  const interval = input({ ltr: true, value: p.min_interval_s ?? 2 });
  const step = input({ ltr: true, value: p.max_step ?? '' });
  const readback = input({ ltr: true, value: p.readback_timeout_s ?? 5 });
  const tagSel = path === undefined ? select([...store.tags.values()].filter((tg) => tg.access === 'read_write').map((tg) => ({ value: tg.tag, label: `${tagLabel(tg)} — ${tg.tag}` })), '') : null;
  const locks = (p.interlocks || []).map((l) => ({ ...l }));
  const lockBox = h('div', { class: 'stack tight' });
  const drawLocks = () => mount(lockBox, locks.map((l, i) => h('div', { class: 'row' },
    select([...store.tags.values()].map((tg) => ({ value: tg.tag, label: `${tagLabel(tg)} — ${tg.tag}` })), l.tag, { onchange: (e) => { l.tag = e.target.value; } }),
    select(OPS.map((o) => ({ value: o, label: t(`op.${o}`) })), l.op, { onchange: (e) => { l.op = e.target.value; } }),
    input({ ltr: true, value: String(l.value), style: { width: '90px' }, oninput: (e) => { const txt = e.target.value.trim(); l.value = txt === 'true' ? true : txt === 'false' ? false : Number.isNaN(parseNumber(txt)) ? txt : parseNumber(txt); } }),
    input({ value: l.message || '', placeholder: t('policy.lock_message'), oninput: (e) => { l.message = e.target.value; } }),
    button('', () => { locks.splice(i, 1); drawLocks(); }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.remove') }))),
  button(t('policy.add_interlock'), () => { locks.push({ tag: [...store.tags.keys()][0], op: 'eq', value: true, max_age_s: 10, message: '' }); drawLocks(); }, { size: 'sm', iconName: 'plus' }));
  drawLocks();
  modal({
    title: path === null ? t('policy.defaults') : path ? t('policy.for_tag', { tag: tagName(path) }) : t('policy.new_override'), wide: true,
    body: h('div', { class: 'stack' },
      tagSel ? field(t('rules.tag'), tagSel) : null,
      confirm,
      h('div', { class: 'form-grid' }, field(t('policy.min_interval'), interval, { hint: t('policy.seconds') }), field(t('policy.max_step'), step, { hint: t('policy.max_step_hint') }),
        field(t('policy.readback_timeout'), readback, { hint: t('policy.seconds') })),
      h('h3', null, t('policy.interlocks')), h('p', { class: 'tiny faint' }, t('policy.interlocks_hint')), lockBox),
    actions: [{ label: t('common.cancel') }, {
      label: t('common.save'), kind: 'primary',
      onClick: async () => {
        const body = { confirm: confirm.input.checked, min_interval_s: parseNumber(interval.value || '0'), readback_timeout_s: parseNumber(readback.value || '5'), interlocks: locks };
        if (step.value.trim()) body.max_step = parseNumber(step.value);
        if (locks.some((l) => !l.message?.trim())) throw new Error(t('policy.lock_message_required'));
        const target = path === undefined ? tagSel.value : path;
        const url = target === null ? 'api/config/control/defaults' : `api/config/control/tags/${encodeURIComponent(target)}`;
        try { await api(url, { method: 'PUT', body }); } catch (err) { throw failure(err); }
        toast(t('cfg.applied'), 'ok');
        onSaved();
      },
    }],
  });
}

export async function render(page, { setTitle }) {
  setTitle(t('rules.title'));
  let tab = 'alarms';
  const holder = h('div', { class: 'stack' });
  const tabBar = tabs([{ id: 'alarms', label: t('rules.tab_alarms') }, { id: 'control', label: t('rules.tab_control') }], tab, (id) => { tab = id; draw(); });

  async function draw() {
    try {
      if (tab === 'alarms') {
        const cfg = await api('api/config/alarms');
        const limitText = (r) => (r.equals !== null && r.equals !== undefined ? `= ${valueText(store.tags.get(r.tag), r.equals)}`
          : ['lolo', 'lo', 'hi', 'hihi'].filter((k) => r[k] !== null && r[k] !== undefined).map((k) => `${t(`limits.${k}`)} ${fmtNumber(r[k])}`).join(' · '));
        mount(holder,
          h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, t('rules.offline_title'))),
            h('p', { class: 'small' }, cfg.offline_alarm ? t('rules.offline_on', { sev: t(`severity.${cfg.offline_severity}`), delay: fmtDuration(cfg.offline_delay_s) }) : t('rules.offline_off')),
            h('p', { class: 'tiny faint' }, t('rules.offline_hint'))),
          h('div', { class: 'card flush' },
            h('div', { class: 'card-head' }, h('h2', null, t('rules.rules_title')), button(t('rules.new_rule'), () => ruleDialog(null, draw), { kind: 'primary', size: 'sm', iconName: 'plus' })),
            table([
              { label: t('rules.tag'), render: (r) => h('div', null, h('div', null, tagName(r.tag)), h('div', { class: 'tagpath' }, r.tag)) },
              { label: t('rules.limits'), render: (r) => h('span', { class: 'small' }, limitText(r)) },
              { label: t('rules.severity'), render: (r) => severityBadge(r.severity) },
              { label: t('rules.deadband'), render: (r) => fmtNumber(r.deadband) },
              { label: t('rules.delay'), render: (r) => fmtDuration(r.delay_s) },
              { label: t('rules.message'), render: (r) => r.message || h('span', { class: 'faint' }, t('rules.message_auto')) },
              { label: '', cls: 'actions', render: (r) => h('div', { class: 'row end' },
                button('', () => ruleDialog(r, draw), { kind: 'ghost', size: 'sm', iconName: 'edit', title: t('common.edit') }),
                button('', async () => {
                  if (!(await confirmDialog(t('rules.delete'), t('rules.delete_text', { tag: tagName(r.tag) }), { danger: true }))) return;
                  try { await api(`api/config/alarms/rules/${encodeURIComponent(r.tag)}`, { method: 'DELETE' }); draw(); } catch (err) { toast(errorText(err), 'bad'); }
                }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.delete') })) },
            ], cfg.rules, { emptyText: t('rules.none') })));
      } else {
        const cfg = await api('api/config/control');
        const desc = (p) => h('div', { class: 'row' },
          p.confirm ? badge(t('policy.confirm_short'), 'sev-medium') : null,
          badge(t('policy.interval_short', { s: fmtNumber(p.min_interval_s ?? cfg.defaults.min_interval_s) }), 'accent'),
          p.max_step ? badge(t('policy.step_short', { s: fmtNumber(p.max_step) }), 'accent') : null,
          p.interlocks?.length ? badge(t('policy.locks_short', { n: p.interlocks.length }), 'rw') : null);
        const rows = Object.entries(cfg.tags).map(([path, pol]) => ({ path, pol: { ...cfg.defaults, ...pol } }));
        mount(holder,
          store.info?.control_enabled ? null : notice(t('control.disabled_hub_banner'), 'warn'),
          h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, t('policy.defaults')),
            button(t('common.edit'), () => policyDialog(null, cfg.defaults, draw), { size: 'sm', iconName: 'edit' })), desc(cfg.defaults),
            h('p', { class: 'tiny faint' }, t('policy.defaults_hint'))),
          h('div', { class: 'card flush' },
            h('div', { class: 'card-head' }, h('h2', null, t('policy.overrides')), button(t('policy.new_override'), () => policyDialog(undefined, cfg.defaults, draw), { kind: 'primary', size: 'sm', iconName: 'plus' })),
            table([
              { label: t('rules.tag'), render: (r) => h('div', null, h('div', null, tagName(r.path)), h('div', { class: 'tagpath' }, r.path)) },
              { label: t('policy.settings'), render: (r) => desc(r.pol) },
              { label: '', cls: 'actions', render: (r) => h('div', { class: 'row end' },
                button('', () => policyDialog(r.path, r.pol, draw), { kind: 'ghost', size: 'sm', iconName: 'edit', title: t('common.edit') }),
                button('', async () => {
                  if (!(await confirmDialog(t('policy.remove_override'), t('policy.remove_text', { tag: tagName(r.path) }), { danger: true }))) return;
                  try { await api(`api/config/control/tags/${encodeURIComponent(r.path)}`, { method: 'DELETE' }); draw(); } catch (err) { toast(errorText(err), 'bad'); }
                }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.delete') })) },
            ], rows, { emptyText: t('policy.no_overrides') })));
      }
    } catch (err) { mount(holder, notice(errorText(err), 'bad')); }
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('rules.title')), h('div', { class: 'sub' }, t('rules.subtitle')))),
    tabBar, holder);
  draw();
  return null;
}
