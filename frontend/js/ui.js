// DOM helpers. Text always goes in as text nodes (never innerHTML), so data from devices,
// users or the API cannot inject markup. Styles are set through the CSSOM (allowed by the CSP).

import {
  t, tMaybe, fmtNumber, fmtDate, localParts, calendar, timeZone, monthName, weekdayNames,
  normaliseDigits,
} from './i18n.js';
import { fromCalendar, monthLength, parseCalendarText, calendarDate } from './jalali.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.setAttribute('class', Array.isArray(value) ? value.filter(Boolean).join(' ') : value);
    else if (key === 'style') Object.assign(el.style, value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'value' && 'value' in el) el.value = value;
    else if (key === 'checked' || key === 'selected' || key === 'disabled') el[key] = Boolean(value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
}

/** h('div', {class: 'x', onclick}, 'text', child, [more]) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) {
    append(el, [attrs, ...children]);
  } else {
    applyAttrs(el, attrs);
    append(el, children);
  }
  return el;
}

export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }

// --- icons (stroke paths, 24x24) -----------------------------------------------------------
const ICONS = {
  overview: 'M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 3v6h8V3z',
  alarm: 'M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6zM10 19a2 2 0 0 0 4 0',
  trend: 'M3 17l5-6 4 4 8-9M14 6h6v6',
  report: 'M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h7M9 9h3',
  control: 'M5 20V12M5 8V4M12 20v-6M12 10V4M19 20v-4M19 12V4M3 12h4M10 10h4M17 16h4',
  dashboard: 'M4 4h7v9H4zM13 4h7v5h-7zM13 11h7v9h-7zM4 15h7v5H4z',
  users: 'M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM22 19v-1a4 4 0 0 0-3-3.9M16 4.1a3 3 0 0 1 0 5.8',
  devices: 'M4 5h16v10H4zM8 19h8M12 15v4',
  rules: 'M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2',
  schedule: 'M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z',
  audit: 'M12 3l8 3v6c0 4.5-3.4 8.4-8 9-4.6-.6-8-4.5-8-9V6zM9 12l2 2 4-4',
  system: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  account: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  menu: 'M3 6h18M3 12h18M3 18h18',
  close: 'M18 6L6 18M6 6l12 12',
  sound: 'M11 5L6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14',
  mute: 'M11 5L6 9H2v6h4l5 4zM23 9l-6 6M17 9l6 6',
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0',
  plus: 'M12 5v14M5 12h14',
  edit: 'M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6',
  up: 'M12 19V5M5 12l7-7 7 7',
  down: 'M12 5v14M19 12l-7 7-7-7',
  refresh: 'M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15',
  download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  play: 'M6 4l14 8-14 8z',
  calendar: 'M4 5h16v16H4zM4 10h16M9 3v4M15 3v4',
  sun: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4',
  check: 'M20 6L9 17l-5-5',
  link: 'M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7',
};

export function icon(name, cls = 'icon') {
  return s('svg', { class: cls, viewBox: '0 0 24 24', 'aria-hidden': 'true' }, s('path', { d: ICONS[name] || '' }));
}

// --- buttons / small pieces ----------------------------------------------------------------

export function button(label, onclick, { kind, size, iconName, title, disabled, type = 'button' } = {}) {
  return h('button', {
    type, class: ['btn', kind, size, !label && 'icon-only'], onclick, title: title || undefined,
    'aria-label': !label ? title : undefined, disabled,
  }, iconName ? icon(iconName) : null, label || null);
}

export function badge(text, cls, withDot = false) {
  return h('span', { class: ['badge', cls] }, withDot ? h('span', { class: 'dot' }) : null, text);
}

export function stateBadge(state) {
  return badge(t(`device_state.${state}`), state, true);
}

export function qualityBadge(q) {
  if (!q) return badge('—', 'unknown');
  return badge(t(`quality.${q}`), q);
}

export function severityBadge(sev) { return badge(t(`severity.${sev}`), `sev-${sev}`); }

export function protoBadge(protocol) { return h('span', { class: 'proto' }, t(`protocol.${protocol}`)); }

export function field(label, control, { hint, error, wide, id } = {}) {
  const lid = id || control.id || (control.id = `f${Math.random().toString(36).slice(2, 9)}`);
  return h('div', { class: ['field', wide && 'wide'] },
    h('label', { for: lid }, label), control,
    hint ? h('span', { class: 'hint' }, hint) : null,
    error ? h('span', { class: 'err' }, error) : null);
}

export function input(attrs = {}) { return h('input', { class: ['input', attrs.ltr && 'ltr'], type: 'text', ...attrs, ltr: undefined }); }

export function select(options, value, attrs = {}) {
  return h('select', { class: 'input', ...attrs },
    options.map((o) => {
      const opt = typeof o === 'object' ? o : { value: o, label: o };
      return h('option', { value: opt.value, selected: String(opt.value) === String(value) }, opt.label);
    }));
}

export function checkbox(label, checked, attrs = {}) {
  const box = h('input', { type: 'checkbox', checked, ...attrs });
  const wrap = h('label', { class: 'check' }, box, label);
  wrap.input = box;
  return wrap;
}

export function segmented(options, value, onchange) {
  const wrap = h('div', { class: 'seg', role: 'group' });
  const render = (current) => mount(wrap, options.map((o) => h('button', {
    type: 'button', class: o.value === current ? 'on' : '', 'aria-pressed': String(o.value === current),
    onclick: () => { render(o.value); onchange(o.value); },
  }, o.label)));
  render(value);
  return wrap;
}

export function tabs(items, active, onchange) {
  const wrap = h('div', { class: 'tabs', role: 'tablist' });
  const render = (current) => mount(wrap, items.map((it) => h('button', {
    type: 'button', role: 'tab', class: it.id === current ? 'on' : '', 'aria-selected': String(it.id === current),
    onclick: () => { render(it.id); onchange(it.id); },
  }, it.label, it.count !== undefined ? h('span', { class: 'count' }, fmtNumber(it.count, 0)) : null)));
  render(active);
  wrap.select = render;
  return wrap;
}

export function empty(text) { return h('div', { class: 'empty' }, text); }
export function notice(text, kind = '') { return h('div', { class: ['notice', kind] }, text); }
export function loading() { return h('div', { class: 'empty' }, t('common.loading')); }

/** Simple data table. columns: [{label, render(row) -> node|text, cls, thCls}] */
export function table(columns, rows, { emptyText, onRow, rowClass } = {}) {
  if (!rows.length) return empty(emptyText || t('common.no_items'));
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', h('tr', columns.map((c) => h('th', { class: c.thCls }, c.label)))),
    h('tbody', rows.map((row) => h('tr', {
      class: [onRow && 'clickable', rowClass?.(row)],
      onclick: onRow ? (e) => { if (!e.target.closest('button, a, input, select')) onRow(row); } : undefined,
    }, columns.map((c) => h('td', { class: c.cls }, c.render(row))))))));
}

