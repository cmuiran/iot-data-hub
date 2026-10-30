// Core of the maker panel: DOM helper, icons, i18n, API client, session, dialogs, dates.
// No framework, no build step; server text always goes in as text nodes, never as HTML.

// --- DOM ---------------------------------------------------------------------------------------

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("aria-") && typeof v === "boolean") { el.setAttribute(k, String(v)); continue; }
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = Boolean(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const PATHS = {
  logo: "M12 2l9 5v10l-9 5-9-5V7z M12 12l9-5M12 12v10M12 12L3 7",
  dashboard: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  products: "M12 2l9 5v10l-9 5-9-5V7z",
  production: "M4 4h16v16H4zM9 9h6v6H9z",
  qr: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h3v3h-3zM18 18h3v3h-3z",
  warranty: "M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z",
  support: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12h2M3 12h2M12 3v2M12 19v2M17 7l1.4-1.4M5.6 18.4L7 17M17 17l1.4 1.4M5.6 5.6L7 7",
  plus: "M12 5v14M5 12h14",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12l5 5 9-10",
  back: "M9 6l6 6-6 6",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  download: "M12 4v12M7 11l5 5 5-5M4 20h16",
  print: "M6 9V3h12v6M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2M6 14h12v7H6z",
  out: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l-5-5 5-5M5 12h11",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  reset: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5",
  chip: "M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4",
  image: "M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6M15 9h0",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18",
  chevron: "M6 9l6 6 6-6",
  menu: "M4 6h16M4 12h16M4 18h16",
};

export function icon(name, size = 18) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("class", "i");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", PATHS[name] || PATHS.chip);
  svg.append(path);
  return svg;
}

// --- storage -------------------------------------------------------------------------------------

export const saved = {
  get(key) { try { return localStorage.getItem(`maker.${key}`); } catch { return null; } },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem(`maker.${key}`);
      else localStorage.setItem(`maker.${key}`, value);
    } catch { /* not kept: this visit only */ }
  },
};

// --- i18n ----------------------------------------------------------------------------------------

let dict = {};
export let lang = saved.get("lang") || "fa";

export async function loadLanguage(code) {
  lang = code === "en" ? "en" : "fa";
  dict = await (await fetch(`i18n/${lang}.json`)).json();
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";
  saved.set("lang", lang);
}

export function t(key, vars = {}) {
  let text = key.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), dict);
  if (typeof text !== "string") text = key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
}

export function has(key) {
  return typeof key.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined),
    dict) === "string";
}

export function name(names) {
  return (names && (names[lang] || names.fa || names.en)) || "";
}

const DIGITS = "۰۱۲۳۴۵۶۷۸۹";
export function num(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return lang === "fa" ? value.replace(/\d/g, (d) => DIGITS[d]) : value;
  if (typeof value !== "number") return String(value);
  const text = Number.isInteger(value)
    ? value.toLocaleString(lang === "fa" ? "fa-IR" : "en-US")
    : value.toFixed(digits);
  return lang === "fa" ? text.replace(/\d/g, (d) => DIGITS[d]).replace(".", "٫") : text;
}

export function date(iso, withTime = false) {
  if (!iso) return "—";
  const opts = withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" };
  return new Intl.DateTimeFormat(lang === "fa" ? "fa-IR-u-ca-persian" : "en-GB",
    { ...opts, timeZone: "Asia/Tehran" }).format(new Date(iso));
}

export function ago(iso) {
  if (!iso) return "";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return t("time.now");
  if (s < 3600) return t("time.minutes", { n: num(Math.round(s / 60)) });
  if (s < 86400) return t("time.hours", { n: num(Math.round(s / 3600)) });
  return t("time.days", { n: num(Math.round(s / 86400)) });
}

// --- toasts and dialogs --------------------------------------------------------------------------

