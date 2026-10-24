// Device configuration: list, enable/disable, add, reload config/ from disk.

import { api } from '../api.js';
import { t } from '../i18n.js';
import {
  h, mount, table, button, stateBadge, protoBadge, notice, errorText, toast, confirmDialog,
  siteName, areaName, modal, select, field,
} from '../ui.js';
import { store, loadCatalogue } from '../store.js';

/** Starting points for a new device, one per protocol (validated by tests/frontend). */
export async function loadTemplates() {
  const r = await fetch('templates/devices.json', { cache: 'no-cache' });
  return r.json();
}

export async function render(page, { navigate, setTitle }) {
  setTitle(t('cfg.devices_title'));
  const box = h('div', { class: 'card flush' });

  function draw() {
    const list = [...store.devices.values()].sort((a, b) => a.device_id.localeCompare(b.device_id));
    mount(box, table([
      { label: t('cfg.device'), render: (d) => h('div', null, h('div', null, d.name), h('div', { class: 'tagpath' }, d.device_id)) },
      { label: t('cfg.protocol'), render: (d) => protoBadge(d.protocol) },
      { label: t('cfg.location'), render: (d) => `${siteName(d.site)} › ${areaName(d.area)}` },
      { label: t('cfg.state'), render: (d) => stateBadge(d.enabled ? d.state : 'disabled') },
      { label: '', cls: 'actions', render: (d) => h('div', { class: 'row end' },
        button(d.enabled ? t('cfg.disable') : t('cfg.enable'), async () => {
          if (d.enabled && !(await confirmDialog(t('cfg.disable'), t('cfg.disable_text', { name: d.name }), { danger: true }))) return;
          try {
            await api(`api/config/devices/${encodeURIComponent(d.device_id)}`, { method: 'PATCH', body: { enabled: !d.enabled } });
            toast(t('cfg.applied'), 'ok');
            await loadCatalogue(); draw();
          } catch (err) { toast(errorText(err), 'bad'); }
        }, { size: 'sm' }),
        button(t('common.edit'), () => navigate(`config/devices/${encodeURIComponent(d.device_id)}`), { size: 'sm', iconName: 'edit' })) },
    ], list, { emptyText: t('overview.no_devices') }));
  }

  const add = () => {
    const proto = select(['modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp', 'fins', 'mc', 'enip', 'iec104'].map((p) => ({ value: p, label: t(`protocol.${p}`) })), 'modbus_tcp');
    modal({
      title: t('cfg.new_device'),
      body: h('div', { class: 'stack' }, field(t('cfg.protocol'), proto), notice(t('cfg.new_hint'))),
      actions: [{ label: t('common.cancel') }, { label: t('cfg.continue'), kind: 'primary', onClick: () => navigate(`config/devices/new?protocol=${proto.value}`) }],
    });
  };

  const reload = async () => {
    try {
      const res = await api('api/config/reload', { method: 'POST' });
      await loadCatalogue();
      draw();
      toast(t('cfg.reloaded', { n: Object.values(res.changes || {}).filter(Array.isArray).reduce((n, a) => n + a.length, 0) }), 'ok');
    } catch (err) {
      const issues = err.body?.issues || [];
      modal({ title: t('cfg.reload_failed'), body: h('div', { class: 'stack' }, notice(errorText(err), 'bad'), issueList(issues)) });
    }
  };

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('cfg.devices_title')), h('div', { class: 'sub' }, t('cfg.devices_subtitle'))),
      h('div', { class: 'row' }, button(t('cfg.reload'), reload, { iconName: 'refresh' }), button(t('cfg.new_device'), add, { kind: 'primary', iconName: 'plus' }))),
    notice(t('cfg.note')),
    box);
  draw();
  return null;
}

export function issueList(issues) {
  if (!issues?.length) return null;
  return h('ul', { class: 'issues' }, issues.map((i) => h('li', { class: i.severity || 'ERROR' },
    h('span', { class: 'mono' }, `${i.source || ''}${i.location ? ` ${i.location}` : ''}`), ' ', i.message || i.msg || JSON.stringify(i))));
}