// --- values ----------------------------------------------------------------------------------

const UNITS = { degC: '°C', degF: '°F', pct: '%', percent: '%' };
export function unitText(unit) { return unit ? (UNITS[unit] || unit) : ''; }

/** A tag value for display: number with unit, bool as words, '—' when missing. */
export function valueText(tag, value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean' || tag?.data_type === 'bool') {
    const key = value ? 'true' : 'false';
    return tMaybe(`tag_words.${tag?.name}.${key}`) || t(`bool.${key}`);
  }
  if (typeof value === 'number') {
    const integer = /int/.test(tag?.data_type || '');
    return fmtNumber(value, integer ? 0 : undefined);
  }
  return String(value);
}

export function valueNode(tag, value) {
  const text = valueText(tag, value);
  const unit = typeof value === 'number' ? unitText(tag?.unit) : '';
  return h('span', null, text, unit ? h('span', { class: 'unit' }, unit) : null);
}

/** Display name of a tag/device: a translation if one exists, else the config name. */
export function tagLabel(tag) {
  return tMaybe(`tag_names.${tag.name}`) || tag.description || tag.name;
}

const DETAILED = new Set(['invalid', 'schedule_invalid', 'config_invalid', 'device_invalid', 'rule_invalid',
  'policy_invalid', 'weak_password', 'invalid_range', 'not_numeric']);

