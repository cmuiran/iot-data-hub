// Entry point: language, theme, session, app shell and the hash router.

import { api, authEvents, hasSession, logout } from './api.js';
import { setLanguage, storedLanguage, setTimeZone, t, getLanguage, fmtInteger } from './i18n.js';
import { h, mount, icon, button, errorText, toast, clear } from './ui.js';
import {
  store, bus, can, loadCatalogue, loadAlarms, startLive, stopLive, resetStore, unackedAlarms,
} from './store.js';
import { annunciator } from './alerts.js';
import { applyTheme } from './theme.js';
import { renderAuth } from './views/auth.js';

const ROUTES = [
  { path: 'overview', view: () => import('./views/overview.js'), perm: 'data.read' },
  { path: 'device/:id', view: () => import('./views/device.js'), perm: 'data.read' },
  { path: 'alarms', view: () => import('./views/alarms.js'), perm: 'data.read' },
  { path: 'trends', view: () => import('./views/trends.js'), perm: 'data.read' },
  { path: 'reports', view: () => import('./views/reports.js'), perm: 'data.read' },
  { path: 'dashboards', view: () => import('./views/dashboards.js'), perm: 'data.read' },
  { path: 'dashboards/:id', view: () => import('./views/dashboard.js'), perm: 'data.read' },
  { path: 'building', view: () => import('./views/building.js'), perm: 'data.read' },
  { path: 'calc', view: () => import('./views/calc.js'), perm: 'data.read' },
  { path: 'control', view: () => import('./views/control.js'), perm: ['tag.write', 'control.setpoint', 'control.command', 'write.confirm'] },
  { path: 'schedules', view: () => import('./views/schedules.js'), perm: ['tag.write', 'control.setpoint', 'control.command'] },
  { path: 'users', view: () => import('./views/users.js'), perm: 'user.manage' },
  { path: 'config/devices', view: () => import('./views/config-devices.js'), perm: 'config.edit' },
  { path: 'config/devices/:id', view: () => import('./views/config-device.js'), perm: 'config.edit' },
  { path: 'config/rules', view: () => import('./views/config-rules.js'), perm: 'config.edit' },
  { path: 'audit', view: () => import('./views/audit.js'), perm: 'audit.read' },
  { path: 'system', view: () => import('./views/system.js'), perm: 'config.edit' },
  { path: 'account', view: () => import('./views/account.js'), perm: null },
];

const NAV = [
  { group: 'nav.monitoring', items: [
    { path: 'overview', icon: 'overview', label: 'nav.overview' },
    { path: 'building', icon: 'building', label: 'nav.building' },
    { path: 'alarms', icon: 'alarm', label: 'nav.alarms', count: true },
    { path: 'trends', icon: 'trend', label: 'nav.trends' },
    { path: 'reports', icon: 'report', label: 'nav.reports' },
    { path: 'dashboards', icon: 'dashboard', label: 'nav.dashboards' },
    { path: 'calc', icon: 'calc', label: 'nav.calc' },
  ] },
  { group: 'nav.operation', items: [
    { path: 'control', icon: 'control', label: 'nav.control' },
    { path: 'schedules', icon: 'schedule', label: 'nav.schedules' },
  ] },
  { group: 'nav.administration', items: [
    { path: 'users', icon: 'users', label: 'nav.users' },
    { path: 'config/devices', icon: 'devices', label: 'nav.config_devices' },
    { path: 'config/rules', icon: 'rules', label: 'nav.config_rules' },
    { path: 'audit', icon: 'audit', label: 'nav.audit' },
    { path: 'system', icon: 'system', label: 'nav.system' },
  ] },
];

const app = document.getElementById('app');
let shell = null;
let cleanup = null;
let renderToken = 0;

function allowed(perm) {
  if (perm === null) return true;
  return (Array.isArray(perm) ? perm : [perm]).some((p) => can(p));
}

