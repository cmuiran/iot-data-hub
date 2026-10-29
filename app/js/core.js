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
  arrive: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M14 7l5 5-5 5M19 12H8",
  sun: "M12 3v2M12 19v2M5 12H3M21 12h-2M6.3 6.3L4.9 4.9M19.1 19.1l-1.4-1.4M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  cinema: "M2 7h20v12H2zM7 3l5 4 5-4",
  star: "M12 3l2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8z",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12h2M3 12h2M12 3v2M12 19v2M17 7l1.4-1.4M5.6 18.4L7 17M17 17l1.4 1.4M5.6 5.6L7 7",
  chevron: "M6 9l6 6 6-6",
  next: "M15 6l-6 6 6 6",
  bulb: "M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.8 1 1.5 1 2.5h6c0-1 .2-1.7 1-2.5A6 6 0 0 0 12 3z",
  snow: "M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7M9 4l3 2 3-2M9 20l3-2 3 2",
  flame: "M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 1 1 2 2 3 2 0-2-1-3 0-5z",
  battery: "M3 8h15v8H3zM21 11v2M6 11h4",
  thermo: "M14 14.8V4a2 2 0 0 0-4 0v10.8a4 4 0 1 0 4 0zM12 9v6",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  edit: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
  alert: "M12 9v4M12 17h0M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  room: "M3 21V8l9-5 9 5v13M9 21v-7h6v7",
  chandelier: "M12 2v4M5 9h14M12 6v3M6 9v3M18 9v3M12 9v4M4 12h4l-2 4zM16 12h4l-2 4zM10 13h4l-2 4z",
  lamp: "M8 3h8l3 8H5zM12 11v8M8 21h8",
  fan: "M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM12 10c0-4 1-7 4-7 2 0 2 3 0 5M14 12c4 0 7 1 7 4 0 2-3 2-5 0M12 14c0 4-1 7-4 7-2 0-2-3 0-5M10 12c-4 0-7-1-7-4 0-2 3-2 5 0",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
  tv: "M3 6h18v12H3zM8 21h8M9 3l3 3 3-3",
  curtain: "M3 3h18M5 3v18c3-2 4-8 4-18M19 3v18c-3-2-4-8-4-18",
  door: "M6 21V3h12v18M3 21h18M14 12h0",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4M12 15v2",
  water: "M12 3c3 4 6 7 6 11a6 6 0 0 1-12 0c0-4 3-7 6-11zM9 15a3 3 0 0 0 3 3",
  pump: "M4 14h4M16 14h4M12 18a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 10V5M9 5h6",
  leaf: "M5 19c0-9 6-14 15-14 0 9-5 15-14 15zM5 19l8-8",
  chat: "M4 5h16v11H9l-5 4zM8 10h0M12 10h0M16 10h0",
};

export const SCENE_ICONS = ["moon", "sun", "out", "arrive", "cinema", "power", "home", "bulb"];
export const SCENE_TONE = { moon: "violet", out: "ok", arrive: "accent", sun: "accent",
  cinema: "warn", power: "accent", home: "ok", bulb: "accent" };

/** A modal dialog; `body` is a node; resolves with the value of the button pressed (or null). */
export function dialog(title, body, buttons = []) {
  return new Promise((resolve) => {
    const box = h("dialog", { class: "sheet" });
    const close = (value) => { box.close(); box.remove(); resolve(value); };
    box.append(h("div", { class: "stack" },
      h("div", { class: "row between" }, h("h2", { class: "h2" }, title),
        h("button", { type: "button", class: "icon-btn", "aria-label": t("common.close"),
          onclick: () => close(null) }, icon("x"))),
      body,
      buttons.length ? h("div", { class: "row wrap end" }, buttons.map(([value, label, kind]) =>
        h("button", { type: "button", class: `btn ${kind || ""}`, onclick: () => close(value) }, label)))
        : null));
    box.addEventListener("cancel", (ev) => { ev.preventDefault(); close(null); });
    box.closeWith = close;
    document.body.append(box);
    box.showModal();
  });
}

export function setAccent(name) {
  if (name) document.documentElement.dataset.accent = name;
  else delete document.documentElement.dataset.accent;
  saved.set("app.accent", name || null);
}

/** The home (organization) the app shows; null = every home of the user. */
export const home = {
  get id() { return saved.get("app.home"); },
  set(id) { saved.set("app.home", id || null); },
  query(prefix = "?") { return this.id ? `${prefix}org=${encodeURIComponent(this.id)}` : ""; },
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

const seen = new Map();
/** Marks ``el`` "changed" (a one-time glow) when the value shown under ``key`` differs from the
 * last time it was drawn: live updates redraw the page, and this shows what moved. */
export function changed(el, key, value) {
  const v = JSON.stringify(value);
  if (seen.has(key) && seen.get(key) !== v) el.classList.add("changed");
  seen.set(key, v);
  return el;
}

/** A shimmering placeholder while a page loads (the text stays for screen readers). */
export function loading() {
  return h("div", { class: "loading", role: "status" }, t("common.loading"));
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

/** Live updates (server-sent events over fetch, so the bearer token goes in a header):
 * `onEvent(type, data)` for "samples" and "status"; reconnects after errors or when the
 * server ends the stream (every five minutes). Returns a stop function. */
export function live(onEvent, query = "") {
  let stopped = false;
  let controller = null;
  async function connect() {
    while (!stopped) {
      try {
        if (!access) await refresh();
        controller = new AbortController();
        const r = await fetch(`/api/platform/my/stream${query}`, {
          headers: { Authorization: `Bearer ${access}` }, signal: controller.signal });
        if (r.status === 401) { await refresh(); continue; }
        if (!r.ok || !r.body) throw new Error(`stream ${r.status}`);
        const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let cut;
          while ((cut = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            const type = (block.match(/^event: (.+)$/m) || [])[1];
            const data = (block.match(/^data: (.+)$/m) || [])[1];
            if (type && data) {
              try { onEvent(type, JSON.parse(data)); } catch { /* a bad frame is skipped */ }
            }
          }
        }
      } catch { /* offline or aborted: try again shortly */ }
      if (!stopped) await new Promise((r) => setTimeout(r, 3000));
    }
  }
  connect();
  return () => { stopped = true; if (controller) controller.abort(); };
}

/** A picture from the API (with the bearer token) as an object URL, cached per path. */
const blobs = new Map();
export async function imageUrl(path) {
  if (blobs.has(path)) return blobs.get(path);
  if (!access) await refresh();
  const r = await fetch(path, { headers: access ? { Authorization: `Bearer ${access}` } : {} });
  if (!r.ok) throw new ApiError(r.status, {});
  const url = URL.createObjectURL(await r.blob());
  blobs.set(path, url);
  return url;
}

export async function logout() {
  try { await api("POST", "/api/auth/logout", { refresh_token: saved.get("app.refresh") }); }
  catch { /* the local session ends anyway */ }
  session.clear();
}