export function errorText(err) {
  if (!err) return '';
  if (err.status === undefined) return err.message || t('errors.http_error');  // a local check
  const code = err.code || 'http_error';
  const text = tMaybe(`errors.${code}`);
  // for refused input the server's message says what exactly is wrong: keep it as detail
  if (text && DETAILED.has(code) && err.message) {
    const details = (err.details || []).map((d) => `${(d.loc || []).filter((x) => x !== 'body').join('.')}: ${d.msg}`);
    return `${text}: ${[err.message, ...details].join(' — ')}`;
  }
  return text || err.message || t('errors.http_error');
}

// --- toast / modal -------------------------------------------------------------------------

let toastBox;
export function toast(text, kind = '') {
  if (!toastBox) { toastBox = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(toastBox); }
  const el = h('div', { class: ['toast', kind] }, text);
  toastBox.append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 7000 : 4000);
}

/**
 * Open a dialog. `build(close)` returns the body node; actions: [{label, kind, onClick}] where
 * onClick may return a promise (the button shows busy state; a thrown error stays in the dialog).
 */
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const previous = document.activeElement;
  const errBox = h('div', { class: 'notice bad', hidden: true, role: 'alert' });
  let closed = false;
  const overlay = h('div', { class: 'overlay' });
  const close = (value) => {
    if (closed) return;
    closed = true;
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    previous?.focus?.();
    onClose?.(value);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(null); };
  const footer = h('div', { class: 'modal-foot' });
  const setActions = (list) => mount(footer, list.map((a) => {
    const b = button(a.label, async () => {
      errBox.hidden = true;
      if (!a.onClick) { close(null); return; }
      b.disabled = true;
      try {
        const result = await a.onClick({ close, setError, setActions });
        if (result !== false && a.closes !== false) close(result);
      } catch (err) {
        setError(errorText(err));
      } finally {
        b.disabled = false;
      }
    }, { kind: a.kind, disabled: a.disabled });
    return b;
  }));
  const setError = (text) => { errBox.textContent = text; errBox.hidden = !text; };
  const content = typeof body === 'function' ? body({ close, setError, setActions }) : body;
  const dialog = h('div', { class: ['modal', wide && 'wide'], role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h2', null, title), button('', () => close(null), { kind: 'ghost', iconName: 'close', title: t('common.close') })),
    h('div', { class: 'modal-body' }, content, errBox),
    footer);
  setActions(actions.length ? actions : [{ label: t('common.close') }]);
  overlay.append(dialog);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(null); });
  document.addEventListener('keydown', onKey);
  document.body.append(overlay);
  (dialog.querySelector('.modal-body input, .modal-body select, .modal-body textarea') || footer.querySelector('button'))?.focus();
  return { close, setError, setActions, dialog };
}

/** Promise<boolean> confirmation built into the page (no window.confirm). */
export function confirmDialog(title, text, { confirmLabel, danger = false, typeToConfirm } = {}) {
  return new Promise((resolve) => {
    let typed;
    const body = h('div', { class: 'stack' }, h('p', null, text),
      typeToConfirm ? field(t('common.type_to_confirm', { text: typeToConfirm }),
        typed = input({ ltr: true, autocomplete: 'off' })) : null);
    modal({
      title, body,
      onClose: (v) => resolve(v === true),
      actions: [
        { label: t('common.cancel') },
        {
          label: confirmLabel || t('common.confirm'), kind: danger ? 'danger' : 'primary',
          onClick: () => {
            if (typeToConfirm && typed.value.trim() !== typeToConfirm) throw new Error(t('common.type_mismatch'));
            return true;
          },
        },
      ],
    });
  });
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast(t('common.copied'), 'ok'); } catch { toast(t('common.copy_failed'), 'bad'); }
}

// --- date + time input in the plant's calendar ---------------------------------------------

function pad(n) { return String(n).padStart(2, '0'); }

/** Text for a Date in the active calendar: "1405/08/01 14:30". */
export function calendarText(date, withTime = true) {
  const p = localParts(date);
  const text = `${p.y}/${pad(p.m)}/${pad(p.d)}`;
  return withTime ? `${text} ${pad(p.hour)}:${pad(p.minute)}` : text;
}

/**
 * A date-time field in the active calendar (Jalali for Persian) with a month picker.
 * Returns an element with .getDate() (Date | null) and .setDate(date).
 */
