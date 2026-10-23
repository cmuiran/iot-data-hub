// Saved dashboards: mine and shared ones; create a new one.

import { api } from '../api.js';
import { t, fmtDate } from '../i18n.js';
import { h, mount, table, button, badge, modal, field, input, checkbox, notice, errorText } from '../ui.js';
import { store } from '../store.js';

export function newLayout() {
  // a useful start: every device's state, the active alarms, nothing else
  const widgets = [...store.devices.values()].slice(0, 4).map((d, i) => ({ id: `w${i + 1}`, type: 'state', device: d.device_id, w: 3 }));
  widgets.push({ id: `w${widgets.length + 1}`, type: 'alarms', w: 12 });
  return { version: 1, widgets };
}

export async function render(page, { navigate, setTitle }) {
  setTitle(t('dash.list_title'));
  const box = h('div', { class: 'card flush' });

  async function load() {
    try {
      const list = await api('api/dashboards');
      mount(box, table([
        { label: t('dash.name'), render: (d) => h('a', { href: `#/dashboards/${d.dashboard_id}` }, d.name) },
        { label: t('dash.owner'), render: (d) => (d.owner_id === store.me.user_id ? t('dash.me') : d.owner) },
        { label: t('dash.flags'), render: (d) => h('div', { class: 'row' }, d.shared ? badge(t('dash.shared'), 'accent') : null, d.is_default && d.owner_id === store.me.user_id ? badge(t('dash.default'), 'online') : null) },
        { label: t('dash.updated'), render: (d) => h('span', { class: 'small num' }, fmtDate(d.updated_at)) },
      ], list, { emptyText: t('dash.none'), onRow: (d) => navigate(`dashboards/${d.dashboard_id}`) }));
    } catch (err) { mount(box, notice(errorText(err), 'bad')); }
  }

  const create = () => {
    const name = input({ maxlength: 100 });
    const shared = checkbox(t('dash.share'), false);
    const def = checkbox(t('dash.make_default'), false);
    modal({
      title: t('dash.new'),
      body: h('div', { class: 'stack' }, field(t('dash.name'), name), shared, def),
      actions: [{ label: t('common.cancel') }, {
        label: t('common.create'), kind: 'primary',
        onClick: async () => {
          if (!name.value.trim()) throw new Error(t('dash.name_required'));
          const d = await api('api/dashboards', { method: 'POST', body: { name: name.value.trim(), layout: newLayout(), shared: shared.input.checked, is_default: def.input.checked } });
          navigate(`dashboards/${d.dashboard_id}?edit=1`);
        },
      }],
    });
  };

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('dash.list_title')), h('div', { class: 'sub' }, t('dash.list_subtitle'))),
      button(t('dash.new'), create, { kind: 'primary', iconName: 'plus' })),
    box);
  load();
  return null;
}
