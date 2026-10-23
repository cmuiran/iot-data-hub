// REST client: bearer tokens, single-flight refresh, uniform errors.
//
// The access token lives in memory only. The refresh token is kept in sessionStorage so a
// reload of the tab keeps the session, while closing the tab (or the browser) ends it.

const REFRESH_KEY = 'hub.refresh';
const state = { access: null, expiresAt: 0, refreshing: null, timer: null };
export const authEvents = new EventTarget();

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.message || `HTTP ${status}`);
    this.status = status;
    this.code = body?.error || (status === 0 ? 'network' : 'http_error');
    this.details = body?.details;
    this.body = body;
  }
}

function storedRefresh() {
  try { return sessionStorage.getItem(REFRESH_KEY); } catch { return null; }
}

function storeRefresh(token) {
  try {
    if (token) sessionStorage.setItem(REFRESH_KEY, token);
    else sessionStorage.removeItem(REFRESH_KEY);
  } catch { /* storage blocked: the session then lasts as long as the page */ }
}

export function hasSession() { return Boolean(state.access || storedRefresh()); }
export function accessToken() { return state.access; }

export function setTokens(pair) {
  state.access = pair.access_token;
  state.expiresAt = Date.now() + pair.expires_in * 1000;
  storeRefresh(pair.refresh_token);
  clearTimeout(state.timer);
  // renew a minute before expiry so the live stream and open forms never see a 401
  const wait = Math.max(10_000, pair.expires_in * 1000 - 60_000);
  state.timer = setTimeout(() => { refresh().catch(() => {}); }, wait);
  authEvents.dispatchEvent(new CustomEvent('tokens'));
}

export function clearTokens() {
  state.access = null;
  state.expiresAt = 0;
  clearTimeout(state.timer);
  storeRefresh(null);
}

function endSession(reason) {
  clearTokens();
  authEvents.dispatchEvent(new CustomEvent('logout', { detail: { reason } }));
}

/** Exchange the refresh token (rotating) for a new pair. Concurrent callers share one call. */
export function refresh() {
  if (state.refreshing) return state.refreshing;
  const token = storedRefresh();
  if (!token) return Promise.reject(new ApiError(401, { error: 'not_authenticated' }));
  state.refreshing = (async () => {
    try {
      const r = await fetch('api/auth/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: token }),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        const err = new ApiError(r.status, body);
        if (r.status === 401 || r.status === 403) endSession(err.code);
        throw err;
      }
      setTokens(body);
      return body;
    } finally {
      state.refreshing = null;
    }
  })();
  return state.refreshing;
}

function buildUrl(path, query) {
  const url = new URL(path.replace(/^\//, ''), document.baseURI);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item));
      else url.searchParams.set(k, v instanceof Date ? v.toISOString() : String(v));
    }
  }
  return url;
}

async function send(path, { method = 'GET', body, query, auth = true, accept } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (accept) headers.Accept = accept;
  if (auth && state.access) headers.Authorization = `Bearer ${state.access}`;
  try {
    return await fetch(buildUrl(path, query), {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, { error: 'network', message: 'network error' });
  }
}

// 401 codes that mean "the session is gone"; others (wrong password, wrong MFA code, locked)
// are ordinary answers to the request and must not log the user out.
const RENEWABLE = new Set(['invalid_token', 'not_authenticated']);
const SESSION_ENDED = new Set(['invalid_token', 'not_authenticated', 'session_expired',
  'session_idle', 'unauthorized']);

/** Call the API; resolves to the JSON body (or null for 204). Throws ApiError. */
export async function api(path, options = {}) {
  if (options.auth !== false && !state.access && storedRefresh()) {
    await refresh();
  }
  let r = await send(path, options);
  if (r.status === 401 && options.auth !== false) {
    const body = await r.clone().json().catch(() => null);
    if (RENEWABLE.has(body?.error) && storedRefresh()) {
      await refresh();
      r = await send(path, options);
    }
  }
  if (r.status === 204) return null;
  const type = r.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await r.json().catch(() => null) : await r.blob();
  if (!r.ok) {
    if (r.status === 401 && options.auth !== false && SESSION_ENDED.has(data?.error)) {
      endSession(data.error);
    }
    throw new ApiError(r.status, data);
  }
  return data;
}

/** Fetch a file from the API (with the bearer token) and hand it to the browser to save. */
export async function download(path, options = {}, fallbackName = 'download') {
  if (!state.access && storedRefresh()) await refresh();
  let r = await send(path, options);
  if (r.status === 401 && storedRefresh()) {
    await refresh();
    r = await send(path, options);
  }
  if (!r.ok) {
    const body = await r.json().catch(() => null);
    throw new ApiError(r.status, body);
  }
  const blob = await r.blob();
  const disposition = r.headers.get('content-disposition') || '';
  const name = /filename="?([^";]+)"?/.exec(disposition)?.[1] || fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

/** Password step of the login: tokens, or {mfa_required, mfa_token}. */
export async function login(username, password) {
  const body = await api('api/auth/login', { method: 'POST', body: { username, password }, auth: false });
  if (body.access_token) setTokens(body);
  return body;
}

export async function verifyMfa(mfaToken, { code, recoveryCode }) {
  const body = await api('api/auth/mfa/verify', {
    method: 'POST', auth: false,
    body: { mfa_token: mfaToken, code: code || null, recovery_code: recoveryCode || null },
  });
  setTokens(body);
  return body;
}

export async function logout() {
  try {
    if (state.access) await api('api/auth/logout', { method: 'POST' });
  } catch { /* the session may already be gone */ }
  endSession('logout');
}
