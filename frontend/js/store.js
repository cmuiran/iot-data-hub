// Shared client state: the signed-in user, hub info, tag/device catalogue, live values,
// active alarms. Views read from here and listen on `bus` for changes.

import { api, accessToken, refresh, authEvents } from './api.js';

export const bus = new EventTarget();
const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

export const store = {
  me: null,
  info: null,
  tags: new Map(),      // path -> TagOut (metadata + latest value)
  devices: new Map(),   // device_id -> DeviceOut
  alarms: [],           // active alarms (AlarmOut)
  trail: new Map(),     // path -> [{t, v}] recent numeric samples for sparklines
  connection: 'idle',   // idle | connecting | live | down
};

const TRAIL_MAX = 240;

// --- permissions ------------------------------------------------------------------------------

/** Like the server's Principal.can: a global grant, or one covering site/area. */
export function can(permission, site, area) {
  const grants = store.me?.permissions?.[permission];
  if (!grants) return false;
  if (site === undefined) return grants.length > 0;
  return grants.some((g) => (g.site === null || g.site === site) && (g.area === null || g.area === area));
}

export function canTag(permission, path) {
  const [site, area] = path.split('.');
  return can(permission, site, area);
}

// --- catalogue -----------------------------------------------------------------------------

export async function loadCatalogue() {
  const [devices, tags] = await Promise.all([api('api/devices'), api('api/tags')]);
  store.devices = new Map(devices.map((d) => [d.device_id, d]));
  const next = new Map();
  for (const tag of tags) {
    const old = store.tags.get(tag.tag);
    // keep a newer live value if the stream already delivered one
    if (old && old.ts && (!tag.ts || old.ts > tag.ts)) Object.assign(tag, { value: old.value, ts: old.ts, quality: old.quality });
    next.set(tag.tag, tag);
  }
  store.tags = next;
  emit('catalogue');
}

export function tagsOfDevice(deviceId) {
  return [...store.tags.values()].filter((t) => t.tag.split('.')[2] === deviceId);
}

/** Newest of the device's status time and its tags' sample times (status events are sparse). */
export function deviceLastSeen(deviceId) {
  let best = store.devices.get(deviceId)?.last_seen || null;
  for (const tag of tagsOfDevice(deviceId)) if (tag.ts && (!best || tag.ts > best)) best = tag.ts;
  return best;
}

export function deviceOfTag(path) { return store.devices.get(path.split('.')[2]); }

export function isNumeric(tag) { return tag && tag.data_type !== 'bool' && tag.data_type !== 'string'; }

/** Sites -> areas -> devices, sorted, for navigation and scope pickers. */
export function siteTree() {
  const tree = new Map();
  for (const d of [...store.devices.values()].sort((a, b) => a.device_id.localeCompare(b.device_id))) {
    if (!tree.has(d.site)) tree.set(d.site, new Map());
    const areas = tree.get(d.site);
    if (!areas.has(d.area)) areas.set(d.area, []);
    areas.get(d.area).push(d);
  }
  return tree;
}

// --- alarms --------------------------------------------------------------------------------

let alarmTimer = null;
export async function loadAlarms() {
  try {
    store.alarms = await api('api/alarms');
    emit('alarms');
  } catch { /* keep the last list; the connection indicator shows the problem */ }
}

function scheduleAlarmReload() {
  clearTimeout(alarmTimer);
  alarmTimer = setTimeout(loadAlarms, 250);
}

const SEVERITY_RANK = { critical: 4, high: 3, medium: 2, low: 1 };
export function severityRank(sev) { return SEVERITY_RANK[sev] || 0; }

export function unackedAlarms() {
  return store.alarms.filter((a) => !a.acked_at && !a.shelved);
}

// --- live stream -----------------------------------------------------------------------------

let ws = null;
let wanted = false;
let retry = 0;
let pingTimer = null;
let reconnectTimer = null;

