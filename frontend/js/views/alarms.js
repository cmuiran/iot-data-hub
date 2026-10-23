// Alarms: active list (acknowledge, shelve), shelved alarms, history, and annunciator settings.

import { api } from '../api.js';
import { t, fmtDate, fmtAgo, fmtNumber } from '../i18n.js';
import {
  h, mount, tabs, table, severityBadge, badge, button, modal, field, input, select, notice,
  errorText, toast, checkbox, empty, valueText, unitText,
} from '../ui.js';
import { store, bus, can, severityRank, loadAlarms, deviceOfTag } from '../store.js';
import { annunciator } from '../alerts.js';

function scopeOf(alarm) {
  if (alarm.tag_path) { const [site, area] = alarm.tag_path.split('.'); return { site, area }; }
  const d = alarm.device_id ? store.devices.get(alarm.device_id) : null;
  return d ? { site: d.site, area: d.area } : { site: null, area: null };
}

function mayAck(alarm) {
  const { site, area } = scopeOf(alarm);
  return site ? can('alarm.ack', site, area) : can('alarm.ack');
}

function sourceText(alarm) {
  if (alarm.tag_path) {
    return h('div', null, h('div', null, deviceOfTag(alarm.tag_path)?.name || ''), h('div', { class: 'tagpath' }, alarm.tag_path));
  }
  if (alarm.device_id) return h('div', null, h('div', null, store.devices.get(alarm.device_id)?.name || alarm.device_id), h('div', { class: 'tagpath' }, alarm.source));
  return h('div', { class: 'tagpath' }, alarm.source);
}

/** The value that raised the alarm, formatted like the tag (stored as text). */
function alarmValue(alarm) {
  const tag = alarm.tag_path ? store.tags.get(alarm.tag_path) : null;
  if (alarm.value === 'true' || alarm.value === 'false') return valueText(tag, alarm.value === 'true');
  const n = Number(alarm.value);
  if (alarm.value !== '' && Number.isFinite(n)) return `${fmtNumber(n)} ${unitText(tag?.unit)}`.trim();
  return alarm.value;
}

function stateText(alarm) {
  if (alarm.shelved) return badge(t('alarms.state_shelved'), 'unknown');
  if (!alarm.cleared_at && !alarm.acked_at) return badge(t('alarms.state_active_unacked'), 'error');
  if (!alarm.cleared_at) return badge(t('alarms.state_active_acked'), 'uncertain');
  return badge(t('alarms.state_cleared_unacked'), 'accent');
}

export function ackDialog(alarm, onDone) {
  const comment = h('textarea', { class: 'input', rows: 2, maxlength: 500 });
  modal({
    title: t('alarms.ack_title'),
    body: h('div', { class: 'stack' }, h('p', null, alarm.message), field(t('alarms.comment'), comment)),
    actions: [{ label: t('common.cancel') }, {
      label: t('alarms.ack'), kind: 'primary',
      onClick: async () => {
        await api(`api/alarms/${alarm.alarm_id}/ack`, { method: 'POST', body: { comment: comment.value.trim() || null } });
        toast(t('alarms.acked'), 'ok');
        await loadAlarms();
        onDone?.();
      },
    }],
  });
}

function shelveDialog(alarm) {
  const minutes = select([15, 30, 60, 120, 240, 480, 1440].map((m) => ({ value: m, label: t('alarms.minutes', { n: m }) })), 60);
  const reason = h('textarea', { class: 'input', rows: 2, maxlength: 500, required: true });
  modal({
    title: t('alarms.shelve_title'),
    body: h('div', { class: 'stack' }, notice(t('alarms.shelve_note'), 'warn'), h('p', null, alarm.message),
      h('div', { class: 'tagpath' }, alarm.source),
      field(t('alarms.shelve_for'), minutes), field(t('alarms.reason'), reason)),
    actions: [{ label: t('common.cancel') }, {
      label: t('alarms.shelve'), kind: 'primary',
      onClick: async () => {
        if (reason.value.trim().length < 3) throw new Error(t('alarms.reason_required'));
        await api('api/alarms/shelve', { method: 'POST', body: { source: alarm.source, minutes: Number(minutes.value), reason: reason.value.trim() } });
        toast(t('alarms.shelved'), 'ok');
        await loadAlarms();
      },
    }],
  });
}

