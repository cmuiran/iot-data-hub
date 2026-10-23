// System status: hub process, HA nodes, storage, certificates, backups, notifications, events.

import { api } from '../api.js';
import { t, tMaybe, fmtDate, fmtInteger, fmtDuration, fmtAgo, fmtNumber } from '../i18n.js';
import { h, mount, table, button, badge, notice, errorText, toast, empty, confirmDialog } from '../ui.js';
import { store } from '../store.js';

function card(title, ...content) {
  return h('div', { class: 'card stack' }, h('h2', null, title), ...content);
}

async function section(box, title, load) {
  mount(box, card(title, empty(t('common.loading'))));
  try { mount(box, card(title, ...[await load()].flat())); } catch (err) { mount(box, card(title, notice(errorText(err), 'bad'))); }
}

export async function render(page, { setTitle }) {
  setTitle(t('system.title'));
  const boxes = Object.fromEntries(['hub', 'nodes', 'certs', 'backup', 'notify', 'events', 'config'].map((k) => [k, h('div')]));

  const loadHub = () => section(boxes.hub, t('system.hub'), async () => {
    const [st, health] = await Promise.all([api('api/system/stats'), api('api/health').catch((e) => e.body)]);
    const kv = (k, v) => [h('dt', null, k), h('dd', null, v)];
    return [
      h('div', { class: 'row' },
        badge(health?.status === 'ok' ? t('system.healthy') : t('system.degraded'), health?.status === 'ok' ? 'online' : 'error', true),
        badge(health?.database ? t('system.db_ok') : t('system.db_down'), health?.database ? 'online' : 'error'),
        badge(st.control_enabled ? t('system.control_on') : t('system.control_off'), st.control_enabled ? 'sev-medium' : 'unknown')),
      h('dl', { class: 'kv' },
        kv(t('system.version'), st.version), kv(t('system.node'), store.info?.node || ''),
        kv(t('system.started'), fmtDate(st.started_at)), kv(t('system.uptime'), fmtDuration(st.uptime_s)),
        kv(t('system.timezone'), store.info?.timezone || ''),
        kv(t('system.samples_received'), fmtInteger(st.samples_received)), kv(t('system.samples_written'), fmtInteger(st.samples_written)),
        kv(t('system.samples_dropped'), fmtInteger(st.samples_dropped)), kv(t('system.buffered'), fmtInteger(st.buffered)),
        kv(t('system.write_errors'), fmtInteger(st.write_errors)), kv(t('system.live_clients'), fmtInteger(st.live_subscribers))),
      st.last_write_error ? notice(`${t('system.last_write_error')}: ${st.last_write_error}`, 'bad') : null,
    ];
  });

  const loadNodes = () => section(boxes.nodes, t('system.nodes'), async () => {
    const nodes = await api('api/system/nodes');
    return table([
      { label: t('system.node'), render: (n) => h('span', { class: 'mono' }, n.node_id) },
      { label: t('system.host'), render: (n) => n.hostname },
      { label: t('system.role'), render: (n) => badge(tMaybe(`ha_role.${n.role}`) || n.role, n.role === 'active' ? 'online' : 'unknown') },
      { label: t('system.heartbeat'), render: (n) => h('span', { class: 'small' }, n.alive ? fmtAgo(n.last_heartbeat) : t('system.no_heartbeat')) },
      { label: t('system.version'), render: (n) => n.version },
    ], nodes, { emptyText: t('system.single_node') });
  });

  const loadCerts = () => section(boxes.certs, t('system.certs'), async () => {
    const certs = await api('api/system/certificates');
    return table([
      { label: t('system.cert_file'), render: (c) => h('span', { class: 'tagpath' }, c.file) },
      { label: t('system.cert_expires'), render: (c) => h('span', { class: 'small num' }, fmtDate(c.expires, 'date')) },
      { label: t('system.cert_days'), render: (c) => badge(t('system.days', { n: c.days_left }), c.days_left < 0 ? 'error' : c.days_left < 30 ? 'uncertain' : 'online') },
    ], certs, { emptyText: t('system.no_certs') });
  });

  const loadBackup = () => section(boxes.backup, t('system.backups'), async () => {
    const events = await api('api/system/events', { query: { limit: 200 } });
    const backups = events.filter((e) => e.kind.startsWith('backup'));
    return [
      table([
        { label: t('system.time'), render: (e) => h('span', { class: 'small num' }, fmtDate(e.ts)) },
        { label: t('system.result'), render: (e) => badge(e.kind === 'backup_ok' ? t('system.backup_ok') : t('system.backup_failed'), e.kind === 'backup_ok' ? 'online' : 'error') },
        { label: t('system.detail'), render: (e) => h('span', { class: 'tagpath' }, e.detail?.file || e.detail?.error || '') },
        { label: t('system.size'), render: (e) => (e.detail?.bytes ? `${fmtNumber(e.detail.bytes / 1048576, 1)} MB` : '') },
      ], backups.slice(0, 10), { emptyText: t('system.no_backups') }),
      h('div', { class: 'row' }, button(t('system.backup_now'), async () => {
        if (!(await confirmDialog(t('system.backup_now'), t('system.backup_now_text')))) return;
        try { const r = await api('api/system/backup', { method: 'POST' }); toast(t('system.backup_done', { file: r.file }), 'ok'); loadBackup(); } catch (err) { toast(errorText(err), 'bad'); }
      }, { iconName: 'download' })),
    ];
  });

  const loadNotify = () => section(boxes.notify, t('system.notifications'), async () => {
    const [cfg, log] = await Promise.all([api('api/system/config'), api('api/notifications/log', { query: { limit: 50 } }).catch(() => [])]);
    const channels = Object.entries(cfg.notifications?.channels || {});
    return [
      channels.length ? h('div', { class: 'stack tight' }, channels.map(([name, type]) => h('div', { class: 'row between' },
        h('span', null, h('span', { class: 'mono' }, name), ' ', badge(tMaybe(`channel.${type}`) || type, 'accent')),
        button(t('system.send_test'), async () => {
          try { await api(`api/notifications/test/${encodeURIComponent(name)}`, { method: 'POST' }); toast(t('system.test_sent'), 'ok'); loadNotify(); } catch (err) { toast(errorText(err), 'bad'); }
        }, { size: 'sm' })))) : empty(t('system.no_channels')),
      h('h3', null, t('system.notify_log')),
      table([
        { label: t('system.time'), render: (e) => h('span', { class: 'small num' }, fmtDate(e.ts)) },
        { label: t('system.channel'), render: (e) => h('span', { class: 'mono' }, e.channel) },
        { label: t('system.event'), render: (e) => `${e.event} · ${e.source}` },
        { label: t('system.result'), render: (e) => h('div', null, badge(tMaybe(`notify_status.${e.status}`) || e.status, e.status === 'sent' ? 'online' : e.status === 'failed' ? 'error' : 'uncertain'), e.error ? h('div', { class: 'tiny faint' }, e.error) : null) },
      ], log, { emptyText: t('system.no_log') }),
    ];
  });

  const loadEvents = () => section(boxes.events, t('system.events'), async () => {
    const events = await api('api/system/events', { query: { limit: 50 } });
    return table([
      { label: t('system.time'), render: (e) => h('span', { class: 'small num nowrap' }, fmtDate(e.ts)) },
      { label: t('system.kind'), render: (e) => tMaybe(`sys_event.${e.kind}`) || e.kind },
      { label: t('system.node'), render: (e) => h('span', { class: 'mono' }, e.node_id || '') },
      { label: t('system.detail'), render: (e) => h('pre', { class: 'pre' }, JSON.stringify(e.detail, null, 1)) },
    ], events, { emptyText: t('system.no_events') });
  });

  const loadConfig = () => section(boxes.config, t('system.config_issues'), async () => {
    const cfg = await api('api/system/config');
    if (!cfg.issues.length) return notice(t('system.config_clean'), 'ok');
    return h('ul', { class: 'issues' }, cfg.issues.map((i) => h('li', { class: i.severity }, h('span', { class: 'mono' }, `${i.source} ${i.location || ''}`), ' ', i.message)));
  });

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('system.title')), h('div', { class: 'sub' }, t('system.subtitle'))),
      button(t('common.refresh'), () => all(), { iconName: 'refresh' })),
    h('div', { class: 'grid wide' }, boxes.hub, boxes.nodes, boxes.config, boxes.certs),
    boxes.backup, boxes.notify, boxes.events);
  const all = () => { loadHub(); loadNodes(); loadCerts(); loadBackup(); loadNotify(); loadEvents(); loadConfig(); };
  all();
  return null;
}