function match(hash) {
  const [pathPart, queryPart] = hash.replace(/^#\/?/, '').split('?');
  const parts = pathPart.split('/').filter(Boolean);
  for (const route of ROUTES) {
    const rp = route.path.split('/');
    if (rp.length !== parts.length) continue;
    const params = {};
    if (rp.every((seg, i) => (seg.startsWith(':') ? ((params[seg.slice(1)] = decodeURIComponent(parts[i])), true) : seg === parts[i]))) {
      return { route, params, query: new URLSearchParams(queryPart || '') };
    }
  }
  return null;
}

export function navigate(path) { location.hash = `#/${path}`; }

// --- shell -----------------------------------------------------------------------------------

function buildShell() {
  const nav = h('nav', { class: 'nav', 'aria-label': t('nav.main') });
  const main = h('main', { class: 'main', id: 'main' });
  const title = h('div', { class: 'title grow' });
  const conn = h('span', { class: 'conn' }, h('span', { class: 'dot' }), h('span', { class: 'txt' }));
  const alarmChip = h('button', { type: 'button', class: 'alarm-chip', onclick: () => navigate('alarms') });
  const soundBtn = button('', () => annunciator.toggleSound(), { kind: 'ghost', iconName: 'sound', title: t('alarms.sound') });
  const silenceBtn = button(t('alarms.silence'), () => annunciator.silence(), { kind: 'danger', size: 'sm' });
  silenceBtn.hidden = true;
  const menuBtn = button('', () => nav.classList.toggle('open'), { kind: 'ghost menu-btn', iconName: 'menu', title: t('nav.menu') });
  const user = h('a', { class: 'user-chip', href: '#/account', title: store.me.display_name }, icon('account'), h('span', { class: 'name' }, store.me.display_name));
  const top = h('header', { class: 'topbar' }, menuBtn, title, conn, silenceBtn, soundBtn, alarmChip, user,
    button('', () => logout(), { kind: 'ghost', iconName: 'logout', title: t('auth.logout') }));

  const brand = h('div', { class: 'brand' }, h('img', { src: 'img/icon.svg', alt: '' }),
    h('div', null, t('app.name'), h('small', null, t('app.site_subtitle', { node: store.info?.node || '' }))));
  nav.append(brand);
  for (const group of NAV) {
    const items = group.items.filter((it) => allowed(ROUTES.find((r) => r.path === it.path).perm));
    if (!items.length) continue;
    nav.append(h('div', { class: 'nav-group' }, h('div', { class: 'label' }, t(group.group)),
      items.map((it) => h('a', { href: `#/${it.path}`, dataset: { path: it.path } }, icon(it.icon), t(it.label),
        it.count ? h('span', { class: 'count', hidden: true }) : null))));
  }
  nav.append(h('div', { class: 'foot' }, `${t('app.version')} ${store.info?.version || ''}`));
  nav.addEventListener('click', (e) => { if (e.target.closest('a')) nav.classList.remove('open'); });

  const root = h('div', { class: 'shell' }, nav, top, main);
  shell = { root, nav, main, title, conn, alarmChip, soundBtn, silenceBtn };
  mount(app, root);
  app.classList.remove('boot');
  updateConnection();
  updateAlarmChip();
  updateSound();
}

function updateConnection() {
  if (!shell) return;
  const state = store.connection;
  shell.conn.className = `conn ${state === 'live' ? 'live' : state === 'down' ? 'down' : ''}`;
  shell.conn.querySelector('.txt').textContent = t(`connection.${state}`);
}

function updateAlarmChip() {
  if (!shell) return;
  const unacked = unackedAlarms();
  const active = store.alarms.filter((a) => !a.cleared_at && !a.shelved).length;
  const chip = shell.alarmChip;
  chip.className = `alarm-chip${unacked.length ? ' has flash' : active ? ' has' : ''}`;
  mount(chip, icon('alarm'), unacked.length ? t('alarms.unacked_n', { n: unacked.length })
    : active ? t('alarms.active_n', { n: active }) : t('alarms.none_short'));
  const count = shell.nav.querySelector('a[data-path="alarms"] .count');
  if (count) { count.hidden = !unacked.length; count.textContent = fmtInteger(unacked.length); }
  shell.silenceBtn.hidden = !annunciator.ringing;
}

function updateSound() {
  if (!shell) return;
  const on = annunciator.soundOn;
  mount(shell.soundBtn, icon(on ? 'sound' : 'mute'));
  shell.soundBtn.title = on ? t('alarms.sound_on') : t('alarms.sound_off');
  shell.soundBtn.setAttribute('aria-label', shell.soundBtn.title);
  shell.silenceBtn.hidden = !annunciator.ringing;
}

export function setTitle(text) {
  if (shell) shell.title.textContent = text;
  document.title = `${text} · ${t('app.name')}`;
}

// --- routing ---------------------------------------------------------------------------------

async function route() {
  if (!shell) return;
  const token = ++renderToken;
  const found = match(location.hash || '');
  if (!found) { navigate(store.home || 'overview'); return; }
  if (!allowed(found.route.perm)) {
    mount(shell.main, h('div', { class: 'page' }, h('div', { class: 'notice warn' }, t('errors.forbidden'))));
    setTitle(t('errors.forbidden_title'));
    return;
  }
  for (const a of shell.nav.querySelectorAll('a[data-path]')) {
    const p = a.dataset.path;
    a.classList.toggle('active', found.route.path === p || found.route.path.startsWith(`${p}/`));
  }
  if (typeof cleanup === 'function') { try { cleanup(); } catch { /* view already gone */ } }
  cleanup = null;
  const page = h('div', { class: 'page' });
  mount(shell.main, page);
  shell.main.scrollTop = 0;
  try {
    const mod = await found.route.view();
    if (token !== renderToken) return;
    cleanup = await mod.render(page, { params: found.params, query: found.query, navigate, setTitle, changeLanguage });
  } catch (err) {
    if (token !== renderToken) return;
    console.error(err);
    mount(page, h('div', { class: 'notice bad' }, errorText(err)));
  }
}

// --- session ---------------------------------------------------------------------------------

async function enterApp() {
  store.me = await api('api/auth/me');
  if (store.me.must_change_password || store.me.mfa_enrollment_required) {
    renderAuth(app, { step: store.me.must_change_password ? 'change-password' : 'mfa-enroll', onDone: enterApp, username: store.me.username });
    return;
  }
  store.info = await api('api/system/info');
  setTimeZone(store.info.timezone);
  if (!storedLanguage() && store.me.language && store.me.language !== getLanguage()) {
    await setLanguage(store.me.language);
  }
  if (can('data.read')) {
    await loadCatalogue();
    await loadAlarms();
    startLive();
    try {
      const boards = await api('api/dashboards');
      const mine = boards.find((b) => b.is_default && b.owner_id === store.me.user_id);
      store.home = mine ? `dashboards/${mine.dashboard_id}` : 'overview';
    } catch { store.home = 'overview'; }
  } else {
    store.home = 'account';
  }
  annunciator.start();
  buildShell();
  if (!location.hash || location.hash === '#/' || location.hash === '#') navigate(store.home);
  else route();
}

function showLogin(reason) {
  stopLive();
  annunciator.stop();
  if (typeof cleanup === 'function') { try { cleanup(); } catch { /* ignore */ } }
  cleanup = null;
  shell = null;
  resetStore();
  clear(app);
  app.classList.remove('boot');
  renderAuth(app, { step: 'login', reason, onDone: enterApp });
}

authEvents.addEventListener('logout', (e) => {
  const reason = e.detail?.reason;
  if (reason === 'logout') history.replaceState(null, '', '#/');   // the next user starts at home
  showLogin(reason && reason !== 'logout' ? reason : null);
});

bus.addEventListener('connection', updateConnection);
bus.addEventListener('alarms', updateAlarmChip);
annunciator.addEventListener('change', () => { updateSound(); updateAlarmChip(); });
window.addEventListener('hashchange', route);

/** Re-render everything after a language change (keeps the session and the live stream). */
export async function changeLanguage(lang) {
  await setLanguage(lang);
  if (store.info) setTimeZone(store.info.timezone);
  if (shell) { buildShell(); route(); }
}

async function boot() {
  applyTheme();
  await setLanguage(storedLanguage() || 'fa');
  if (hasSession()) {
    try { await enterApp(); return; } catch (err) {
      if (err.status !== 401) { toast(errorText(err), 'bad'); }
    }
  }
  showLogin(null);
}

boot();
