// My account: language, theme, password, two-factor authentication, my sessions.

import { api, logout, login } from '../api.js';
import { t, getLanguage, LANGUAGES, fmtDate } from '../i18n.js';
import {
  h, mount, button, segmented, field, input, notice, errorText, toast, table, badge, modal, confirmDialog,
  siteName, areaName,
} from '../ui.js';
import { store } from '../store.js';
import { themeSetting, setTheme } from '../theme.js';
import { mfaSetupBlock, recoveryBlock } from './auth.js';

export async function render(page, { setTitle, changeLanguage }) {
  setTitle(t('account.title'));
  const me = store.me;
  const sessionsBox = h('div', { class: 'card flush' });
  const mfaBox = h('div', { class: 'card stack' });

  const roles = me.roles.map((g) => badge(`${t(`roles.${g.role}`)} · ${g.site ? (g.area ? `${siteName(g.site)} › ${areaName(g.area)}` : siteName(g.site)) : t('users.scope_all')}`, 'accent'));

  // --- password
  const cur = input({ type: 'password', autocomplete: 'current-password', ltr: true });
  const next = input({ type: 'password', autocomplete: 'new-password', ltr: true });
  const again = input({ type: 'password', autocomplete: 'new-password', ltr: true });
  const pwMsg = h('div');
  const changePw = async () => {
    mount(pwMsg);
    if (next.value !== again.value) { mount(pwMsg, notice(t('auth.passwords_differ'), 'bad')); return; }
    try {
      await api('api/auth/change-password', { method: 'POST', body: { current_password: cur.value, new_password: next.value } });
      // every session ends with a password change; continue here with a fresh one
      const result = await login(me.username, next.value);
      cur.value = ''; next.value = ''; again.value = '';
      if (result.mfa_required) { logout(); return; }
      mount(pwMsg, notice(t('account.password_changed'), 'ok'));
    } catch (err) { mount(pwMsg, notice(errorText(err), 'bad')); }
  };

  // --- MFA
  function drawMfa() {
    if (me.mfa_enabled) {
      mount(mfaBox, h('h2', null, t('account.mfa')), notice(t('account.mfa_on'), 'ok'),
        h('div', { class: 'row' }, button(t('account.mfa_disable'), disableMfa, { kind: 'danger' })));
    } else {
      mount(mfaBox, h('h2', null, t('account.mfa')), notice(t('account.mfa_off'), 'warn'),
        h('div', { class: 'row' }, button(t('account.mfa_enable'), enableMfa, { kind: 'primary' })));
    }
  }
  async function enableMfa() {
    let setup;
    try { setup = await api('api/auth/mfa/setup', { method: 'POST' }); } catch (err) { toast(errorText(err), 'bad'); return; }
    const code = input({ class: 'input otp', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8 });
    modal({
      title: t('auth.mfa_setup'),
      body: h('div', { class: 'stack' }, ...mfaSetupBlock(setup), field(t('auth.mfa_first_code'), code)),
      actions: [{ label: t('common.cancel') }, {
        label: t('auth.enable_mfa'), kind: 'primary',
        onClick: async () => {
          const res = await api('api/auth/mfa/enable', { method: 'POST', body: { code: code.value.replace(/\s/g, '') } });
          me.mfa_enabled = true;
          drawMfa();
          modal({ title: t('auth.recovery_title'), body: h('div', { class: 'stack' }, ...recoveryBlock(res.recovery_codes).slice(1)) });
        },
      }],
    });
  }
  function disableMfa() {
    const pw = input({ type: 'password', autocomplete: 'current-password', ltr: true });
    const code = input({ class: 'input otp', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8 });
    modal({
      title: t('account.mfa_disable'),
      body: h('div', { class: 'stack' }, notice(t('account.mfa_disable_note'), 'warn'), field(t('auth.password'), pw), field(t('auth.mfa_code'), code)),
      actions: [{ label: t('common.cancel') }, {
        label: t('account.mfa_disable'), kind: 'danger',
        onClick: async () => {
          await api('api/auth/mfa/disable', { method: 'POST', body: { password: pw.value, code: code.value.replace(/\s/g, '') } });
          me.mfa_enabled = false;
          drawMfa();
          toast(t('account.mfa_disabled'), 'ok');
        },
      }],
    });
  }

  // --- sessions
  async function loadSessions() {
    try {
      const list = await api('api/auth/sessions');
      mount(sessionsBox, h('div', { class: 'card-head' }, h('h2', null, t('account.sessions'))),
        table([
          { label: t('account.started'), render: (s) => h('span', { class: 'small num' }, fmtDate(s.created_at)) },
          { label: t('account.address'), render: (s) => h('span', { class: 'tagpath' }, s.client_addr || '') },
          { label: t('account.browser'), render: (s) => h('span', { class: 'tiny' }, (s.user_agent || '').slice(0, 90)) },
          { label: t('account.expires'), render: (s) => h('span', { class: 'small num' }, fmtDate(s.expires_at)) },
          { label: '', cls: 'actions', render: (s) => (s.current ? badge(t('account.this_session'), 'online') : button(t('account.end'), async () => {
            if (!(await confirmDialog(t('account.end'), t('account.end_text')))) return;
            try { await api(`api/auth/sessions/${s.session_id}`, { method: 'DELETE' }); loadSessions(); } catch (err) { toast(errorText(err), 'bad'); }
          }, { size: 'sm' })) },
        ], list));
    } catch (err) { mount(sessionsBox, notice(errorText(err), 'bad')); }
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, me.display_name), h('div', { class: 'sub mono' }, me.username)),
      button(t('auth.logout'), () => logout(), { iconName: 'logout' })),
    h('div', { class: 'split' },
      h('div', { class: 'stack' },
        h('div', { class: 'card stack' }, h('h2', null, t('account.roles')), h('div', { class: 'row' }, roles.length ? roles : t('users.no_roles'))),
        h('div', { class: 'card stack' }, h('h2', null, t('account.display')),
          field(t('account.language'), segmented(LANGUAGES.map((l) => ({ value: l, label: t(`language.${l}`) })), getLanguage(), (l) => changeLanguage(l))),
          field(t('account.theme'), segmented(['system', 'light', 'dark'].map((v) => ({ value: v, label: t(`account.theme_${v}`) })), themeSetting(), (v) => setTheme(v)))),
        mfaBox),
      h('div', { class: 'card stack' }, h('h2', null, t('auth.change_password')),
        field(t('auth.current_password'), cur), field(t('auth.new_password'), next, { hint: t('auth.password_rules') }), field(t('auth.new_password_again'), again),
        h('div', { class: 'row' }, button(t('common.save'), changePw, { kind: 'primary' })), pwMsg)),
    sessionsBox);
  drawMfa();
  loadSessions();
  return null;
}
