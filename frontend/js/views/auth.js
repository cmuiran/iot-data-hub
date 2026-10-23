// Sign-in flow: password -> (MFA code) -> (forced password change) -> (MFA enrolment).

import { api, login, verifyMfa, logout } from '../api.js';
import { t, tMaybe, getLanguage, LANGUAGES } from '../i18n.js';
import { h, mount, field, input, button, errorText, copyText, notice } from '../ui.js';

function frame(app, ...content) {
  const langSwitch = h('div', { class: 'row' }, LANGUAGES.map((l) => button(t(`language.${l}`), async () => {
    const { changeLanguage } = await import('../main.js');
    await changeLanguage(l);
    // the auth screen is rebuilt by whoever rendered it
    document.dispatchEvent(new CustomEvent('hub:auth-rerender'));
  }, { kind: l === getLanguage() ? 'on' : 'ghost', size: 'sm' })));
  mount(app, h('div', { class: 'auth-screen' }, h('div', { class: 'auth-box' },
    h('div', { class: 'brand' }, h('img', { src: 'img/icon.svg', alt: '' }),
      h('div', null, t('app.name'), h('small', null, t('app.tagline')))),
    h('div', { class: 'auth-card' }, ...content),
    h('div', { class: 'auth-foot' }, h('span', null, t('auth.footer')), langSwitch))));
}

function formOf(onSubmit, ...children) {
  const errBox = h('div', { class: 'notice bad', hidden: true, role: 'alert' });
  const form = h('form', { class: 'stack', novalidate: true }, ...children, errBox);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errBox.hidden = true;
    const submit = form.querySelector('button[type=submit]');
    if (submit) submit.disabled = true;
    try {
      await onSubmit();
    } catch (err) {
      errBox.textContent = errorText(err);
      errBox.hidden = false;
    } finally {
      if (submit) submit.disabled = false;
    }
  });
  form.showError = (text) => { errBox.textContent = text; errBox.hidden = !text; };
  return form;
}