export async function render(page, { setTitle }) {
  setTitle(t('alarms.title'));
  let tab = 'active';
  let history = null;
  const tabBar = h('div');
  const body = h('div', { class: 'card flush' });
  const settings = h('div', { class: 'card' });

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('alarms.title')), h('div', { class: 'sub' }, t('alarms.subtitle')))),
    tabBar, body, settings);

  function counts() {
    const active = store.alarms.filter((a) => !a.shelved);
    return { active: active.length, shelved: store.alarms.filter((a) => a.shelved).length };
  }

  function drawTabs() {
    const c = counts();
    mount(tabBar, tabs([
      { id: 'active', label: t('alarms.tab_active'), count: c.active },
      { id: 'shelved', label: t('alarms.tab_shelved'), count: c.shelved },
      { id: 'history', label: t('alarms.tab_history') },
    ], tab, (id) => { tab = id; draw(); }));
  }

  const cols = (withActions) => [
    { label: t('alarms.col_severity'), render: (a) => severityBadge(a.severity) },
    { label: t('alarms.col_message'), render: (a) => h('div', null, h('div', null, a.message), a.value !== null ? h('div', { class: 'tiny faint' }, `${t('alarms.value')}: ${alarmValue(a)}`) : null) },
    { label: t('alarms.col_source'), render: sourceText },
    { label: t('alarms.col_raised'), render: (a) => h('span', { class: 'small num', title: fmtDate(a.raised_at, 'datetimesec') }, fmtDate(a.raised_at, 'daytime'), h('div', { class: 'tiny faint' }, fmtAgo(a.raised_at))) },
    { label: t('alarms.col_state'), render: (a) => h('div', { class: 'stack tight' }, stateText(a),
      a.shelved ? h('span', { class: 'tiny faint' }, t('alarms.shelved_until', { time: fmtDate(a.shelved.until, 'daytime'), by: a.shelved.shelved_by })) : null,
      a.acked_at ? h('span', { class: 'tiny faint' }, t('alarms.acked_at', { time: fmtDate(a.acked_at, 'daytime') })) : null,
      a.cleared_at ? h('span', { class: 'tiny faint' }, t('alarms.cleared_at', { time: fmtDate(a.cleared_at, 'daytime') })) : null) },
    withActions ? { label: '', cls: 'actions', render: (a) => h('div', { class: 'row end' },
      !a.acked_at && mayAck(a) ? button(t('alarms.ack'), () => ackDialog(a), { size: 'sm', kind: 'primary' }) : null,
      !a.shelved && mayAck(a) && a.source && !a.source.startsWith('system:') ? button(t('alarms.shelve'), () => shelveDialog(a), { size: 'sm' }) : null,
      a.shelved && mayAck(a) ? button(t('alarms.unshelve'), async () => {
        try { await api(`api/alarms/shelve/${encodeURIComponent(a.source)}`, { method: 'DELETE' }); await loadAlarms(); } catch (err) { toast(errorText(err), 'bad'); }
      }, { size: 'sm' }) : null) } : null,
  ].filter(Boolean);

  const rowClass = (a) => ['alarm-row', `sev-${a.severity}`, !a.acked_at && 'unacked', a.cleared_at && 'cleared'].filter(Boolean).join(' ');

  async function draw() {
    drawTabs();
    if (tab === 'active') {
      const list = store.alarms.filter((a) => !a.shelved).sort((a, b) =>
        (Boolean(a.acked_at) - Boolean(b.acked_at)) || (severityRank(b.severity) - severityRank(a.severity)) || b.raised_at.localeCompare(a.raised_at));
      const ackable = list.filter((a) => !a.acked_at && mayAck(a));
      mount(body,
        h('div', { class: 'card-head' }, h('h2', null, t('alarms.tab_active')),
          ackable.length > 1 ? button(t('alarms.ack_all', { n: ackable.length }), async () => {
            let ok = 0;
            for (const a of ackable) { try { await api(`api/alarms/${a.alarm_id}/ack`, { method: 'POST', body: { comment: null } }); ok += 1; } catch { /* reported below */ } }
            toast(t('alarms.acked_n', { n: ok }), ok === ackable.length ? 'ok' : 'bad');
            await loadAlarms();
          }, { size: 'sm' }) : null),
        list.length ? table(cols(true), list, { rowClass }) : empty(t('alarms.none')));
    } else if (tab === 'shelved') {
      const list = store.alarms.filter((a) => a.shelved);
      mount(body, list.length ? table(cols(true), list, { rowClass }) : empty(t('alarms.none_shelved')));
    } else {
      mount(body, empty(t('common.loading')));
      try {
        history = await api('api/alarms/history', { query: { limit: 500 } });
        if (tab !== 'history') return;
        mount(body, table(cols(false), history, { rowClass, emptyText: t('alarms.no_history') }));
      } catch (err) {
        mount(body, notice(errorText(err), 'bad'));
      }
    }
  }

  function drawSettings() {
    const sound = checkbox(t('alarms.sound_enable'), annunciator.prefs.sound, { onchange: (e) => { annunciator.setPrefs({ sound: e.target.checked }); if (e.target.checked) annunciator.unlock(); } });
    const min = select(['low', 'medium', 'high', 'critical'].map((s) => ({ value: s, label: t(`severity.${s}`) })), annunciator.prefs.minSeverity,
      { onchange: (e) => annunciator.setPrefs({ minSeverity: e.target.value }) });
    const supported = 'Notification' in window;
    const notifyState = supported ? (annunciator.prefs.notify && Notification.permission === 'granted' ? 'on' : Notification.permission === 'denied' ? 'denied' : 'off') : 'unsupported';
    mount(settings,
      h('div', { class: 'card-head' }, h('h2', null, t('alarms.settings'))),
      h('div', { class: 'form-grid' },
        h('div', { class: 'stack tight' }, sound, h('span', { class: 'hint tiny faint' }, t('alarms.sound_hint'))),
        field(t('alarms.min_severity'), min),
        h('div', { class: 'stack tight' },
          h('span', { class: 'small' }, t(`alarms.notify_${notifyState}`)),
          notifyState === 'off' ? button(t('alarms.notify_enable'), async () => { await annunciator.enableNotifications(); drawSettings(); }, { size: 'sm' }) : null,
          notifyState === 'on' ? button(t('alarms.notify_disable'), () => { annunciator.setPrefs({ notify: false }); drawSettings(); }, { size: 'sm' }) : null),
        h('div', { class: 'stack tight' }, button(t('alarms.test_sound'), () => { annunciator.unlock(); annunciator.horn(); }, { size: 'sm', iconName: 'sound' }))));
  }

  const onAlarms = () => { if (tab !== 'history') draw(); else drawTabs(); };
  bus.addEventListener('alarms', onAlarms);
  draw();
  drawSettings();
  return () => bus.removeEventListener('alarms', onAlarms);
}

export { mayAck };
