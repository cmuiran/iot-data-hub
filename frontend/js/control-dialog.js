// The command dialog: shows the tag's policy, checks the value in the browser, asks for a
// review of old -> new, then sends it through POST /api/control/write (the control-safety
// layer on the server makes the real decision and writes the audit trail).

import { api } from './api.js';
import { t, tMaybe, fmtNumber, fmtDate, fmtDuration } from './i18n.js';
import {
  h, mount, modal, field, input, button, valueText, unitText, tagLabel, notice, errorText,
  parseNumber, badge,
} from './ui.js';
import { store, deviceOfTag } from './store.js';

const OPS = { eq: (a, b) => a === b, ne: (a, b) => a !== b, lt: (a, b) => a < b, le: (a, b) => a <= b, gt: (a, b) => a > b, ge: (a, b) => a >= b };

/** Browser-side preview of an interlock (the server checks again with fresh data). */
export function interlockState(lock) {
  const tag = store.tags.get(lock.tag);
  if (!tag || tag.value === null || tag.value === undefined || tag.quality !== 'good') return 'unknown';
  if (tag.ts && (Date.now() - Date.parse(tag.ts)) / 1000 > lock.max_age_s) return 'stale';
  const fn = OPS[lock.op];
  return fn && fn(tag.value, lock.value) ? 'ok' : 'blocked';
}

function disabledReason(tag, policy) {
  if (!store.info?.control_enabled) return t('control.disabled_hub');
  const device = deviceOfTag(tag.tag);
  if (device && !device.enabled) return t('control.disabled_device');
  if (tag.access !== 'read_write') return t('control.read_only');
  if (!policy.enabled) return t('control.disabled_generic');
  return null;
}

/** Validate a typed value against the tag config and policy. Returns {value} or {error}. */
export function checkValue(tag, policy, raw) {
  if (tag.data_type === 'bool') {
    if (raw !== true && raw !== false) return { error: t('control.pick_state') };
    return { value: raw };
  }
  const v = parseNumber(raw);
  if (Number.isNaN(v)) return { error: t('control.not_a_number') };
  if (/int/.test(tag.data_type) && !Number.isInteger(v)) return { error: t('control.must_be_integer') };
  if (tag.min !== null && v < tag.min) return { error: t('control.below_min', { min: fmtNumber(tag.min) }) };
  if (tag.max !== null && v > tag.max) return { error: t('control.above_max', { max: fmtNumber(tag.max) }) };
  if (policy.max_step && typeof tag.value === 'number' && Math.abs(v - tag.value) > policy.max_step) {
    return { error: t('control.step_too_big', { step: fmtNumber(policy.max_step) }) };
  }
  return { value: v };
}

export function resultBox(result) {
  return h('div', { class: ['result-box', result.status], role: 'status' },
    h('div', { class: 'row between' }, h('strong', null, t(`write_status.${result.status}`)),
      result.result !== result.status && result.result !== 'requested' ? badge(tMaybe(`write_result.${result.result}`) || result.result, 'accent') : null),
    h('p', { class: 'small mono' }, result.message || ''),
    result.status === 'pending_confirmation' && result.expires_at
      ? h('p', { class: 'small' }, t('control.waiting_until', { time: fmtDate(result.expires_at, 'timesec') })) : null,
    result.readback_value !== null && result.readback_value !== undefined
      ? h('p', { class: 'small mono' }, `${t('control.readback')}: ${result.readback_value}`) : null);
}