export function renderAuth(app, { step, reason, onDone, username }) {
  let current = step;
  let knownUser = username || null;
  let mfaToken = null;
  const rerender = () => draw();
  document.addEventListener('hub:auth-rerender', rerender);
  const finish = async () => {
    document.removeEventListener('hub:auth-rerender', rerender);
    await onDone();
  };

  function draw() {
    if (current === 'login') drawLogin();
    else if (current === 'mfa') drawMfa();
    else if (current === 'change-password') drawChangePassword();
    else if (current === 'mfa-enroll') drawEnroll();
  }

  function drawLogin() {
    const user = input({ autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false', ltr: true, required: true });
    const pass = input({ type: 'password', autocomplete: 'current-password', ltr: true, required: true });
    const form = formOf(async () => {
      if (!user.value.trim() || !pass.value) { form.showError(t('auth.fill_both')); return; }
      knownUser = user.value.trim();
      const result = await login(knownUser, pass.value);
      if (result.mfa_required) { mfaToken = result.mfa_token; current = 'mfa'; draw(); return; }
      await finish();
    },
    h('h1', null, t('auth.sign_in')),
    reason ? notice(tMaybe(`auth.reason.${reason}`) || t('auth.reason.generic'), 'warn') : null,
    field(t('auth.username'), user), field(t('auth.password'), pass),
    button(t('auth.sign_in'), null, { kind: 'primary', type: 'submit' }));
    frame(app, form);
    user.focus();
  }

  function drawMfa() {
    let recovery = false;
    const code = input({ class: 'input otp', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8 });
    const rec = input({ ltr: true, autocomplete: 'off', maxlength: 20 });
    const codeField = field(t('auth.mfa_code'), code, { hint: t('auth.mfa_hint') });
    const recField = field(t('auth.recovery_code'), rec, { hint: t('auth.recovery_hint') });
    recField.hidden = true;
    const toggle = button(t('auth.use_recovery'), () => {
      recovery = !recovery;
      codeField.hidden = recovery; recField.hidden = !recovery;
      toggle.textContent = recovery ? t('auth.use_code') : t('auth.use_recovery');
      (recovery ? rec : code).focus();
    }, { kind: 'ghost', size: 'sm' });
    const form = formOf(async () => {
      try {
        await verifyMfa(mfaToken, recovery ? { recoveryCode: rec.value.trim() } : { code: code.value.replace(/\s/g, '') });
      } catch (err) {
        if (err.code === 'invalid_token') { current = 'login'; reason = 'mfa_timeout'; draw(); return; }
        throw err;
      }
      await finish();
    },
    h('h1', null, t('auth.mfa_title')), codeField, recField,
    h('div', { class: 'row between' }, toggle, button(t('auth.verify'), null, { kind: 'primary', type: 'submit' })));
    frame(app, form);
    code.focus();
  }

  function drawChangePassword() {
    const cur = input({ type: 'password', autocomplete: 'current-password', ltr: true });
    const next = input({ type: 'password', autocomplete: 'new-password', ltr: true });
    const again = input({ type: 'password', autocomplete: 'new-password', ltr: true });
    const form = formOf(async () => {
      if (next.value !== again.value) { form.showError(t('auth.passwords_differ')); return; }
      await api('api/auth/change-password', { method: 'POST', body: { current_password: cur.value, new_password: next.value } });
      // the server ends every session of the user after a password change: sign in again
      const result = await login(knownUser, next.value);
      if (result.mfa_required) { mfaToken = result.mfa_token; current = 'mfa'; draw(); return; }
      await finish();
    },
    h('h1', null, t('auth.change_password')),
    notice(t('auth.must_change'), 'warn'),
    field(t('auth.current_password'), cur),
    field(t('auth.new_password'), next, { hint: t('auth.password_rules') }),
    field(t('auth.new_password_again'), again),
    h('div', { class: 'row between' }, button(t('auth.logout'), () => logout(), { kind: 'ghost' }),
      button(t('common.save'), null, { kind: 'primary', type: 'submit' })));
    frame(app, form);
    cur.focus();
  }

  async function drawEnroll() {
    frame(app, h('p', null, t('common.loading')));
    let setup;
    try {
      setup = await api('api/auth/mfa/setup', { method: 'POST' });
    } catch (err) {
      frame(app, h('h1', null, t('auth.mfa_setup')), notice(errorText(err), 'bad'),
        button(t('auth.logout'), () => logout(), { kind: 'ghost' }));
      return;
    }
    const code = input({ class: 'input otp', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8 });
    const form = formOf(async () => {
      const result = await api('api/auth/mfa/enable', { method: 'POST', body: { code: code.value.replace(/\s/g, '') } });
      showRecovery(result.recovery_codes);
    },
    h('h1', null, t('auth.mfa_setup')),
    notice(t('auth.mfa_required_for_role'), 'warn'),
    ...mfaSetupBlock(setup),
    field(t('auth.mfa_first_code'), code),
    h('div', { class: 'row between' }, button(t('auth.logout'), () => logout(), { kind: 'ghost' }),
      button(t('auth.enable_mfa'), null, { kind: 'primary', type: 'submit' })));
    frame(app, form);
  }

  function showRecovery(codes) {
    frame(app, ...recoveryBlock(codes), button(t('auth.continue'), () => finish(), { kind: 'primary' }));
  }

  draw();
}

/** Secret + otpauth link for an authenticator app (shared with the account page). */
export function mfaSetupBlock(setup) {
  const grouped = setup.secret.replace(/(.{4})/g, '$1 ').trim();
  return [
    h('ol', { class: 'small stack tight' },
      h('li', null, t('auth.mfa_step1')),
      h('li', null, t('auth.mfa_step2')),
      h('li', null, t('auth.mfa_step3'))),
    h('div', { class: 'secret' }, grouped),
    h('div', { class: 'row' },
      button(t('auth.copy_secret'), () => copyText(setup.secret), { size: 'sm' }),
      h('a', { class: 'btn sm', href: setup.otpauth_uri }, t('auth.open_in_app'))),
    h('p', { class: 'tiny faint' }, t('auth.mfa_manual_hint')),
  ];
}

export function recoveryBlock(codes) {
  return [
    h('h1', null, t('auth.recovery_title')),
    notice(t('auth.recovery_once'), 'warn'),
    h('div', { class: 'codes' }, codes.map((c) => h('span', null, c))),
    button(t('auth.copy_codes'), () => copyText(codes.join('\n')), { size: 'sm' }),
  ];
}
