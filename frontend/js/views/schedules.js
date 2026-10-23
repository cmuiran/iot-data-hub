// Scheduled command sequences: cron timing, steps (write / delay / wait for a condition),
// manual runs and run history. Every write still goes through the control-safety layer.

import { api } from '../api.js';
import { t, fmtDate, fmtNumber } from '../i18n.js';
import {
  h, mount, table, button, badge, modal, field, input, select, checkbox, notice, errorText, toast,
  confirmDialog, tagLabel, parseNumber, empty,
} from '../ui.js';
import { store, can, canTag } from '../store.js';

const CRON_PRESETS = [
  ['0 6 * * *', 'cron_daily_6'], ['0 * * * *', 'cron_hourly'], ['*/15 * * * *', 'cron_15min'],
  ['0 7 * * 6,0,1,2,3', 'cron_workdays_7'], ['0 22 * * *', 'cron_daily_22'], ['0 0 1 * *', 'cron_monthly'],
];
const OPS = ['eq', 'ne', 'lt', 'le', 'gt', 'ge'];

function parseValue(tag, text) {
  if (tag?.data_type === 'bool') return text === 'true';
  return parseNumber(text);
}

function stepEditor(steps) {
  const box = h('div', { class: 'stack tight' });
  const writable = [...store.tags.values()].filter((tg) => tg.access === 'read_write' && canTag('tag.write', tg.tag));
  const readable = [...store.tags.values()];
  const draw = () => {
    mount(box, steps.map((st, i) => {
      const kind = st.write ? 'write' : st.wait ? 'wait' : 'delay';
      const kindSel = select(['write', 'delay', 'wait'].map((k) => ({ value: k, label: t(`sched.step_${k}`) })), kind, {
        onchange: (e) => {
          const k = e.target.value;
          steps[i] = k === 'write' ? { write: { tag: writable[0]?.tag, value: 0 } } : k === 'wait'
            ? { wait: { tag: readable[0]?.tag, op: 'ge', value: 0, timeout_s: 60 } } : { delay_s: 10 };
          draw();
        },
      });
      let detail;
      if (kind === 'write') {
        const tag = store.tags.get(st.write.tag);
        detail = h('div', { class: 'row' },
          select(writable.map((tg) => ({ value: tg.tag, label: `${tagLabel(tg)} — ${tg.tag}` })), st.write.tag, { onchange: (e) => { st.write.tag = e.target.value; draw(); } }),
          tag?.data_type === 'bool'
            ? select([{ value: 'true', label: t('bool.true') }, { value: 'false', label: t('bool.false') }], String(st.write.value), { onchange: (e) => { st.write.value = e.target.value === 'true'; } })
            : input({ ltr: true, value: String(st.write.value), style: { width: '110px' }, oninput: (e) => { st.write.value = parseValue(tag, e.target.value); } }));
      } else if (kind === 'wait') {
        const tag = store.tags.get(st.wait.tag);
        detail = h('div', { class: 'row' },
          select(readable.map((tg) => ({ value: tg.tag, label: `${tagLabel(tg)} — ${tg.tag}` })), st.wait.tag, { onchange: (e) => { st.wait.tag = e.target.value; draw(); } }),
          select(OPS.map((o) => ({ value: o, label: t(`op.${o}`) })), st.wait.op, { onchange: (e) => { st.wait.op = e.target.value; } }),
          input({ ltr: true, value: String(st.wait.value), style: { width: '90px' }, oninput: (e) => { st.wait.value = parseValue(tag, e.target.value); } }),
          h('span', { class: 'small' }, t('sched.timeout')),
          input({ ltr: true, value: String(st.wait.timeout_s), style: { width: '70px' }, oninput: (e) => { st.wait.timeout_s = parseNumber(e.target.value); } }));
      } else {
        detail = h('div', { class: 'row' }, input({ ltr: true, value: String(st.delay_s), style: { width: '90px' }, oninput: (e) => { st.delay_s = parseNumber(e.target.value); } }), h('span', { class: 'small' }, t('sched.seconds')));
      }
      return h('div', { class: 'step-row' }, h('span', { class: 'n' }, fmtNumber(i + 1, 0)), kindSel, detail,
        h('div', { class: 'row' },
          button('', () => { if (i > 0) { steps.splice(i - 1, 0, steps.splice(i, 1)[0]); draw(); } }, { kind: 'ghost', size: 'sm', iconName: 'up', title: t('dash.move_earlier') }),
          button('', () => { steps.splice(i, 1); draw(); }, { kind: 'ghost', size: 'sm', iconName: 'trash', title: t('common.remove') })));
    }),
    button(t('sched.add_step'), () => { steps.push(writable.length ? { write: { tag: writable[0].tag, value: 0 } } : { delay_s: 10 }); draw(); }, { size: 'sm', iconName: 'plus' }));
  };
  draw();
  return box;
}

