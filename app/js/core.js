// Core of the consumer app: DOM helper, icons, i18n, API client, session, toasts.
// No framework, no build step; every text from the server goes in as a text node, never as HTML.

// --- DOM ---------------------------------------------------------------------------------------

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("aria-") && typeof v === "boolean") { el.setAttribute(k, String(v)); continue; }
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const PATHS = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  plus: "M12 5v14M5 12h14",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  back: "M9 6l6 6-6 6",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
  power: "M12 3v8M6.3 6.3a8 8 0 1 0 11.4 0",
  relay: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  hub: "M4 5h16v6H4zM4 15h16v4H4zM8 8h0M8 17h0M12 11v4",
  sensor: "M12 3c3 4 6 7 6 11a6 6 0 0 1-12 0c0-4 3-7 6-11z",
  check: "M5 12l5 5 9-10",
  clock: "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  x: "M6 6l12 12M18 6L6 18",
  share: "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.6 13.5l6.8 4M15.4 6.5l-6.8 4",
  qr: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM18 18h3v3h-3z",
  people: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M17 11a3 3 0 1 0 0-6M22 21a6 6 0 0 0-5-5.9",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  out: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l-5-5 5-5M5 12h11",
};

export function icon(name, cls = "i") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", PATHS[name] || PATHS.sensor);
  svg.append(path);
  return svg;
}

// --- storage (may be unavailable: private windows) ---------------------------------------------

export const saved = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) {
    try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); }
    catch { /* not kept: works for this visit only */ }
  },
};

// --- i18n ----------------------------------------------------------------------------------------

let dict = {};
export let lang = saved.get("app.lang") || "fa";

export async function loadLanguage(code) {
  lang = code === "en" ? "en" : "fa";
  const r = await fetch(`i18n/${lang}.json`);
  dict = await r.json();
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";
  document.title = t("app.title");
  saved.set("app.lang", lang);
}

export function t(key, vars = {}) {
  let text = key.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), dict);
  if (typeof text !== "string") text = key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
}

export function name(names) {          // a template name {fa, en} in the user's language
  return (names && (names[lang] || names.fa || names.en)) || "";
}

const DIGITS = "۰۱۲۳۴۵۶۷۸۹";
export function num(value, digits = 1) {
  if (typeof value === "string") {           // times, Jalali dates: digits only
    return lang === "fa" ? value.replace(/\d/g, (d) => DIGITS[d]) : value;
  }
  if (typeof value !== "number") return String(value ?? "");
  const text = Number.isInteger(value) ? String(value) : value.toFixed(digits);
  return lang === "fa" ? text.replace(/\d/g, (d) => DIGITS[d]).replace(".", "٫") : text;
}

export function when(iso) {
  if (!iso) return "";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return t("time.now");
  if (s < 3600) return t("time.minutes", { n: num(Math.round(s / 60)) });
  if (s < 86400) return t("time.hours", { n: num(Math.round(s / 3600)) });
  return new Intl.DateTimeFormat(lang === "fa" ? "fa-IR" : "en-GB",
    { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

// --- toasts --------------------------------------------------------------------------------------

export function toast(text, kind = "") {
  const box = document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind}`, role: "status" }, text);
  box.append(el);
  setTimeout(() => el.remove(), 4200);
}

export function errorText(err) {
  if (err && err.code) {
    const known = t(`errors.${err.code}`);
    if (known !== `errors.${err.code}`) return known;
  }
  return (err && err.message) || t("errors.network");
}

// --- API -----------------------------------------------------------------------------------------

let access = null;
export const session = {
  get signedIn() { return !!(access || saved.get("app.refresh")); },
  store(tokens) {
    access = tokens.access_token;
    saved.set("app.refresh", tokens.refresh_token);
  },
  clear() { access = null; saved.set("app.refresh", null); },
};

export class ApiError extends Error {
  constructor(status, body) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.code = body.error;
    this.body = body;
  }
}

async function refresh() {
  const token = saved.get("app.refresh");
  if (!token) return false;
  const r = await fetch("/api/auth/refresh", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: token }),
  });
  if (!r.ok) { session.clear(); return false; }
  session.store(await r.json());
  return true;
}

export async function api(method, path, body, { auth = true, retry = true } = {}) {
  if (auth && !access && !(await refresh())) throw new ApiError(401, { error: "not_authenticated" });
  const headers = { "Content-Type": "application/json" };
  if (auth && access) headers.Authorization = `Bearer ${access}`;
  let r;
  try {
    r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, { error: "network" });
  }
  if (r.status === 401 && auth && retry && (await refresh())) {
    return api(method, path, body, { auth, retry: false });
  }
  if (r.status === 204) return null;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, data);
  return data;
}

export async function logout() {
  try { await api("POST", "/api/auth/logout", { refresh_token: saved.get("app.refresh") }); }
  catch { /* the local session ends anyway */ }
  session.clear();
}
