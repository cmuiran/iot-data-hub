// Control: writable tags, commands waiting for a second person, and the recent command log.

import { api } from '../api.js';
import { t, tMaybe, fmtDate } from '../i18n.js';
import {
  h, mount, table, button, valueNode, tagLabel, notice, errorText, toast, badge, modal, field,
  qualityBadge,
} from '../ui.js';
import { store, bus, canTag, can, deviceOfTag } from '../store.js';
import { openWriteDialog, resultBox } from '../control-dialog.js';

export async function render(page, { setTitle }) {
  setTitle(t('control.title'));
  const tagsBox = h('div', { class: 'card flush' });
  const pendingBox = h('div', { class: 'card flush' });
  const logBox = h('div', { class: 'card flush' });
  const valueCells = new Map();
  const users = new Map();

  const writable = () => [...store.tags.values()].filter((tag) => tag.access === 'read_write' && canTag('tag.write', tag.tag));

  function drawTags() {
    valueCells.clear();
    const list = writable();
    mount(tagsBox, h('div', { class: 'card-head' }, h('h2', null, t('control.writable_tags'))),
      table([
        { label: t('control.tag'), render: (tag) => h('div', null, h('div', null, tagLabel(tag)), h('div', { class: 'tagpath' }, tag.tag)) },
        { label: t('control.device'), render: (tag) => deviceOfTag(tag.tag)?.name || '' },
        { label: t('control.current'), cls: 'value', render: (tag) => { const c = h('span', null, valueNode(tag, tag.value)); valueCells.set(tag.tag, c); return c; } },
        { label: t('device.col_quality'), render: (tag) => qualityBadge(tag.quality) },
        { label: t('control.range'), render: (tag) => (tag.data_type === 'bool' ? '' : h('span', { class: 'mono small' }, `${tag.min ?? '−∞'} … ${tag.max ?? '+∞'}`)) },
        { label: '', cls: 'actions', render: (tag) => button(t('device.command'), () => openWriteDialog(tag.tag, { onResult: () => { loadPending(); loadLog(); } }), { size: 'sm', kind: 'primary', iconName: 'control' }) },
      ], list, { emptyText: t('control.no_writable') }));
  }

  async function decide(item, approve) {
    if (approve) {
      const tag = store.tags.get(item.tag_path);
      modal({
        title: t('control.confirm_title'),
        body: h('div', { class: 'stack' },
          h('div', { class: 'change' }, h('span', { class: 'old' }, tag ? String(tag.value) : '—'), h('span', { class: 'arrow' }, '→'), h('span', null, String(item.value))),
          h('p', null, t('control.confirm_text', { tag: tag ? tagLabel(tag) : item.tag_path, user: users.get(item.requested_by) || t('control.user_n', { id: item.requested_by }) })),
          item.reason ? h('p', { class: 'small muted' }, `${t('control.reason')}: ${item.reason}`) : null),
        actions: [{ label: t('common.cancel') }, {
          label: t('control.confirm_send'), kind: 'danger', closes: false,
          onClick: async ({ setActions, close }) => {
            const res = await api(`api/control/pending/${item.request_id}/confirm`, { method: 'POST' });
            close();
            modal({ title: t('control.result_title'), body: resultBox(res) });
            loadPending(); loadLog();
            return false;
          },
        }],
      });
      return;
    }
    const reason = h('textarea', { class: 'input', rows: 2, maxlength: 500 });
    modal({
      title: t('control.reject_title'),
      body: field(t('control.reason'), reason),
      actions: [{ label: t('common.cancel') }, {
        label: t('control.reject'), kind: 'primary',
        onClick: async () => {
          await api(`api/control/pending/${item.request_id}/reject`, { method: 'POST', body: { reason: reason.value.trim() || null } });
          toast(t('control.rejected'), 'ok');
          loadPending(); loadLog();
        },
      }],
    });
  }

  async function loadPending() {
    let list;
    try { list = await api('api/control/pending'); } catch (err) { mount(pendingBox, notice(errorText(err), 'bad')); return; }
    const mine = (item) => item.requested_by === store.me.user_id;
    mount(pendingBox, h('div', { class: 'card-head' }, h('h2', null, t('control.pending')), list.length ? badge(String(list.length), 'sev-medium') : null),
      table([
        { label: t('control.tag'), render: (i) => h('div', null, h('div', null, store.tags.get(i.tag_path) ? tagLabel(store.tags.get(i.tag_path)) : ''), h('div', { class: 'tagpath' }, i.tag_path)) },
        { label: t('control.requested_value'), cls: 'value', render: (i) => String(i.value) },
        { label: t('control.requested_by'), render: (i) => (mine(i) ? t('control.you') : users.get(i.requested_by) || t('control.user_n', { id: i.requested_by })) },
        { label: t('control.reason'), render: (i) => i.reason || '—' },
        { label: t('control.expires'), render: (i) => h('span', { class: 'small num', title: fmtDate(i.expires_at, 'datetimesec') }, fmtDate(i.expires_at, 'timesec')) },
        { label: '', cls: 'actions', render: (i) => h('div', { class: 'row end' },
          mine(i) ? h('span', { class: 'tiny faint' }, t('control.own_request')) : null,
          !mine(i) && canTag('write.confirm', i.tag_path) ? button(t('control.approve'), () => decide(i, true), { size: 'sm', kind: 'primary' }) : null,
          mine(i) || canTag('write.confirm', i.tag_path) ? button(mine(i) ? t('control.withdraw') : t('control.reject'), () => decide(i, false), { size: 'sm' }) : null) },
      ], list, { emptyText: t('control.no_pending') }));
  }

  async function loadLog() {
    if (!can('audit.read')) { logBox.hidden = true; return; }
    try {
      const rows = await api('api/audit/writes', { query: { limit: 50 } });
      mount(logBox, h('div', { class: 'card-head' }, h('h2', null, t('control.recent'))),
        table([
          { label: t('audit.time'), render: (r) => h('span', { class: 'small num', title: fmtDate(r.ts, 'datetimesec') }, fmtDate(r.ts, 'daytime')) },
          { label: t('audit.user'), render: (r) => r.username },
          { label: t('control.tag'), render: (r) => h('span', { class: 'tagpath' }, r.tag_path) },
          { label: t('audit.phase'), render: (r) => tMaybe(`write_phase.${r.phase}`) || r.phase },
          { label: t('audit.change'), cls: 'value', render: (r) => `${r.previous_value ?? '—'} → ${r.requested_value}${r.readback_value !== null ? ` (${r.readback_value})` : ''}` },
          { label: t('audit.result'), render: (r) => badge(tMaybe(`write_result.${r.result}`) || r.result, r.result === 'applied' ? 'online' : r.result.startsWith('rejected') || r.result === 'failed' ? 'error' : 'uncertain') },
          { label: t('control.reason'), render: (r) => r.reason || '' },
        ], rows, { emptyText: t('control.no_log') }));
    } catch (err) { mount(logBox, notice(errorText(err), 'bad')); }
  }

  if (can('user.manage')) {
    try { for (const u of await api('api/users')) users.set(u.user_id, u.display_name); } catch { /* names are optional */ }
  }

  mount(page,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, t('control.title')), h('div', { class: 'sub' }, t('control.subtitle')))),
    store.info?.control_enabled ? null : notice(t('control.disabled_hub_banner'), 'warn'),
    can('tag.write') ? tagsBox : null, pendingBox, logBox);
  if (can('tag.write')) drawTags();
  loadPending();
  loadLog();

  const onSamples = (e) => {
    for (const s of e.detail || []) {
      const c = valueCells.get(s.tag); const tag = store.tags.get(s.tag);
      if (c && tag) mount(c, valueNode(tag, tag.value));
    }
  };
  const onWrite = () => { loadPending(); loadLog(); };
  bus.addEventListener('samples', onSamples);
  bus.addEventListener('write', onWrite);
  const tick = setInterval(loadPending, 20_000);
  return () => {
    bus.removeEventListener('samples', onSamples);
    bus.removeEventListener('write', onWrite);
    clearInterval(tick);
  };
}

