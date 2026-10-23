// Jalali (Solar Hijri) <-> Gregorian conversion and plant-time-zone helpers.
// Pure functions; the algorithm is the one used by jalaali-js (Borkowski's), valid for
// Jalali years -61 .. 3177.

const BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192,
  2262, 2324, 2394, 2456, 3178];

function div(a, b) { return ~~(a / b); }
function mod(a, b) { return a - ~~(a / b) * b; }

function jalCal(jy) {
  let jp = BREAKS[0];
  let jump = 0;
  let leapJ = -14;
  if (jy < jp || jy >= BREAKS[BREAKS.length - 1]) throw new RangeError(`invalid Jalali year ${jy}`);
  let i;
  for (i = 1; i < BREAKS.length; i += 1) {
    const jm = BREAKS[i];
    jump = jm - jp;
    if (jy < jm) break;
    leapJ = leapJ + div(jump, 33) * 8 + div(mod(jump, 33), 4);
    jp = jm;
  }
  let n = jy - jp;
  leapJ = leapJ + div(n, 33) * 8 + div(mod(n, 33) + 3, 4);
  if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;
  const gy = jy + 621;
  const leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
  const march = 20 + leapJ - leapG;
  if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33;
  let leap = mod(mod(n + 1, 33) - 1, 4);
  if (leap === -1) leap = 4;
  return { leap, gy, march };
}

function g2d(gy, gm, gd) {
  let d = div((gy + div(gm - 8, 6) + 100100) * 1461, 4) + div(153 * mod(gm + 9, 12) + 2, 5) + gd - 34840408;
  d = d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752;
  return d;
}

function d2g(jdn) {
  let j = 4 * jdn + 139361631;
  j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
  const i = div(mod(j, 1461), 4) * 5 + 308;
  const gd = div(mod(i, 153), 5) + 1;
  const gm = mod(div(i, 153), 12) + 1;
  const gy = div(j, 1461) - 100100 + div(8 - gm, 6);
  return { gy, gm, gd };
}

function j2d(jy, jm, jd) {
  const r = jalCal(jy);
  return g2d(r.gy, 3, r.march) + (jm - 1) * 31 - div(jm, 7) * (jm - 7) + jd - 1;
}

function d2j(jdn) {
  const { gy } = d2g(jdn);
  let jy = gy - 621;
  const r = jalCal(jy);
  const jdn1f = g2d(gy, 3, r.march);
  let k = jdn - jdn1f;
  if (k >= 0) {
    if (k <= 185) return { jy, jm: 1 + div(k, 31), jd: mod(k, 31) + 1 };
    k -= 186;
  } else {
    jy -= 1;
    k += 179;
    if (r.leap === 1) k += 1;
  }
  return { jy, jm: 7 + div(k, 30), jd: mod(k, 30) + 1 };
}

export function toJalali(gy, gm, gd) { return d2j(g2d(gy, gm, gd)); }
export function toGregorian(jy, jm, jd) { return d2g(j2d(jy, jm, jd)); }
export function isLeapJalali(jy) { return jalCal(jy).leap === 0; }
export function jalaliMonthLength(jy, jm) {
  if (jm <= 6) return 31;
  if (jm <= 11) return 30;
  return isLeapJalali(jy) ? 30 : 29;
}
export function gregorianMonthLength(gy, gm) { return new Date(Date.UTC(gy, gm, 0)).getUTCDate(); }

// --- time zone ------------------------------------------------------------------------------

const partsCache = new Map();
function partsFormatter(tz) {
  if (!partsCache.has(tz)) {
    partsCache.set(tz, new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short',
    }));
  }
  return partsCache.get(tz);
}

const WEEKDAYS = { Sat: 0, Sun: 1, Mon: 2, Tue: 3, Wed: 4, Thu: 5, Fri: 6 };

/** Wall-clock fields of an instant in a time zone (Gregorian), weekday 0 = Saturday. */
export function zonedParts(date, tz) {
  const out = {};
  for (const p of partsFormatter(tz).formatToParts(date)) out[p.type] = p.value;
  return {
    year: Number(out.year), month: Number(out.month), day: Number(out.day),
    hour: Number(out.hour) % 24, minute: Number(out.minute), second: Number(out.second),
    weekday: WEEKDAYS[out.weekday],
  };
}

