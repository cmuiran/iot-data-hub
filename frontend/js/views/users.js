// User management: accounts, role grants with site/area scope, password / MFA / lock / sessions.

import { api } from '../api.js';
import { t, fmtDate } from '../i18n.js';
import {
  h, mount, table, button, badge, modal, field, input, select, notice, errorText, toast,
  confirmDialog, siteName, areaName, empty,
} from '../ui.js';
import { store, siteTree, can } from '../store.js';

function scopeText(g) {
  if (g.organization) return t('users.scope_org', { org: g.organization });
  if (!g.site) return t('users.scope_all');
  return g.area ? `${siteName(g.site)} › ${areaName(g.area)}` : siteName(g.site);
}

function roleChip(g) {
  return badge(`${t(`roles.${g.role}`)} · ${scopeText(g)}`, g.role === 'admin' ? 'sev-high' : 'accent');
}

/** Role + scope pickers; returns {el, value()} */
function grantPicker(roles, organizations = []) {
  const tree = siteTree();
  const org = select([{ value: '', label: t('users.no_org') }, ...organizations.map((o) => ({ value: o.id, label: o.name }))], '');
  const role = select(roles.map((r) => ({ value: r, label: t(`roles.${r}`) })), roles.includes('viewer') ? 'viewer' : roles[0]);
  const site = select([{ value: '', label: t('users.scope_all') }, ...[...tree.keys()].map((s) => ({ value: s, label: siteName(s) }))], '');
  const area = select([{ value: '', label: t('users.all_areas') }], '');
  site.addEventListener('change', () => {
    const areas = site.value ? [...(tree.get(site.value)?.keys() || [])] : [];
    mount(area, h('option', { value: '' }, t('users.all_areas')), areas.map((a) => h('option', { value: a }, areaName(a))));
    area.disabled = !site.value;
  });
  area.disabled = true;
  org.addEventListener('change', () => { site.disabled = Boolean(org.value); if (org.value) { site.value = ''; area.value = ''; area.disabled = true; } });
  return {
    el: h('div', { class: 'form-grid' }, field(t('users.role'), role),
      organizations.length ? field(t('users.organization'), org) : null,
      field(t('users.site'), site), field(t('users.area'), area)),
    value: () => (org.value ? { role: role.value, organization: org.value, site: null, area: null }
      : { role: role.value, site: site.value || null, area: area.value || null }),
  };
}

function passwordHint() { return t('users.temp_password_hint', { n: store.info?.password_min_length || 10 }); }