function setConnection(state) {
  if (store.connection !== state) { store.connection = state; emit('connection', state); }
}

function applySample(s) {
  const tag = store.tags.get(s.tag);
  if (tag) { tag.value = s.value; tag.ts = s.ts; tag.quality = s.quality; }
  if (typeof s.value === 'number') {
    let trail = store.trail.get(s.tag);
    if (!trail) { trail = []; store.trail.set(s.tag, trail); }
    const ms = Date.parse(s.ts);
    if (!trail.length || trail[trail.length - 1].t < ms) {
      trail.push({ t: ms, v: s.value });
      if (trail.length > TRAIL_MAX) trail.splice(0, trail.length - TRAIL_MAX);
    }
  }
}

function onMessage(msg) {
  switch (msg.type) {
    case 'snapshot':
      msg.tags.forEach(applySample);
      for (const st of msg.devices) {
        const d = store.devices.get(st.device_id);
        if (d) Object.assign(d, { state: st.state, last_seen: st.last_seen, last_error: st.last_error });
      }
      emit('samples', msg.tags);
      emit('status');
      scheduleAlarmReload();
      break;
    case 'samples':
      msg.samples.forEach(applySample);
      emit('samples', msg.samples);
      break;
    case 'status': {
      const d = store.devices.get(msg.device_id);
      if (d) Object.assign(d, { state: msg.state, last_seen: msg.last_seen, last_error: msg.last_error });
      emit('status', msg);
      break;
    }
    case 'alarm':
      emit('alarm-event', msg);
      scheduleAlarmReload();
      break;
    case 'write':
      emit('write', msg);
      break;
    default:
      break;
  }
}

function socketUrl() {
  const url = new URL('api/ws', document.baseURI);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}

function scheduleReconnect() {
  if (!wanted) return;
  clearTimeout(reconnectTimer);
  const delay = Math.min(30_000, 1000 * 2 ** retry) * (0.75 + Math.random() * 0.5);
  retry = Math.min(retry + 1, 5);
  reconnectTimer = setTimeout(connect, delay);
}

async function connect() {
  if (!wanted || (ws && ws.readyState <= 1)) return;
  setConnection('connecting');
  if (!accessToken()) {
    try { await refresh(); } catch { setConnection('down'); return; }
  }
  const socket = new WebSocket(socketUrl());
  ws = socket;
  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ type: 'auth', token: accessToken() }));
    clearInterval(pingTimer);
    pingTimer = setInterval(() => { if (socket.readyState === 1) socket.send('{"type":"ping"}'); }, 25_000);
  });
  socket.addEventListener('message', (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'snapshot') { retry = 0; setConnection('live'); }
    onMessage(msg);
  });
  socket.addEventListener('close', async (e) => {
    clearInterval(pingTimer);
    if (ws === socket) ws = null;
    if (!wanted) { setConnection('idle'); return; }
    setConnection('down');
    if (e.code === 4403) return;                 // no permission to read data: stay down
    if (e.code === 4401) {                       // token expired / session check failed
      try { await refresh(); } catch { return; } // refresh failure ends the session
    }
    scheduleReconnect();
  });
}

export function startLive() {
  wanted = true;
  retry = 0;
  connect();
}

export function stopLive() {
  wanted = false;
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  if (ws) { try { ws.close(1000); } catch { /* already closed */ } }
  ws = null;
  setConnection('idle');
}

// a new access token: the open socket keeps working (the server re-checks the session itself)
authEvents.addEventListener('tokens', () => { if (wanted && !ws) connect(); });

// reconnect quickly when the machine wakes up or the network comes back
window.addEventListener('online', () => { if (wanted) { retry = 0; connect(); } });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && wanted && !ws) { retry = 0; connect(); }
});

export function resetStore() {
  store.me = null; store.info = null; store.tags = new Map(); store.devices = new Map();
  store.alarms = []; store.trail = new Map();
}