/** The UTC instant of a wall-clock time in a time zone (Gregorian fields). */
export function zonedToDate(year, month, day, hour = 0, minute = 0, tz = 'UTC') {
  const wanted = Date.UTC(year, month - 1, day, hour, minute);
  let guess = wanted;
  for (let i = 0; i < 3; i += 1) {
    const p = zonedParts(new Date(guess), tz);
    const seen = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
    const diff = wanted - seen;
    if (diff === 0) break;
    guess += diff;
  }
  return new Date(guess);
}

/** Start of the local day / month containing `date` in `tz`, in the given calendar. */
export function startOfDay(date, tz) {
  const p = zonedParts(date, tz);
  return zonedToDate(p.year, p.month, p.day, 0, 0, tz);
}

export function startOfMonth(date, tz, calendar) {
  const p = zonedParts(date, tz);
  if (calendar === 'persian') {
    const j = toJalali(p.year, p.month, p.day);
    const g = toGregorian(j.jy, j.jm, 1);
    return zonedToDate(g.gy, g.gm, g.gd, 0, 0, tz);
  }
  return zonedToDate(p.year, p.month, 1, 0, 0, tz);
}

export function addMonths(date, n, tz, calendar) {
  const p = zonedParts(date, tz);
  if (calendar === 'persian') {
    const j = toJalali(p.year, p.month, p.day);
    let jm = j.jm + n;
    let jy = j.jy + Math.floor((jm - 1) / 12);
    jm = ((jm - 1) % 12 + 12) % 12 + 1;
    const jd = Math.min(j.jd, jalaliMonthLength(jy, jm));
    const g = toGregorian(jy, jm, jd);
    return zonedToDate(g.gy, g.gm, g.gd, p.hour, p.minute, tz);
  }
  let gm = p.month + n;
  const gy = p.year + Math.floor((gm - 1) / 12);
  gm = ((gm - 1) % 12 + 12) % 12 + 1;
  const gd = Math.min(p.day, gregorianMonthLength(gy, gm));
  return zonedToDate(gy, gm, gd, p.hour, p.minute, tz);
}

/** Calendar date (in `calendar`) of an instant in `tz`: {y, m, d}. */
export function calendarDate(date, tz, calendar) {
  const p = zonedParts(date, tz);
  if (calendar === 'persian') {
    const j = toJalali(p.year, p.month, p.day);
    return { y: j.jy, m: j.jm, d: j.jd, hour: p.hour, minute: p.minute, weekday: p.weekday };
  }
  return { y: p.year, m: p.month, d: p.day, hour: p.hour, minute: p.minute, weekday: p.weekday };
}

/** Instant of a calendar date + time in `tz`. */
export function fromCalendar(y, m, d, hour, minute, tz, calendar) {
  if (calendar === 'persian') {
    const g = toGregorian(y, m, d);
    return zonedToDate(g.gy, g.gm, g.gd, hour, minute, tz);
  }
  return zonedToDate(y, m, d, hour, minute, tz);
}

export function monthLength(y, m, calendar) {
  return calendar === 'persian' ? jalaliMonthLength(y, m) : gregorianMonthLength(y, m);
}

/** Parse "1405/08/01", "1405-8-1" or "2026-10-23" (+ optional " 14:30"). Null when invalid. */
export function parseCalendarText(text, calendar) {
  const m = String(text).trim()
    .replace(/[۰-۹]/g, (c) => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
    .replace(/[٠-٩]/g, (c) => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)))
    .match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$/);
  if (!m) return null;
  const [y, mo, d, h, mi] = [m[1], m[2], m[3], m[4] ?? '0', m[5] ?? '0'].map(Number);
  if (mo < 1 || mo > 12 || d < 1 || d > monthLength(y, mo, calendar) || h > 23 || mi > 59) return null;
  return { y, m: mo, d, hour: h, minute: mi };
}