function editSchedule(existing, onSaved) {
  const sch = existing ? structuredClone(existing) : { name: '', description: '', cron: '0 6 * * *', timezone: store.info?.timezone || 'Asia/Tehran', enabled: true, steps: [] };
  const name = input({ value: sch.name, maxlength: 100 });
  const desc = input({ value: sch.description || '', maxlength: 500 });
  const cron = input({ ltr: true, value: sch.cron });
  const presets = select([{ value: '', label: t('sched.cron_presets') }, ...CRON_PRESETS.map(([v, k]) => ({ value: v, label: `${t(`sched.${k}`)} — ${v}` }))], '', { onchange: (e) => { if (e.target.value) cron.value = e.target.value; } });
  const enabled = checkbox(t('sched.enabled'), sch.enabled);
  const steps = sch.steps.map((s) => structuredClone(s));
  modal({
    title: existing ? t('sched.edit') : t('sched.new'), wide: true,
    body: h('div', { class: 'stack' },
      h('div', { class: 'form-grid' }, field(t('sched.name'), name), field(t('sched.description'), desc),
        field(t('sched.cron'), cron, { hint: t('sched.cron_hint') }), field(t('sched.preset'), presets)),
      enabled,
      notice(t('sched.safety_note')),
      h('h3', null, t('sched.steps')), stepEditor(steps)),
    actions: [{ label: t('common.cancel') }, {
      label: t('common.save'), kind: 'primary',
      onClick: async () => {
        if (!steps.length) throw new Error(t('sched.need_step'));
        const bad = steps.some((s) => (s.write && Number.isNaN(s.write.value)) || (s.wait && (Number.isNaN(s.wait.value) || Number.isNaN(s.wait.timeout_s))) || (s.delay_s !== undefined && Number.isNaN(s.delay_s)));
        if (bad) throw new Error(t('sched.bad_number'));
        const body = { name: name.value.trim(), description: desc.value.trim() || null, cron: cron.value.trim(), timezone: sch.timezone, enabled: enabled.input.checked, steps };
        if (existing) await api(`api/schedules/${existing.schedule_id}`, { method: 'PUT', body });
        else await api('api/schedules', { method: 'POST', body });
        toast(t('sched.saved'), 'ok');
        onSaved();
      },
    }],
  });
}

function stepSummary(st) {
  if (st.write) { const tag = store.tags.get(st.write.tag); return t('sched.sum_write', { tag: tag ? tagLabel(tag) : st.write.tag, value: String(st.write.value) }); }
  if (st.wait) { const tag = store.tags.get(st.wait.tag); return t('sched.sum_wait', { tag: tag ? tagLabel(tag) : st.wait.tag, op: t(`op.${st.wait.op}`), value: String(st.wait.value) }); }
  return t('sched.sum_delay', { s: st.delay_s });
}

async function showRuns(sch) {
  const body = h('div', null, empty(t('common.loading')));
  modal({ title: t('sched.runs_of', { name: sch.name }), body, wide: true });
  try {
    const runs = await api(`api/schedules/${sch.schedule_id}/runs`);
    mount(body, table([
      { label: t('sched.started'), render: (r) => h('span', { class: 'small num' }, fmtDate(r.started_at, 'datetimesec')) },
      { label: t('sched.trigger'), render: (r) => t(`sched.trigger_${r.trigger}`) },
      { label: t('sched.status'), render: (r) => badge(t(`sched.run_${r.status}`), r.status === 'ok' ? 'online' : r.status === 'failed' ? 'error' : 'uncertain') },
      { label: t('sched.detail'), render: (r) => h('pre', { class: 'pre' }, JSON.stringify(r.detail, null, 1)) },
    ], runs, { emptyText: t('sched.no_runs') }));
  } catch (err) { mount(body, notice(errorText(err), 'bad')); }
}

export async function render(page, { setTitle }) {
  setTitle(t('sched.title'));
  const box = h('div', { class: 'card flush' });
  const mayEdit = (s) => s.owner_id === store.me.user_id || can('config.edit');

  async function load() {
    try {
      const list = await api('api/schedules');
      mount(box, table([
        { label: t('sched.name'), render: (s) => h('div', null, h('div', null, s.name), s.description ? h('div', { class: 'tiny faint' }, s.description) : null) },
        { label: t('sched.cron'), render: (s) => h('span', { class: 'mono small' }, s.cron) },
        { label: t('sched.steps'), render: (s) => h('ol', { class: 'tiny stack tight' }, s.steps.map((st) => h('li', null, stepSummary(st)))) },
        { label: t('sched.next_run'), render: (s) => (s.enabled ? h('span', { class: 'small num' }, fmtDate(s.next_run_at)) : badge(t('sched.disabled'), 'disabled')) },
        { label: t('sched.last_run'), render: (s) => h('span', { class: 'small num' }, fmtDate(s.last_run_at)) },
        { label: t('dash.owner'), render: (s) => s.owner },
        { label: '', cls: 'actions', render: (s) => h('div', { class: 'row end' },
          button(t('sched.history'), () => showRuns(s), { size: 'sm' }),
          mayEdit(s) ? button(t('sched.run_now'), async () => {
            if (!(await confirmDialog(t('sched.run_now'), t('sched.run_now_text', { name: s.name }), { danger: true, confirmLabel: t('sched.run_now') }))) return;
            try { await api(`api/schedules/${s.schedule_id}/run`, { method: 'POST' }); toast(t('sched.started_ok'), 'ok'); } catch (err) { toast(errorText(err), 'bad'); }
          }, { size: 'sm', iconName: 'play' }) : null,
          mayEdit(s) ? button('', () => editSchedule(s, load), { size: 'sm', kind: 'ghost', iconName: 'edit', title: t('common.edit') }) : null,
          mayEdit(s) ? button('', async () => {
            if (!(await confirmDialog(t('sched.delete'), t('sched.delete_text', { name: s.name }), { danger: true }))) return;
            try { await api(`api/schedules/${s.schedule_id}`, { method: 'DELETE' }); load(); } catch (err) { toast(errorText(err), 'bad'); }
          }, { size: 'sm', kind: 'ghost', iconName: 'trash', title: t('common.delete') }) : null) },
      ], list, { emptyText: t('sched.none') }));
    } catch (err) { mount(box, notice(errorText(err), 'bad')); }
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('sched.title')), h('div', { class: 'sub' }, t('sched.subtitle'))),
      button(t('sched.new'), () => editSchedule(null, load), { kind: 'primary', iconName: 'plus' })),
    store.info?.control_enabled ? null : notice(t('control.disabled_hub_banner'), 'warn'),
    box);
  load();
  return null;
}
