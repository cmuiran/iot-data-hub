// Edit (or create) one device as JSON: check against the schema and the rest of the config,
// then save. The server validates again, writes the YAML file and restarts only that driver.

import { api } from '../api.js';
import { t } from '../i18n.js';
import { h, mount, button, notice, errorText, toast, confirmDialog, empty } from '../ui.js';
import { loadCatalogue } from '../store.js';
import { loadTemplates, issueList } from './config-devices.js';

export async function render(page, { params, query, navigate, setTitle }) {
  const isNew = params.id === 'new';
  let data;
  try {
    if (isNew) {
      const templates = await loadTemplates();
      data = templates[query.get('protocol')] || templates.modbus_tcp;
    } else {
      data = await api(`api/config/devices/${encodeURIComponent(params.id)}`);
    }
  } catch (err) { mount(page, notice(errorText(err), 'bad')); return null; }
  setTitle(isNew ? t('cfg.new_device') : t('cfg.edit_title', { id: params.id }));

  const editor = h('textarea', { class: 'input code', spellcheck: 'false', 'aria-label': t('cfg.json') });
  editor.value = JSON.stringify(data, null, 2);
  const result = h('div', { class: 'stack tight' });
  let dirty = false;
  editor.addEventListener('input', () => { dirty = true; });
  editor.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {              // indent instead of leaving the field
      e.preventDefault();
      const { selectionStart: a, selectionEnd: b } = editor;
      editor.setRangeText('  ', a, b, 'end');
    }
  });

  const parse = () => {
    try { return JSON.parse(editor.value); } catch (err) {
      mount(result, notice(t('cfg.json_error', { msg: err.message }), 'bad'));
      return null;
    }
  };

  const showErrors = (errors, issues) => mount(result,
    errors?.length ? h('ul', { class: 'issues' }, errors.map((e) => h('li', { class: 'ERROR' }, h('span', { class: 'mono' }, (e.loc || []).join('.')), ' ', e.msg))) : null,
    issueList(issues));

  async function check() {
    const body = parse();
    if (!body) return false;
    try {
      const res = await api('api/config/devices/validate', { method: 'POST', body });
      if (res.valid) {
        mount(result, notice(t('cfg.valid'), 'ok'), issueList(res.issues));
        return true;
      }
      showErrors(res.errors, res.issues);
      result.prepend(notice(t('cfg.invalid'), 'bad'));
      return false;
    } catch (err) { mount(result, notice(errorText(err), 'bad')); return false; }
  }

  async function save() {
    const body = parse();
    if (!body) return;
    try {
      const res = isNew
        ? await api('api/config/devices', { method: 'POST', body })
        : await api(`api/config/devices/${encodeURIComponent(params.id)}`, { method: 'PUT', body });
      dirty = false;
      await loadCatalogue();
      toast(t('cfg.saved'), 'ok');
      mount(result, notice(t('cfg.saved_detail'), 'ok'), issueList(res.warnings));
      if (isNew) navigate(`config/devices/${encodeURIComponent(body.device_id)}`);
    } catch (err) {
      showErrors(err.body?.errors, err.body?.issues);
      result.prepend(notice(errorText(err), 'bad'));
    }
  }

  async function remove() {
    if (!(await confirmDialog(t('cfg.delete'), t('cfg.delete_text', { id: params.id }), { danger: true, typeToConfirm: params.id, confirmLabel: t('cfg.delete') }))) return;
    try {
      await api(`api/config/devices/${encodeURIComponent(params.id)}`, { method: 'DELETE' });
      await loadCatalogue();
      toast(t('cfg.deleted'), 'ok');
      navigate('config/devices');
    } catch (err) { toast(errorText(err), 'bad'); }
  }

  mount(page,
    h('div', { class: 'row' }, h('a', { href: '#/config/devices' }, `‹ ${t('nav.config_devices')}`)),
    h('div', { class: 'page-head' },
      h('div', null, h('h1', null, isNew ? t('cfg.new_device') : t('cfg.edit_title', { id: params.id })), h('div', { class: 'sub' }, t('cfg.editor_hint'))),
      h('div', { class: 'row' },
        !isNew ? button(t('cfg.delete'), remove, { kind: 'danger', iconName: 'trash' }) : null,
        button(t('cfg.check'), check, { iconName: 'check' }),
        button(isNew ? t('common.create') : t('common.save'), save, { kind: 'primary' }))),
    h('div', { class: 'split' },
      h('div', { class: 'card stack' }, editor),
      h('div', { class: 'stack' },
        h('div', { class: 'card stack' }, h('h2', null, t('cfg.result')), result),
        h('div', { class: 'card stack small' }, h('h2', null, t('cfg.help_title')),
          h('p', null, t('cfg.help_1')), h('p', null, t('cfg.help_2')), h('p', null, t('cfg.help_3'))))));
  mount(result, empty(t('cfg.not_checked')));

  const beforeUnload = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } };
  window.addEventListener('beforeunload', beforeUnload);
  return () => window.removeEventListener('beforeunload', beforeUnload);
}