export async function openWriteDialog(path, { onResult } = {}) {
  const tag = store.tags.get(path);
  if (!tag) return;
  let policy;
  try {
    policy = await api(`api/control/policy/${encodeURIComponent(path)}`);
  } catch (err) {
    modal({ title: t('control.dialog_title'), body: notice(errorText(err), 'bad') });
    return;
  }
  const reasonDisabled = disabledReason(tag, policy);
  const isBool = tag.data_type === 'bool';
  let chosen = isBool ? null : undefined;
  const valueInput = isBool ? null : input({ ltr: true, inputmode: 'decimal', autocomplete: 'off', value: typeof tag.value === 'number' ? String(tag.value) : '' });
  const errLine = h('span', { class: 'err' });
  const boolButtons = isBool ? h('div', { class: 'big-switch' }, [true, false].map((v) => {
    const b = button(valueText(tag, v), () => { chosen = v; for (const x of boolButtons.children) x.classList.toggle('on', x === b); }, {});
    return b;
  })) : null;
  const reason = h('textarea', { class: 'input', maxlength: 500, rows: 2, placeholder: t('control.reason_placeholder') });
  const current = h('dd', { class: 'mono' }, `${valueText(tag, tag.value)} ${typeof tag.value === 'number' ? unitText(tag.unit) : ''}`);

  const locks = policy.interlocks.map((lock) => {
    const st = interlockState(lock);
    return h('li', { class: 'row' }, badge(t(`interlock.${st}`), st === 'ok' ? 'online' : st === 'blocked' ? 'error' : 'uncertain'),
      h('span', null, lock.message), h('span', { class: 'tagpath' }, `${lock.tag} ${lock.op} ${lock.value}`));
  });

  const summary = h('dl', { class: 'ctl-summary' },
    h('dt', null, t('control.tag')), h('dd', null, tagLabel(tag), h('div', { class: 'tagpath' }, tag.tag)),
    h('dt', null, t('control.current')), current,
    !isBool ? [h('dt', null, t('control.range')), h('dd', { class: 'mono' }, `${tag.min ?? '−∞'} … ${tag.max ?? '+∞'} ${unitText(tag.unit)}`)] : null,
    policy.max_step ? [h('dt', null, t('control.max_step')), h('dd', { class: 'mono' }, `${fmtNumber(policy.max_step)} ${unitText(tag.unit)}`)] : null,
    h('dt', null, t('control.min_interval')), h('dd', null, fmtDuration(policy.min_interval_s)),
    h('dt', null, t('control.confirmation')), h('dd', null, policy.confirm ? t('control.needs_second') : t('control.no_second')));

  const body = h('div', { class: 'stack' }, summary,
    locks.length ? h('div', { class: 'stack tight' }, h('h3', null, t('control.interlocks')), h('ul', { class: 'stack tight small' }, locks)) : null,
    reasonDisabled ? notice(reasonDisabled, 'warn') : h('div', { class: 'stack' },
      field(t('control.new_value'), isBool ? boolButtons : valueInput, { hint: isBool ? null : unitText(tag.unit) }),
      errLine,
      field(t('control.reason'), reason)));

  const dlg = modal({
    title: t('control.dialog_title'),
    body,
    actions: reasonDisabled ? [{ label: t('common.close') }] : [
      { label: t('common.cancel') },
      { label: t('control.review'), kind: 'primary', closes: false, onClick: ({ setActions }) => review(setActions) },
    ],
  });

  function review(setActions) {
    const check = checkValue(tag, policy, isBool ? chosen : valueInput.value);
    errLine.textContent = check.error || '';
    if (check.error) return false;
    const blocked = policy.interlocks.filter((l) => interlockState(l) === 'blocked');
    mount(body,
      h('div', { class: 'change' }, h('span', { class: 'old' }, valueText(tag, tag.value)), h('span', { class: 'arrow' }, '→'),
        h('span', null, `${valueText(tag, check.value)} ${isBool ? '' : unitText(tag.unit)}`)),
      h('p', null, t('control.review_text', { tag: tagLabel(tag) })),
      blocked.length ? notice(t('control.interlock_warning'), 'bad') : null,
      policy.confirm ? notice(t('control.confirm_notice'), 'warn') : null,
      reason.value ? h('p', { class: 'small muted' }, `${t('control.reason')}: ${reason.value}`) : null);
    setActions([
      { label: t('control.back'), closes: false, onClick: () => { dlg.close(); openWriteDialog(path, { onResult }); return false; } },
      { label: t('control.send'), kind: 'danger', closes: false, onClick: () => send(check.value, setActions) },
    ]);
    return false;
  }

  async function send(value, setActions) {
    const result = await api('api/control/write', { method: 'POST', body: { tag: path, value, reason: reason.value.trim() || null } });
    mount(body, resultBox(result));
    setActions([{ label: t('common.close') }]);
    onResult?.(result);
    return false;
  }
}