export function dateTimeInput(initial, { withTime = true, id } = {}) {
  const cal = calendar();
  const tz = timeZone();
  const text = input({ ltr: true, id, placeholder: withTime ? 'YYYY/MM/DD HH:MM' : 'YYYY/MM/DD', autocomplete: 'off', inputmode: 'numeric' });
  const pop = h('div', { class: 'dp-pop', hidden: true });
  const wrap = h('div', { class: 'dp' }, h('div', { class: 'row' }, text,
    button('', () => { pop.hidden ? openPicker() : (pop.hidden = true); }, { kind: 'ghost', size: 'sm', iconName: 'calendar', title: t('common.pick_date') })), pop);
  let view;

  const current = () => {
    const parsed = parseCalendarText(normaliseDigits(text.value), cal);
    return parsed ? fromCalendar(parsed.y, parsed.m, parsed.d, parsed.hour, parsed.minute, tz, cal) : null;
  };
  wrap.getDate = () => current();
  wrap.setDate = (d) => { text.value = d ? calendarText(d, withTime) : ''; text.classList.remove('invalid'); };
  text.addEventListener('change', () => text.classList.toggle('invalid', Boolean(text.value) && !current()));

  function openPicker() {
    const base = current() || new Date();
    const p = calendarDate(base, tz, cal);
    view = { y: p.y, m: p.m };
    renderPicker();
    pop.hidden = false;
  }
  function renderPicker() {
    const sel = current();
    const selP = sel ? calendarDate(sel, tz, cal) : null;
    const today = calendarDate(new Date(), tz, cal);
    const first = fromCalendar(view.y, view.m, 1, 12, 0, tz, cal);
    let lead = calendarDate(first, tz, cal).weekday;  // 0 = Saturday
    if (cal !== 'persian') lead = (lead + 5) % 7;      // Gregorian grid starts on Monday
    const days = monthLength(view.y, view.m, cal);
    const names = weekdayNames();
    const order = cal === 'persian' ? names : [...names.slice(2), ...names.slice(0, 2)];
    const cells = [];
    for (let i = 0; i < lead; i += 1) cells.push(h('span'));
    for (let d = 1; d <= days; d += 1) {
      const isSel = selP && selP.y === view.y && selP.m === view.m && selP.d === d;
      const isToday = today.y === view.y && today.m === view.m && today.d === d;
      cells.push(h('button', {
        type: 'button', class: [isSel && 'sel', isToday && 'today'],
        onclick: () => {
          const hour = selP ? selP.hour : 0;
          const minute = selP ? selP.minute : 0;
          wrap.setDate(fromCalendar(view.y, view.m, d, hour, minute, tz, cal));
          pop.hidden = true;
          text.dispatchEvent(new Event('change'));
        },
      }, fmtNumber(d, 0)));
    }
    const step = (n) => {
      let m = view.m + n; let y = view.y;
      if (m < 1) { m = 12; y -= 1; } if (m > 12) { m = 1; y += 1; }
      view = { y, m }; renderPicker();
    };
    mount(pop,
      h('div', { class: 'dp-head' },
        button('‹', () => step(document.documentElement.dir === 'rtl' ? 1 : -1), { kind: 'ghost', size: 'sm', title: t('common.prev_month') }),
        h('span', null, `${monthName(view.m)} ${fmtNumber(view.y, 0).replace(/[٬,]/g, '')}`),
        button('›', () => step(document.documentElement.dir === 'rtl' ? -1 : 1), { kind: 'ghost', size: 'sm', title: t('common.next_month') })),
      h('div', { class: 'dp-grid' }, order.map((n) => h('span', { class: 'wd' }, n)), cells));
  }
  document.addEventListener('mousedown', (e) => { if (!wrap.contains(e.target)) pop.hidden = true; });
  wrap.setDate(initial);
  return wrap;
}

/** Parse a number typed by a person (Persian digits allowed). NaN when invalid. */
export function parseNumber(text) {
  const clean = normaliseDigits(text).trim();
  if (!/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(clean)) return NaN;
  return Number(clean);
}

export { fmtDate };

/** Site / area names: a translation when the i18n file has one, else the config segment. */
export function siteName(site) { return tMaybe(`sites.${site}`) || site; }
export function areaName(area) { return tMaybe(`areas.${area}`) || area; }