export async function render(page, { setTitle }) {
  setTitle(t('users.title'));
  const box = h('div', { class: 'card flush' });
  let roles = {};
  let users = [];
  let organizations = [];

  async function load() {
    try {
      [roles, users] = await Promise.all([api('api/roles'), api('api/users')]);
      // organizations of the building model (readable with model.edit; otherwise none offered)
      if (can('model.edit')) {
        try { organizations = (await api('api/config/building')).organizations || []; } catch { organizations = []; }
      }
      draw();
    } catch (err) { mount(box, notice(errorText(err), 'bad')); }
  }

  function status(u) {
    if (!u.is_active) return badge(t('users.disabled'), 'disabled');
    if (u.locked_until && new Date(u.locked_until) > new Date()) return badge(t('users.locked'), 'error');
    if (u.must_change_password) return badge(t('users.must_change'), 'uncertain');
    if (u.auth_source === 'ldap') return badge(t('users.source_ldap'), 'online');
    return badge(t('users.active'), 'online');
  }

  function draw() {
    mount(box, table([
      { label: t('users.user'), render: (u) => h('div', null, h('div', null, u.display_name), h('div', { class: 'tagpath' }, u.username)) },
      { label: t('users.roles'), render: (u) => h('div', { class: 'row' }, u.roles.length ? u.roles.map(roleChip) : h('span', { class: 'tiny faint' }, t('users.no_roles'))) },
      { label: t('users.status'), render: status },
      { label: t('users.mfa'), render: (u) => (u.mfa_enabled ? badge(t('users.mfa_on'), 'online') : badge(t('users.mfa_off'), 'unknown')) },
      { label: t('users.last_login'), render: (u) => h('span', { class: 'small num' }, fmtDate(u.last_login_at)) },
    ], users, { onRow: (u) => detail(u) }));
  }

  function createUser() {
    const username = input({ ltr: true, autocomplete: 'off', placeholder: t('users.username_placeholder') });
    const display = input({ maxlength: 128 });
    const password = input({ ltr: true, type: 'password', autocomplete: 'new-password' });
    const lang = select([{ value: 'fa', label: t('language.fa') }, { value: 'en', label: t('language.en') }], 'fa');
    const grant = grantPicker(Object.keys(roles), organizations);
    modal({
      title: t('users.new'), wide: true,
      body: h('div', { class: 'stack' },
        h('div', { class: 'form-grid' },
          field(t('users.username'), username, { hint: t('users.username_hint') }), field(t('users.display_name'), display),
          field(t('users.temp_password'), password, { hint: passwordHint() }), field(t('users.language'), lang)),
        h('h3', null, t('users.first_role')), grant.el),
      actions: [{ label: t('common.cancel') }, {
        label: t('common.create'), kind: 'primary',
        onClick: async () => {
          await api('api/users', { method: 'POST', body: { username: username.value.trim(), display_name: display.value.trim(), password: password.value, language: lang.value, roles: [grant.value()] } });
          toast(t('users.created'), 'ok');
          load();
        },
      }],
    });
  }

  function detail(user) {
    const body = h('div', { class: 'stack' });
    const dlg = modal({ title: user.display_name, body, wide: true });
    const refresh = async () => {
      const u = await api(`api/users/${user.user_id}`);
      Object.assign(user, u);
      users = users.map((x) => (x.user_id === u.user_id ? u : x));
      draw();
      drawDetail();
    };
    const act = (fn, okText) => async () => {
      try { await fn(); if (okText) toast(okText, 'ok'); await refresh(); } catch (err) { dlg.setError(errorText(err)); }
    };
    function drawDetail() {
      const display = input({ value: user.display_name, maxlength: 128 });
      const lang = select([{ value: 'fa', label: t('language.fa') }, { value: 'en', label: t('language.en') }], user.language);
      const grant = grantPicker(Object.keys(roles), organizations);
      const self = user.user_id === store.me.user_id;
      const directory = user.auth_source === 'ldap';   // password and roles come from AD
      mount(body,
        h('div', { class: 'row' }, h('span', { class: 'tagpath' }, user.username), status(user),
          user.mfa_enabled ? badge(t('users.mfa_on'), 'online') : null),
        h('dl', { class: 'kv' },
          h('dt', null, t('users.created_at')), h('dd', null, fmtDate(user.created_at)),
          h('dt', null, t('users.last_login')), h('dd', null, fmtDate(user.last_login_at)),
          user.locked_until ? [h('dt', null, t('users.locked_until')), h('dd', null, fmtDate(user.locked_until))] : null),
        h('h3', null, t('users.profile')),
        h('div', { class: 'form-grid' }, field(t('users.display_name'), display), field(t('users.language'), lang)),
        h('div', { class: 'row' }, button(t('common.save'), act(() => api(`api/users/${user.user_id}`, { method: 'PATCH', body: { display_name: display.value.trim(), language: lang.value } }), t('common.saved')), { size: 'sm', kind: 'primary' })),
        h('h3', null, t('users.roles')),
        directory ? [h('div', { class: 'row' }, user.roles.length ? user.roles.map(roleChip) : empty(t('users.no_roles'))), notice(t('users.ldap_roles_note'))] : null,
        directory ? null : user.roles.length ? h('div', { class: 'stack tight' }, user.roles.map((g) => h('div', { class: 'row between' }, roleChip(g),
          button(t('users.revoke'), act(async () => {
            if (!(await confirmDialog(t('users.revoke'), t('users.revoke_text', { role: t(`roles.${g.role}`), user: user.display_name }), { danger: true }))) return;
            await api(`api/users/${user.user_id}/roles/${g.user_role_id}`, { method: 'DELETE' });
          }), { size: 'sm' })))) : empty(t('users.no_roles')),
        directory ? null : grant.el,
        directory ? null : h('div', { class: 'row' }, button(t('users.grant'), act(() => api(`api/users/${user.user_id}/roles`, { method: 'POST', body: grant.value() }), t('users.granted')), { size: 'sm', iconName: 'plus' })),
        h('h3', null, t('users.security')),
        h('div', { class: 'row' },
          directory ? null : button(t('users.reset_password'), () => resetPassword(user, refresh), { size: 'sm' }),
          user.locked_until ? button(t('users.unlock'), act(() => api(`api/users/${user.user_id}/unlock`, { method: 'POST' }), t('users.unlocked')), { size: 'sm' }) : null,
          user.mfa_enabled ? button(t('users.reset_mfa'), act(async () => {
            if (!(await confirmDialog(t('users.reset_mfa'), t('users.reset_mfa_text'), { danger: true }))) return;
            await api(`api/users/${user.user_id}/mfa/reset`, { method: 'POST' });
          }, t('users.mfa_reset_done')), { size: 'sm' }) : null,
          button(t('users.end_sessions'), act(async () => {
            if (!(await confirmDialog(t('users.end_sessions'), t('users.end_sessions_text', { user: user.display_name }), { danger: true }))) return;
            await api(`api/users/${user.user_id}/sessions`, { method: 'DELETE' });
          }, t('users.sessions_ended')), { size: 'sm' }),
          !self ? button(user.is_active ? t('users.disable') : t('users.enable'), act(async () => {
            if (user.is_active && !(await confirmDialog(t('users.disable'), t('users.disable_text', { user: user.display_name }), { danger: true }))) return;
            await api(`api/users/${user.user_id}`, { method: 'PATCH', body: { is_active: !user.is_active } });
          }), { size: 'sm', kind: user.is_active ? 'danger' : 'primary' }) : null));
    }
    drawDetail();
  }

  function resetPassword(user, after) {
    const pw = input({ ltr: true, type: 'password', autocomplete: 'new-password' });
    modal({
      title: t('users.reset_password'),
      body: h('div', { class: 'stack' }, notice(t('users.reset_password_note')), field(t('users.temp_password'), pw, { hint: passwordHint() })),
      actions: [{ label: t('common.cancel') }, {
        label: t('users.reset_password'), kind: 'primary',
        onClick: async () => { await api(`api/users/${user.user_id}/password`, { method: 'POST', body: { new_password: pw.value } }); toast(t('users.password_reset_done'), 'ok'); await after(); },
      }],
    });
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('users.title')), h('div', { class: 'sub' }, t('users.subtitle'))),
      button(t('users.new'), () => createUser(), { kind: 'primary', iconName: 'plus' })),
    box);
  load();
  return null;
}

