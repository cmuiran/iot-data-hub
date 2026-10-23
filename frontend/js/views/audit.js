// Audit trail: commands (write_audit), sign-in and administration events (auth_audit), and the
// hash-chain check of both tables.

import { api } from '../api.js';
import { t, tMaybe, fmtDate, fmtInteger } from '../i18n.js';
import { h, mount, table, button, tabs, badge, notice, errorText, select, input, empty } from '../ui.js';

const AUTH_EVENTS = ['login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
  'password_changed', 'password_expired', 'user_created', 'user_updated', 'user_disabled',
  'role_granted', 'role_revoked', 'session_revoked', 'session_expired', 'alarm_acked',
  'alarm_shelved', 'alarm_unshelved', 'write_confirmed', 'write_rejected', 'config_changed',
  'schedule_created', 'schedule_updated', 'schedule_deleted', 'opcua_login', 'mfa_enabled',
  'mfa_disabled', 'mfa_failed', 'mfa_reset', 'network_denied'];

const BAD_EVENTS = new Set(['login_failed', 'login_throttled', 'locked', 'mfa_failed', 'network_denied', 'user_disabled']);

export async function render(page, { setTitle }) {
  setTitle(t('audit.title'));
  let tab = 'writes';
  const body = h('div', { class: 'card flush' });
  const chain = h('div', { class: 'card' });
  const tabBar = tabs([{ id: 'writes', label: t('audit.tab_writes') }, { id: 'auth', label: t('audit.tab_auth') }], tab, (id) => { tab = id; draw(); });
  const eventSel = select([{ value: '', label: t('audit.all_events') }, ...AUTH_EVENTS.map((e) => ({ value: e, label: tMaybe(`auth_event.${e}`) || e }))], '', { onchange: () => draw() });
  const tagFilter = input({ ltr: true, placeholder: t('audit.filter_tag'), onchange: () => draw() });
  const filters = h('div', { class: 'row' });

  async function draw() {
    mount(filters, tab === 'auth' ? eventSel : tagFilter);
    mount(body, empty(t('common.loading')));
    try {
      if (tab === 'writes') {
        const rows = await api('api/audit/writes', { query: { limit: 500, tag: tagFilter.value.trim() || undefined } });
        mount(body, table([
          { label: t('audit.time'), render: (r) => h('span', { class: 'small num nowrap', title: fmtDate(r.ts, 'datetimesec') }, fmtDate(r.ts, 'datetimesec')) },
          { label: t('audit.user'), render: (r) => h('div', null, r.username, r.client_addr ? h('div', { class: 'tagpath' }, r.client_addr) : null) },
          { label: t('audit.tag'), render: (r) => h('span', { class: 'tagpath' }, r.tag_path) },
          { label: t('audit.phase'), render: (r) => tMaybe(`write_phase.${r.phase}`) || r.phase },
          { label: t('audit.change'), cls: 'value', render: (r) => `${r.previous_value ?? '—'} → ${r.requested_value}` },
          { label: t('audit.readback'), cls: 'value', render: (r) => r.readback_value ?? '' },
          { label: t('audit.result'), render: (r) => badge(tMaybe(`write_result.${r.result}`) || r.result, r.result === 'applied' ? 'online' : /rejected|failed|timeout/.test(r.result) ? 'error' : 'uncertain') },
          { label: t('audit.reason'), render: (r) => h('span', { class: 'small' }, r.reason || '') },
          { label: t('audit.request'), render: (r) => h('span', { class: 'tagpath' }, String(r.request_id).slice(0, 8)) },
        ], rows, { emptyText: t('audit.none') }));
      } else {
        const rows = await api('api/audit/auth', { query: { limit: 500, event: eventSel.value || undefined } });
        mount(body, table([
          { label: t('audit.time'), render: (r) => h('span', { class: 'small num nowrap' }, fmtDate(r.ts, 'datetimesec')) },
          { label: t('audit.event'), render: (r) => badge(tMaybe(`auth_event.${r.event}`) || r.event, BAD_EVENTS.has(r.event) ? 'error' : 'accent') },
          { label: t('audit.user'), render: (r) => r.username },
          { label: t('audit.address'), render: (r) => h('span', { class: 'tagpath' }, r.client_addr || '') },
          { label: t('audit.detail'), render: (r) => (Object.keys(r.detail || {}).length ? h('pre', { class: 'pre' }, JSON.stringify(r.detail, null, 1)) : '') },
        ], rows, { emptyText: t('audit.none') }));
      }
    } catch (err) { mount(body, notice(errorText(err), 'bad')); }
  }

  async function verify() {
    mount(chain, h('h2', null, t('audit.chain_title')), empty(t('common.loading')));
    try {
      const res = await api('api/audit/verify');
      mount(chain, h('div', { class: 'card-head' }, h('h2', null, t('audit.chain_title')), button(t('audit.verify_again'), verify, { size: 'sm', iconName: 'refresh' })),
        h('div', { class: 'stack' }, Object.values(res).map((r) => h('div', { class: 'stack tight' },
          h('div', { class: 'row' }, badge(r.ok ? t('audit.chain_ok') : t('audit.chain_broken'), r.ok ? 'online' : 'error'),
            h('strong', null, t(`audit.table_${r.table}`)), h('span', { class: 'small muted' }, t('audit.rows_checked', { n: fmtInteger(r.checked) }))),
          r.ok ? null : notice(t('audit.broken_detail', { id: r.first_bad_id, problem: r.problem }), 'bad'),
          r.head_hash ? h('div', { class: 'hash' }, `${t('audit.head')}: ${r.head_hash}`) : null))),
        h('p', { class: 'tiny faint' }, t('audit.chain_hint')));
    } catch (err) { mount(chain, notice(errorText(err), 'bad')); }
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('audit.title')), h('div', { class: 'sub' }, t('audit.subtitle')))),
    chain, h('div', { class: 'row between' }, tabBar, filters), body);
  verify();
  draw();
  return null;
}
