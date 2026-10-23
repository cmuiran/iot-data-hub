// Translations, direction and locale-aware formatting. All user-facing text goes through t().

import { calendarDate } from './jalali.js';

export const LANGUAGES = ['fa', 'en'];
const state = { lang: 'fa', dict: {}, fallback: {}, tz: 'Asia/Tehran' };
const formatters = new Map();

function lookup(dict, key) {
  let node = dict;
  for (const part of key.split('.')) {
    if (node == null || typeof node !== 'object') return undefined;
    node = node[part];
  }
  return typeof node === 'string' ? node : undefined;
}

async function fetchDict(lang) {
  const r = await fetch(`i18n/${lang}.json`, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`cannot load language ${lang}`);
  return r.json();
}

export async function setLanguage(lang) {
  const chosen = LANGUAGES.includes(lang) ? lang : 'fa';
  state.dict = await fetchDict(chosen);
  if (chosen !== 'en' && !Object.keys(state.fallback).length) {
    try { state.fallback = await fetchDict('en'); } catch { state.fallback = {}; }
  }
  state.lang = chosen;
  formatters.clear();
  const root = document.documentElement;
  root.lang = chosen;
  root.dir = chosen === 'fa' ? 'rtl' : 'ltr';
  try { localStorage.setItem('hub.lang', chosen); } catch { /* storage may be blocked */ }
}

export function storedLanguage() {
  try { return localStorage.getItem('hub.lang'); } catch { return null; }
}

export function getLanguage() { return state.lang; }
export function isRtl() { return state.lang === 'fa'; }
export function calendar() { return state.lang === 'fa' ? 'persian' : 'gregory'; }
export function setTimeZone(tz) { if (tz) { state.tz = tz; formatters.clear(); } }
export function timeZone() { return state.tz; }

/** t('alarms.ack_done', {n: 3}) -> text with {n} replaced; the key itself when missing. */
export function t(key, vars) {
  let text = lookup(state.dict, key) ?? lookup(state.fallback, key);
  if (text === undefined) {
    if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
      console.warn(`missing translation: ${key}`);
    }
    text = key;
  }
  if (vars) {
    text = text.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? fmtAny(vars[name]) : m));
  }
  return text;
}

/** Translation when the key exists, otherwise null (for optional labels like error codes). */
export function tMaybe(key, vars) {
  const text = lookup(state.dict, key) ?? lookup(state.fallback, key);
  return text === undefined ? null : t(key, vars);
}

function fmtAny(v) { return typeof v === 'number' ? fmtNumber(v) : String(v); }

function locale() { return state.lang === 'fa' ? 'fa-IR' : 'en-GB'; }

function cached(key, make) {
  if (!formatters.has(key)) formatters.set(key, make());
  return formatters.get(key);
}

export function fmtNumber(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const opts = digits === undefined
    ? { maximumFractionDigits: Math.abs(value) >= 1000 ? 1 : Math.abs(value) >= 10 ? 2 : 3 }
    : { minimumFractionDigits: digits, maximumFractionDigits: digits };
  const f = cached(`n${JSON.stringify(opts)}`, () => new Intl.NumberFormat(locale(), { ...opts, useGrouping: true }));
  return f.format(value);
}

export function fmtInteger(value) { return fmtNumber(value, 0); }

function dtf(kind) {
  const base = { timeZone: state.tz, calendar: calendar(), numberingSystem: state.lang === 'fa' ? 'arabext' : 'latn' };
  const opts = {
    datetime: { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' },
    datetimesec: { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' },
    date: { year: 'numeric', month: '2-digit', day: '2-digit' },
    longdate: { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' },
    month: { year: 'numeric', month: 'long' },
    time: { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' },
    timesec: { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' },
    daytime: { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' },
    monthname: { month: 'long' },
  }[kind];
  return cached(`d${kind}`, () => new Intl.DateTimeFormat(`${locale()}-u-ca-${calendar()}`, { ...base, ...opts }));
}

/** Format an ISO string / Date in the plant's time zone and the language's calendar. */
export function fmtDate(value, kind = 'datetime') {
  if (!value) return '—';
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  return dtf(kind).format(d);
}

export function monthName(m) {
  // any date inside month m of the active calendar
  if (calendar() === 'persian') {
    const names = t('calendar.persian_months').split(',');
    return names[m - 1] ?? String(m);
  }
  const names = t('calendar.gregorian_months').split(',');
  return names[m - 1] ?? String(m);
}

export function weekdayNames() { return t('calendar.weekdays_short').split(','); }

/** "3 min ago" style text. */
export function fmtAgo(value) {
  if (!value) return '—';
  const s = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (s < 5) return t('time.just_now');
  if (s < 60) return t('time.seconds_ago', { n: s });
  if (s < 3600) return t('time.minutes_ago', { n: Math.round(s / 60) });
  if (s < 86400) return t('time.hours_ago', { n: Math.round(s / 3600) });
  return fmtDate(value);
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return t('time.seconds', { n: s });
  if (s < 3600) return t('time.minutes', { n: Math.round(s / 60) });
  if (s < 86400) return t('time.hours', { n: fmtNumber(s / 3600, 1) });
  return t('time.days', { n: fmtNumber(s / 86400, 1) });
}

/** Calendar fields of an instant in the plant time zone (for pickers). */
export function localParts(date) { return calendarDate(date, state.tz, calendar()); }

/** Digits typed by the user may be Persian; normalise before parsing numbers. */
export function normaliseDigits(text) {
  return String(text)
    .replace(/[۰-۹]/g, (c) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
    .replace(/[٠-٩]/g, (c) => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)))
    .replace(/٫/g, '.').replace(/[٬,]/g, '');
}