export function toast(text, kind = "") {
  const box = document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind}`, role: "status" }, text);
  box.append(el);
  setTimeout(() => el.remove(), 5000);
}

export function errorText(err) {
  const details = err && err.body && Array.isArray(err.body.details) && err.body.details.length
    ? err.body.details.slice(0, 3).map((d) => d.msg).join(" · ")
    : err && err.body && Array.isArray(err.body.values) ? err.body.values.slice(0, 10).join("، ") : "";
  const base = err && err.code && has(`errors.${err.code}`) ? t(`errors.${err.code}`)
    : (err && err.message) || t("errors.network");
  return details ? `${base}: ${details}` : base;
}

/** A modal dialog; `body` is a node; resolves with the clicked button's value (or null). */
export function dialog(title, body, buttons = [["ok", t("common.ok"), "primary"]]) {
  return new Promise((resolve) => {
    const box = h("dialog", { class: "dialog" });
    const close = (value) => { box.close(); box.remove(); resolve(value); };
    box.append(h("form", { method: "dialog", class: "stack" },
      h("div", { class: "row between" }, h("h2", { class: "h2" }, title),
        h("button", { type: "button", class: "icon-btn", "aria-label": t("common.close"),
          onclick: () => close(null) }, icon("x"))),
      body,
      h("div", { class: "row end wrap" }, buttons.map(([value, label, kind]) => h("button", {
        type: "button", class: `btn ${kind || ""}`, onclick: () => close(value) }, label)))));
    box.addEventListener("cancel", (ev) => { ev.preventDefault(); close(null); });
    document.body.append(box);
    box.showModal();
  });
}

export async function confirmAction(text, label = t("common.confirm"), danger = false) {
  return (await dialog(t("common.are_you_sure"), h("p", { class: "muted" }, text),
    [["no", t("common.cancel")], ["yes", label, danger ? "danger" : "primary"]])) === "yes";
}

export function busy(button, task) {
  return async (ev) => {
    ev?.preventDefault?.();
    button.disabled = true;
    try { await task(); } catch (err) { toast(errorText(err), "err"); }
    finally { button.disabled = false; }
  };
}

export function download(filename, text, type = "text/csv;charset=utf-8") {
  const url = URL.createObjectURL(new Blob(["﻿", text], { type }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i += 1;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((x) => x.trim()));
}

// --- API -----------------------------------------------------------------------------------------

let access = null;
export const session = {
  get signedIn() { return Boolean(access || saved.get("refresh")); },
  store(tokens) { access = tokens.access_token; saved.set("refresh", tokens.refresh_token); },
  clear() { access = null; saved.set("refresh", null); },
};

export class ApiError extends Error {
  constructor(status, body) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.code = body.error;
    this.body = body;
  }
}

let refreshing = null;
async function refresh() {
  const token = saved.get("refresh");
  if (!token) return false;
  refreshing = refreshing || fetch("/api/auth/refresh", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: token }),
  }).then(async (r) => {
    if (!r.ok) { session.clear(); return false; }
    session.store(await r.json());
    return true;
  }).finally(() => { refreshing = null; });
  return refreshing;
}

/** JSON API call; `raw` sends a Blob/ArrayBuffer body; `as: "text" | "blob"` for answers. */
export async function api(method, path, body, { auth = true, retry = true, raw = null,
  type = null, as = "json" } = {}) {
  if (auth && !access && !(await refresh())) throw new ApiError(401, { error: "not_authenticated" });
  const headers = {};
  if (raw === null && body !== undefined) headers["Content-Type"] = "application/json";
  if (raw !== null) headers["Content-Type"] = type || "application/octet-stream";
  if (auth && access) headers.Authorization = `Bearer ${access}`;
  let r;
  try {
    r = await fetch(path, { method, headers,
      body: raw !== null ? raw : body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, { error: "network" });
  }
  if (r.status === 401 && auth && retry && (await refresh())) {
    return api(method, path, body, { auth, retry: false, raw, type, as });
  }
  if (r.status === 204) return null;
  if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => ({})));
  if (as === "text") return r.text();
  if (as === "blob") return r.blob();
  return r.json().catch(() => ({}));
}

export async function logout() {
  try { await api("POST", "/api/auth/logout", { refresh_token: saved.get("refresh") }); }
  catch { /* the local session ends anyway */ }
  session.clear();
}
