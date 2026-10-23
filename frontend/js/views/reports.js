// Period reports (hourly / daily / monthly) over several tags, in the plant calendar,
// with Excel and CSV export.

import { api, download } from '../api.js';
import { t, fmtDate, fmtNumber, timeZone, calendar } from '../i18n.js';
import {
  h, mount, button, select, field, input, notice, errorText, dateTimeInput, tagLabel, table,
  segmented, unitText, toast, empty,
} from '../ui.js';
import { store, isNumeric } from '../store.js';
import { startOfDay, startOfMonth, addMonths } from '../jalali.js';

const FUNCTIONS = ['avg', 'min', 'max', 'last', 'delta'];
const KEY = 'hub.report_columns';

function guessFunction(tag) {
  return /energy|total|hours|count|volume/.test(tag.name) ? 'delta' : 'avg';
}

export async function render(page, { setTitle }) {
  setTitle(t('reports.title'));
  let columns = [];
  try { columns = JSON.parse(localStorage.getItem(KEY) || '[]').filter((c) => store.tags.has(c.tag)); } catch { columns = []; }
  let preset = 'today';
  let interval = 'hour';
  let result = null;

  const colBox = h('div', { class: 'stack tight' });
  const title = input({ maxlength: 200, placeholder: t('reports.title_placeholder') });
  const startIn = dateTimeInput(startOfDay(new Date(), timeZone()), { withTime: false });
  const endIn = dateTimeInput(new Date(), { withTime: false });
  const custom = h('div', { class: 'row' }, field(t('reports.from'), startIn), field(t('reports.to_exclusive'), endIn));
  const out = h('div', { class: 'card flush' });

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(columns)); } catch { /* ignore */ } };

  function drawColumns() {
    const numeric = [...store.tags.values()].filter(isNumeric);
    const picker = select([{ value: '', label: t('reports.add_column') }, ...numeric.map((tag) => ({ value: tag.tag, label: `${tagLabel(tag)} — ${tag.tag}` }))], '');
    picker.addEventListener('change', () => {
      const tag = store.tags.get(picker.value);
      if (!tag) return;
      columns.push({ tag: tag.tag, function: guessFunction(tag), label: '' });
      save(); drawColumns();
    });
    mount(colBox,
      columns.length ? columns.map((c, i) => {
        const tag = store.tags.get(c.tag);
        const fn = select(FUNCTIONS.map((f) => ({ value: f, label: t(`reports.fn_${f}`) })), c.function, { onchange: (e) => { c.function = e.target.value; save(); } });
        const label = input({ value: c.label || '', placeholder: tagLabel(tag), maxlength: 100, oninput: (e) => { c.label = e.target.value; save(); } });
        return h('div', { class: 'row' },
          h('div', { class: 'grow' }, h('div', null, tagLabel(tag)), h('div', { class: 'tagpath' }, c.tag)),
          h('div', { style: { width: '150px' } }, fn), h('div', { style: { width: '200px' } }, label),
          button('', () => { columns.splice(i, 1); save(); drawColumns(); }, { kind: 'ghost', iconName: 'trash', title: t('common.remove') }));
      }) : h('p', { class: 'small faint' }, t('reports.no_columns')),
      h('div', { style: { maxWidth: '520px' } }, picker));
  }

  function period() {
    const now = new Date();
    const tz = timeZone();
    const cal = calendar();
    switch (preset) {
      case 'today': return { start: startOfDay(now, tz), end: now, interval: 'hour' };
      case 'yesterday': { const end = startOfDay(now, tz); return { start: startOfDay(new Date(end.getTime() - 12 * 3600e3), tz), end, interval: 'hour' }; }
      case '7d': return { start: startOfDay(new Date(now.getTime() - 6 * 86400e3), tz), end: now, interval: 'day' };
      case 'month': return { start: startOfMonth(now, tz, cal), end: now, interval: 'day' };
      case 'last_month': { const end = startOfMonth(now, tz, cal); return { start: addMonths(end, -1, tz, cal), end, interval: 'day' }; }
      case 'year': return { start: addMonths(startOfMonth(now, tz, cal), -11, tz, cal), end: now, interval: 'month' };
      default: return { start: startIn.getDate(), end: endIn.getDate(), interval };
    }
  }

  function requestBody() {
    const p = period();
    if (!columns.length) throw new Error(t('reports.no_columns'));
    if (!p.start || !p.end || p.start >= p.end) throw new Error(t('reports.bad_range'));
    return {
      columns: columns.map((c) => ({ tag: c.tag, function: c.function, label: c.label || `${tagLabel(store.tags.get(c.tag))} (${t(`reports.fn_${c.function}`)})` })),
      start: p.start.toISOString(), end: p.end.toISOString(), interval: p.interval, title: title.value.trim() || null,
    };
  }

  function periodLabel(iso, iv) {
    if (iv === 'month') return fmtDate(iso, 'month');
    if (iv === 'day') return fmtDate(iso, 'date');
    return fmtDate(iso, 'daytime');
  }

  async function run() {
    let body;
    try { body = requestBody(); } catch (err) { mount(out, notice(err.message, 'warn')); return; }
    mount(out, empty(t('common.loading')));
    try {
      result = await api('api/reports', { method: 'POST', body });
      drawResult();
    } catch (err) { mount(out, notice(errorText(err), 'bad')); }
  }

  function drawResult() {
    const cols = [{ label: t('reports.period'), render: (i) => h('span', { class: 'num nowrap' }, periodLabel(result.periods[i], result.interval)) },
      ...result.columns.map((c) => ({ label: `${c.label}${c.unit ? ` [${unitText(c.unit)}]` : ''}`, cls: 'value', render: (i) => fmtNumber(c.values[i]) }))];
    const rows = result.periods.map((_, i) => i);
    const hasTotal = result.columns.some((c) => c.total !== null);
    mount(out,
      h('div', { class: 'card-head' }, h('h2', null, result.title || t('reports.result')),
        h('span', { class: 'small muted' }, `${fmtDate(result.start, 'date')} → ${fmtDate(result.end, 'date')} · ${t(`reports.iv_${result.interval}`)}`)),
      table(cols, rows, { emptyText: t('reports.empty') }),
      hasTotal ? h('div', { class: 'row end', style: { padding: '10px 16px' } }, result.columns.filter((c) => c.total !== null).map((c) =>
        h('span', { class: 'badge accent' }, `${t('reports.total')} ${c.label}: ${fmtNumber(c.total)} ${unitText(c.unit)}`))) : null);
  }

  async function exportAs(format) {
    try { await download('api/reports/export', { method: 'POST', body: requestBody(), query: { format } }, `report.${format}`); } catch (err) { toast(errorText(err) || err.message, 'bad'); }
  }

  const intervalSel = select(['hour', 'day', 'month'].map((v) => ({ value: v, label: t(`reports.iv_${v}`) })), interval, { onchange: (e) => { interval = e.target.value; } });
  const intervalField = field(t('reports.interval'), intervalSel);
  const syncPreset = () => { custom.hidden = preset !== 'custom'; intervalField.hidden = preset !== 'custom'; };

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('reports.title')), h('div', { class: 'sub' }, t('reports.subtitle')))),
    h('div', { class: 'card stack' },
      h('h2', null, t('reports.columns')), colBox),
    h('div', { class: 'card stack' },
      h('h2', null, t('reports.period_title')),
      segmented(['today', 'yesterday', '7d', 'month', 'last_month', 'year', 'custom'].map((v) => ({ value: v, label: t(`reports.p_${v}`) })), preset, (v) => { preset = v; syncPreset(); }),
      h('div', { class: 'row top' }, custom, intervalField),
      field(t('reports.report_title'), title),
      h('div', { class: 'row' }, button(t('reports.run'), run, { kind: 'primary', iconName: 'play' }),
        button(t('reports.export_xlsx'), () => exportAs('xlsx'), { iconName: 'download' }),
        button(t('reports.export_csv'), () => exportAs('csv'), { iconName: 'download' }))),
    out);
  drawColumns();
  syncPreset();
  mount(out, empty(t('reports.hint')));
  return null;
}
