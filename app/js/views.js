// Screens of the consumer app. Each returns a DOM node; `go(route)` navigates.

import { commandsSection, deleteAccount, groupsRow, newHome, sharesList, smsSection, ticketsSection,
  timersSection } from "./more.js";
import { favoritesRow, outButton, smsSheet } from "./outputs.js";
import { api, changed, loading, dialog, errorText, h, home, icon, imageUrl, lang, loadLanguage, logout, name, num,
  saved, SCENE_TONE, session, setAccent, t, toast, when } from "./core.js";

export const go = (route) => { location.hash = route; };

// --- shared pieces -------------------------------------------------------------------------------

function field(label, attrs) {
  const id = `f-${Math.random().toString(36).slice(2, 8)}`;
  const input = h("input", { class: "input", id, ...attrs });
  return { input, el: h("div", { class: "field" }, h("label", { for: id }, label), input) };
}

function busy(button, task) {
  return async (ev) => {
    ev?.preventDefault?.();
    button.disabled = true;
    try { await task(); } catch (err) { toast(errorText(err), "err"); }
    finally { button.disabled = false; }
  };
}

function header(title, back = "#/home", sub = null, right = null) {
  return h("div", { class: "row between top-bar" },
    h("a", { class: "icon-btn", href: back, "aria-label": t("common.back") }, icon("back")),
    h("div", { class: "stack tight center grow" }, h("h1", { class: "h2" }, title),
      sub ? h("span", { class: "small muted" }, sub) : null),
    right || h("span", { class: "icon-btn hidden" }));
}

export function nav(active) {
  const item = (route, key, ic, on = route) => h("a", { href: `#/${route}`,
    class: active === on ? "on" : "", "aria-current": active === on ? "page" : null }, icon(ic), t(key));
  return h("nav", { class: "nav", "aria-label": t("nav.label") },
    item("home", "nav.home", "home"),
    item("auto/scenes", "nav.scenes", "star", "scenes"),
    h("a", { href: "#/add", class: "fab", "aria-label": t("nav.add") }, icon("plus", "i lg")),
    item("auto/automations", "nav.automations", "sliders", "automations"),
    item("me", "nav.me", "user"));
}

function online(dev) { return dev.state === "online"; }

/** The icon of a device from what its template has. */
function deviceIcon(dev, view) {
  if (dev.product_kind === "edge_hub") return "hub";
  const tags = view?.tags || [];
  const outs = tags.filter((x) => x.writable && x.data_type === "bool");
  const units = tags.map((x) => x.unit || "");
  if (outs.length > 1) return "relay";
  if (tags.some((x) => x.ui?.icon && x.ui.icon in { bulb: 1, snow: 1, flame: 1, sensor: 1 })) {
    return tags.find((x) => x.ui?.icon).ui.icon;
  }
  if (outs.length === 1 && units.some((u) => u === "°C")) return "thermo";
  if (outs.length === 1) return "bulb";
  if (units.some((u) => u === "kW" || u === "kWh" || u === "W")) return "bolt";
  if (units.some((u) => u === "%")) return "battery";
  if (units.some((u) => u === "°C")) return "thermo";
  return "sensor";
}

const TRANSPORT = { mqtt: "MQTT", http: "HTTP", tcp: "TCP/IP" };
function protoLabel(dev) {
  if (dev.protocol === "native") {
    const tr = dev.connection?.transports || ["mqtt", "http", "tcp"];
    return tr.map((x) => TRANSPORT[x] || x).slice(0, 2).join(" · ");
  }
  return t(`proto.${dev.protocol}`) !== `proto.${dev.protocol}` ? t(`proto.${dev.protocol}`) : dev.protocol;
}

function display(tag) {
  if (tag.value === null || tag.value === undefined) return "—";
  if (tag.states && String(Number(tag.value)) in tag.states) return tag.states[Number(tag.value)];
  if (typeof tag.value === "boolean") return tag.value ? t("device.on") : t("device.off");
  return num(tag.value);
}

function jalali(iso) {
  return new Intl.DateTimeFormat(lang === "fa" ? "fa-IR-u-ca-persian" : "en-GB",
    { dateStyle: "medium", timeZone: "Asia/Tehran" }).format(new Date(iso));
}

// --- homes ---------------------------------------------------------------------------------------

export async function homes() {
  const rows = await api("GET", "/api/account/organizations");
  const out = new Map();
  for (const m of rows) if (m.kind !== "manufacturer" && !out.has(m.org_id)) out.set(m.org_id, m);
  return [...out.values()];
}

function homeName(m) { return m.personal ? t("me.my_home") : m.name; }

async function pickHome(list) {
  const body = h("div", { class: "stack" });
  const done = dialog(t("home.pick"), body);
  const box = document.querySelector("dialog.sheet:last-of-type");
  body.append(...[{ org_id: null, name: t("home.every") }, ...list].map((m) => h("button", {
    class: `reading pick ${String(home.id) === String(m.org_id) || (!home.id && !m.org_id) ? "on" : ""}`,
    onclick: () => { home.set(m.org_id); box.closeWith("ok"); } },
  h("span", { class: "row" }, icon(m.org_id ? "home" : "star"), m.org_id ? homeName(m) : m.name),
  m.role_id ? h("span", { class: "chip" }, t(`roles.${m.role_id}`)) : null)));
  return done;
}

// --- sign-in and account links -------------------------------------------------------------------

export function login(brand = null) {
  const email = field(t("auth.email"), { type: "email", autocomplete: "username", dir: "ltr" });
  const password = field(t("auth.password"), { type: "password", autocomplete: "current-password" });
  const code = field(t("auth.mfa_code"), { inputmode: "numeric", autocomplete: "one-time-code" });
  code.el.classList.add("hidden");
  let mfaToken = null;
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("auth.sign_in"));
  const form = h("form", { class: "stack" }, email.el, password.el, code.el, submit);
  form.addEventListener("submit", busy(submit, async () => {
    let tokens;
    if (mfaToken) {
      tokens = await api("POST", "/api/auth/mfa/verify",
        { mfa_token: mfaToken, code: code.input.value.trim() }, { auth: false });
    } else {
      tokens = await api("POST", "/api/auth/login",
        { username: email.input.value.trim(), password: password.input.value }, { auth: false });
      if (tokens.mfa_required) {
        mfaToken = tokens.mfa_token;
        code.el.classList.remove("hidden");
        code.input.focus();
        return;
      }
    }
    session.store(tokens);
    go(saved.get("app.after") || "#/home");
    saved.set("app.after", null);
  }));
  const logo = brand?.has_logo
    ? h("img", { class: "brand-logo", src: `/api/public/brand/${brand.org_id}/logo`, alt: brand.display_name })
    : h("div", { class: "brand" }, icon("home", "i lg"));
  return h("div", { class: "page" },
    h("div", { class: "hero" }, logo,
      h("h1", { class: "h1" }, brand ? brand.display_name : t("auth.welcome")),
      h("p", { class: "muted" }, t("auth.lead"))),
    form,
    h("div", { class: "row between" },
      h("a", { href: "#/signup", class: "link" }, t("auth.create")),
      h("a", { href: "#/forgot", class: "link" }, t("auth.forgot"))));
}

export function signup(invitation = null) {
  const nameF = field(t("auth.name"), { autocomplete: "name", required: true });
  const email = field(t("auth.email"), { type: "email", autocomplete: "email", dir: "ltr",
    required: true });
  const password = field(t("auth.password_new"), { type: "password",
    autocomplete: "new-password", required: true });
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("auth.create"));
  const done = h("div", { class: "card hidden" });
  const form = h("form", { class: "stack" }, nameF.el, email.el, password.el,
    h("p", { class: "small muted" }, t("auth.password_rule")), submit);
  form.addEventListener("submit", busy(submit, async () => {
    const r = await api("POST", "/api/account/signup", {
      email: email.input.value.trim(), password: password.input.value,
      display_name: nameF.input.value.trim(), language: lang,
      invitation_token: invitation || undefined }, { auth: false });
    form.classList.add("hidden");
    done.classList.remove("hidden");
    done.replaceChildren(h("p", {}, r.status === "active" ? t("auth.active") : t("auth.check_email")),
      h("a", { href: "#/login", class: "btn primary block" }, t("auth.sign_in")));
  }));
  return h("div", { class: "page" }, header(t("auth.create"), "#/login"),
    invitation ? h("p", { class: "chip accent" }, t("auth.invited")) : null, form, done);
}

export function forgot() {
  const email = field(t("auth.email"), { type: "email", dir: "ltr", required: true });
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("auth.send_link"));
  const form = h("form", { class: "stack" }, email.el, submit);
  form.addEventListener("submit", busy(submit, async () => {
    await api("POST", "/api/account/password/forgot", { email: email.input.value.trim() },
      { auth: false });
    form.replaceChildren(h("p", { class: "card" }, t("auth.check_email")));
  }));
  return h("div", { class: "page" }, header(t("auth.forgot"), "#/login"), form);
}

export function reset(token) {
  const password = field(t("auth.password_new"), { type: "password", autocomplete: "new-password" });
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("auth.save_password"));
  const form = h("form", { class: "stack" }, password.el,
    h("p", { class: "small muted" }, t("auth.password_rule")), submit);
  form.addEventListener("submit", busy(submit, async () => {
    await api("POST", "/api/account/password/reset",
      { token, new_password: password.input.value }, { auth: false });
    session.clear();
    toast(t("auth.password_changed"), "ok");
    go("#/login");
  }));
  return h("div", { class: "page" }, header(t("auth.reset"), "#/login"), form);
}

export function verify(token) {
  const box = h("div", { class: "card" }, loading());
  api("POST", "/api/account/verify", { token }, { auth: false })
    .then(() => box.replaceChildren(h("p", {}, t("auth.verified")),
      h("a", { href: "#/login", class: "btn primary block" }, t("auth.sign_in"))))
    .catch((err) => box.replaceChildren(h("p", {}, errorText(err))));
  return h("div", { class: "page" }, header(t("auth.verify"), "#/login"), box);
}

export function invitation(token) {
  if (!session.signedIn) {
    const page = signup(token);
    page.append(h("p", { class: "small muted center" }, t("auth.have_account"), " ",
      h("a", { href: "#/login", onclick: () => saved.set("app.after", `#/account/invitation/${token}`) },
        t("auth.sign_in"))));
    return page;
  }
  const box = h("div", { class: "card stack" }, loading());
  api("POST", "/api/account/invitations/accept", { token })
    .then((r) => {
      toast(t("invite.accepted"), "ok");
      go(r.kind === "device" ? `#/device/${r.device_id}` : "#/home");
    })
    .catch((err) => box.replaceChildren(h("p", {}, errorText(err))));
  return h("div", { class: "page" }, header(t("invite.title")), box);
}

// --- home ----------------------------------------------------------------------------------------

export function home_(state) {
  const grid = h("div", { class: "grid" });
  const stats = h("div", { class: "stats" });
  const scenes = h("div", { class: "scene-row" });
  const groupsBox = h("div");
  const favsBox = h("div");
  const rooms = h("div", { class: "tabs", role: "tablist", "aria-label": t("home.rooms") });
  const homeBtn = h("button", { class: "home-pick", "aria-label": t("home.pick") });
  const bell = h("a", { class: "icon-btn bell", href: "#/notifications", "aria-label": t("nav.notifications") },
    icon("bell"));
  const avatar = h("a", { class: "avatar", href: "#/me", "aria-label": t("nav.me") },
    (state.me?.display_name || "?").trim().charAt(0));
  const page = h("div", { class: "page" },
    h("div", { class: "row between" },
      h("div", { class: "stack tight" }, h("span", { class: "greet" }, t(greeting())), homeBtn),
      h("div", { class: "row" }, bell, avatar)),
    groupsBox, stats, scenes, favsBox, rooms, grid);
  let room = saved.get("app.room") || "all";
  let groupsKey = "";
  let homeList = [];

  async function draw() {
    const q = home.query();
    let devices;
    try {
      [devices, homeList] = await Promise.all([api("GET", `/api/platform/my/devices${q}`), homes()]);
    } catch (err) { grid.replaceChildren(h("p", { class: "muted" }, errorText(err))); return; }
    const current = homeList.find((m) => m.org_id === home.id);
    if (home.id && !current) { home.set(null); }
    homeBtn.replaceChildren(current ? homeName(current) : homeList.length === 1 ? homeName(homeList[0])
      : t("home.every"), icon("chevron"));
    homeBtn.onclick = async () => { await pickHome(homeList); draw(); };
    const [views, summary, notes, sceneRows] = await Promise.all([
      Promise.all(devices.slice(0, 60).map((d) =>
        api("GET", `/api/platform/my/devices/${d.device_id}/view`).catch(() => null))),
      api("GET", `/api/platform/my/summary${q}`).catch(() => null),
      api("GET", `/api/platform/my/notifications${q}`).catch(() => []),
      sceneOrg(homeList).then((org) => (org ? api("GET", `/api/platform/organizations/${org}/scenes`)
        .then((rows) => rows.map((s) => ({ ...s, org }))) : [])).catch(() => [])]);
    bell.classList.toggle("dot-on", notes.length > 0);
    const key = `${home.id}|${devices.map((d) => `${d.device_id}:${d.state}`).join(",")}`;
    if (key !== groupsKey && devices.length) {      // the groups row only when devices change
      groupsKey = key;
      groupsBox.replaceChildren(groupsRow(devices));
    }
    stats.replaceChildren(
      stat(t("home.temperature"), summary?.temperature !== null && summary?.temperature !== undefined
        ? `${num(summary.temperature)}°` : "—"),
      h("a", { class: "plain-link", href: "#/energy", "aria-label": t("energy.title") },
        summary?.energy_kwh !== null && summary?.energy_kwh !== undefined
          ? stat(t("home.energy_today"), num(summary.energy_kwh), "", "kWh")
          : stat(t("home.power_now"), summary?.power_kw !== null && summary?.power_kw !== undefined
            ? num(summary.power_kw) : "—", "", "kW")),
      stat(t("home.alerts"), num(summary?.alerts || 0), summary?.alerts ? "warn" : "",
        t("home.active_alerts")));
    scenes.replaceChildren(...sceneRows.slice(0, 3).map((s) => sceneButton(s)));
    favsBox.replaceChildren(...[favoritesRow(views, draw)].filter(Boolean));
    scenes.classList.toggle("hidden", !sceneRows.length);
    const names = [...new Set(devices.map((d) => d.room).filter(Boolean))].sort();
    const groups = [["all", t("home.all")], ...names.map((r) => [`room:${r}`, r])];
    if (devices.some((d) => d.access !== "owner")) groups.push(["shared", t("home.shared")]);
    if (!groups.some(([key]) => key === room)) room = "all";
    rooms.replaceChildren(...groups.map(([key, label]) => h("button", {
      class: `tab ${room === key ? "on" : ""}`, role: "tab", "aria-selected": room === key,
      onclick: () => { room = key; saved.set("app.room", key); draw(); } }, label)));
    rooms.classList.toggle("hidden", groups.length < 2);
    const shown = devices.map((d, i) => [d, views[i]]).filter(([d]) => room === "all" ||
      (room === "shared" && d.access !== "owner") || room === `room:${d.room}`);
    if (!devices.length) {
      grid.replaceChildren(empty());
      return;
    }
    grid.replaceChildren(...shown.map(([d, v]) => card(d, v, draw)));
  }
  draw();
  state.onLive = () => state.later(draw);          // a value or a state changed: redraw soon
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 30000);
  return page;
}

async function sceneOrg(list) {
  if (home.id) return home.id;
  const own = list.find((m) => m.personal) || list[0];
  return own ? own.org_id : null;
}

function sceneButton(s) {
  const btn = h("button", { class: `scene-btn tone-${SCENE_TONE[s.icon] || "accent"}`, onclick: async () => {
    btn.disabled = true;
    try {
      const r = await api("POST", `/api/platform/organizations/${s.org}/scenes/${s.scene_id}/run`);
      const bad = r.detail.filter((d) => d.tag && d.result !== "applied").length;
      toast(bad ? t("auto.scene_partial", { n: num(bad) }) : t("auto.scene_done"), bad ? "err" : "ok");
    } catch (err) { toast(errorText(err), "err"); }
    btn.disabled = false;
  } }, icon(s.icon || "power"), s.name);
  return btn;
}

function greeting() {
  const hour = new Date().getHours();
  return hour < 12 ? "home.morning" : hour < 18 ? "home.afternoon" : "home.evening";
}

function stat(label, value, kind = "", unit = "") {
  return h("div", { class: `stat ${kind}` }, h("span", { class: "tiny label" }, label),
    changed(h("b", {}, value, unit ? h("span", { class: "unit" }, ` ${unit}`) : null), `stat:${label}`, value));
}

function empty() {
  return h("div", { class: "card empty" }, icon("qr", "i lg"), h("p", {}, t("home.empty")),
    h("a", { href: "#/add", class: "btn primary" }, t("nav.add")));
}

function card(dev, view, redraw) {
  const outs = view?.tags.filter((x) => x.writable && x.data_type === "bool") || [];
  const reading = view?.tags.find((x) => typeof x.value === "number" && !x.writable);
  const onCount = outs.filter((x) => x.value === true).length;
  const isOn = onCount > 0;
  let state;
  if (!online(dev)) state = dev.last_seen ? t("device.offline_ago", { ago: when(dev.last_seen) }) : t("device.offline");
  else if (outs.length > 1) state = onCount ? t("device.on_count", { n: num(onCount), total: num(outs.length) }) : t("device.all_off");
  else if (outs.length === 1) {
    state = isOn ? t("device.on") : t("device.off");
    if (isOn && reading) state += ` · ${num(reading.value)}${reading.unit === "°C" ? "°" : ` ${reading.unit || ""}`}`;
  } else if (reading) state = `${num(reading.value)} ${reading.unit || ""}`;
  else state = t("device.online");
  const body = h("button", { class: `dev ${isOn && online(dev) ? "on" : ""} ${online(dev) ? "" : "off-line"}`,
    onclick: () => go(`#/device/${dev.device_id}`) },
  h("div", { class: "row between top" }, h("span", { class: "ico" }, icon(deviceIcon(dev, view), "i lg"))),
  h("div", { class: "stack tight" }, h("span", { class: "name" }, dev.name),
    changed(h("span", { class: `state ${online(dev) ? "" : "warn-text"}` }, state), `state:${dev.device_id}`, state)),
  h("div", { class: "row wrap" }, h("span", { class: "proto ltr" }, protoLabel(dev)),
    dev.access !== "owner" ? h("span", { class: "proto" }, t(`access.${dev.access}`)) : null));
  changed(body, `dev:${dev.device_id}`, isOn && online(dev));
  if (!outs.length || !online(dev)) return body;
  const sw = h("button", { class: `switch ${isOn ? "on" : ""}`, "aria-pressed": isOn,
    "aria-label": `${dev.name}: ${isOn ? t("device.turn_off") : t("device.turn_on")}`,
    onclick: async (ev) => {
      ev.stopPropagation();
      sw.classList.toggle("on", !isOn);              // answer the touch now; the device confirms
      body.classList.toggle("on", !isOn);
      body.classList.add("busy");
      sw.disabled = true;
      await Promise.all(outs.filter((x) => x.value !== !isOn).map((x) => command(dev.device_id, x.key, !isOn, outs.length > 1)));
      if (outs.length > 1) toast(t("device.done"), "ok");
      redraw();
    } }, h("span", { class: "track" }, h("span", { class: "knob" })));
  body.firstChild.append(sw);
  return body;
}

export async function command(deviceId, key, value, quiet = false) {
  try {
    const r = await api("POST", "/api/control/write",
      { tag: `platform.devices.${deviceId}.${key}`, value, reason: "app" });
    if (r.result === "applied") { if (!quiet) toast(t("device.done"), "ok"); }
    else if (r.status === "pending_confirmation") toast(t("device.pending"));
    else toast(t(`write.${r.result}`) !== `write.${r.result}` ? t(`write.${r.result}`) : r.message, "err");
    return r;
  } catch (err) {
    toast(errorText(err), "err");
    return null;
  }
}

// --- device --------------------------------------------------------------------------------------

export function device(state, id) {
  const page = h("div", { class: "page" }, loading());
  let chartTag = null;
  let editing = false;
  async function draw() {
    let view;
    try { view = await api("GET", `/api/platform/my/devices/${id}/view`); }
    catch (err) { page.replaceChildren(header(""), h("p", {}, errorText(err))); return; }
    const d = view.device;
    const outputs = view.tags.filter((x) => x.writable && x.data_type === "bool");
    const params = view.tags.filter((x) => x.writable && x.data_type !== "bool");
    const readings = view.tags.filter((x) => !x.writable);
    const sub = [d.room, view.product].filter(Boolean).join(" · ");
    const gear = h("a", { class: "icon-btn", href: `#/device/${id}/settings`, "aria-label": t("device.settings_page") },
      icon("gear"));
    const parts = [header(d.name, "#/home", sub, gear),
      h("div", { class: "row wrap" },
        h("span", { class: `chip ${online(d) ? "ok" : "warn"}` }, h("span", { class: "dot" }),
          online(d) ? t("device.online") : t("device.offline"),
          d.last_seen ? ` · ${when(d.last_seen)}` : ""),
        h("span", { class: "chip ltr" }, [protoLabel(d), d.firmware_version ? `FW ${d.firmware_version}` : null]
          .filter(Boolean).join(" · ")),
        d.warranty_until ? h("span", { class: "chip" }, t("device.warranty_until", { date: jalali(d.warranty_until) }))
          : null,
        d.access !== "owner" ? h("span", { class: "chip accent" }, t(`access.${d.access}`)) : null)];
    if (outputs.length) {
      const onCount = outputs.filter((x) => x.value === true).length;
      const allOn = h("button", { class: "btn primary" }, t("device.all_on"));
      allOn.addEventListener("click", busy(allOn, async () => {
        await Promise.all(outputs.filter((x) => x.value !== true).map((x) => command(id, x.key, true, true)));
        toast(t("device.done"), "ok");
        draw();
      }));
      const allOff = h("button", { class: "btn" }, t("device.all_off_btn"));
      allOff.addEventListener("click", busy(allOff, async () => {
        await Promise.all(outputs.filter((x) => x.value === true).map((x) => command(id, x.key, false, true)));
        toast(t("device.done"), "ok");
        draw();
      }));
      const operate = d.access !== "viewer";
      const naming = operate ? h("button", { class: `chip ${editing ? "accent" : ""}`, "aria-pressed": editing,
        onclick: () => { editing = !editing; draw(); } }, icon(editing ? "check" : "edit"),
      editing ? t("out.edit_done") : t("out.edit_mode")) : null;
      const sms = operate ? h("button", { class: `btn sms-btn ${online(d) ? "" : "urgent"}`,
        onclick: () => smsSheet(d, outputs, d.access === "owner") }, icon("chat"), t("sms.quick")) : null;
      parts.push(h("section", { class: "card stack big-card" },
        h("div", { class: "row between" }, h("h2", { class: "h2" }, t("device.outputs")),
          h("div", { class: "row" }, h("span", { class: "small accent-text" }, t("device.on_count",
            { n: num(onCount), total: num(outputs.length) })), naming)),
        editing ? h("p", { class: "tiny muted lh" }, t("out.edit_hint")) : null,
        h("div", { class: "outputs" }, outputs.map((x) => outButton(id, x, draw,
          { disabled: !editing && !online(d), editing }))),
        outputs.length > 1 ? h("div", { class: "two" }, allOn, allOff) : null, sms));
      if (!online(d)) {
        parts.push(h("a", { class: "card tight row link-card warn-card", href: `#/device/${id}/settings` },
          h("span", { class: "ico" }, icon("alert")), h("span", { class: "grow small" }, t("sms.offline_hint")),
          icon("next")));
      }
    }
    if (params.length) {
      parts.push(h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("device.settings")),
        params.map((x) => paramRow(id, x, draw))));
    }
    const chartable = readings.filter((x) => x.ui?.chart !== false && typeof x.value === "number");
    chartTag = chartable.find((x) => x.key === chartTag?.key) || chartable.find((x) => /kW|W$/.test(x.unit || ""))
      || chartable[0] || null;
    if (chartTag) parts.push(await chart(id, chartTag, chartable, (tag) => { chartTag = tag; draw(); }));
    const rest = readings.filter((x) => x !== chartTag);
    if (rest.length) {
      parts.push(h("section", { class: "stack" }, h("h2", { class: "h2" }, t("device.readings")),
        rest.map((x) => h("div", { class: "reading" },
          h("span", { class: "muted" }, name(x.display_name)),
          changed(h("b", {}, display(x), " ", h("span", { class: "small muted" }, x.unit || "")),
            `r:${id}:${x.key}`, x.value)))));
    }
    parts.push(commandsSection(view, draw));
    if (outputs.length && d.access !== "viewer") parts.push(timersSection(d, outputs, draw));
    parts.push(await history(id, view.tags));
    page.replaceChildren(...parts);
  }
  draw();
  state.onLive = (type, data) => {                // only this device's values and state
    const mine = type === "status" ? data.device_id === id
      : data.some((x) => x.tag.split(".")[2] === id);
    if (mine) state.later(draw);
  };
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 30000);
  return page;
}

async function chart(id, tag, choices, pick) {
  let points = [];
  try { points = await api("GET", `/api/platform/my/devices/${id}/history?tag=${tag.key}&hours=24&points=96`); }
  catch { /* no history yet */ }
  // only changes are stored: the value now continues the line up to the present
  if (points.length && typeof tag.value === "number") points.push({ ts: new Date().toISOString(), value: tag.value });
  const values = points.map((p) => p.value).filter((v) => typeof v === "number");
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 300 64");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", t("device.chart_label", { name: name(tag.display_name) }));
  if (values.length > 1) {
    const t0 = new Date(points[0].ts).getTime();
    const span = Math.max(1, new Date(points[points.length - 1].ts).getTime() - t0);
    const lo = Math.min(...values), hi = Math.max(...values);
    const y = (v) => 56 - ((v - lo) / ((hi - lo) || 1)) * 48;
    const xy = points.filter((p) => typeof p.value === "number").map((p) =>
      `${((new Date(p.ts).getTime() - t0) / span * 300).toFixed(1)} ${y(p.value).toFixed(1)}`);
    const line = `M${xy.join(" L")}`;
    const area = document.createElementNS(ns, "path");
    area.setAttribute("d", `${line} L300 64 L0 64 Z`);
    area.setAttribute("class", "area");
    const stroke = document.createElementNS(ns, "path");
    stroke.setAttribute("d", line);
    stroke.setAttribute("class", "line");
    svg.append(area, stroke);
  }
  const head = h("div", { class: "row between" },
    choices.length > 1 ? h("select", { class: "plain", "aria-label": t("device.chart_pick"), onchange: (ev) =>
      pick(choices.find((x) => x.key === ev.target.value)) },
    choices.map((x) => h("option", { value: x.key, selected: x.key === tag.key }, name(x.display_name))))
      : h("span", { class: "small strong" }, name(tag.display_name)),
    h("b", { class: "big-value" }, display(tag), " ", h("span", { class: "small muted" }, tag.unit || "")));
  return h("section", { class: "card stack tight-card" }, head,
    values.length > 1 ? svg : h("p", { class: "small muted" }, t("device.no_history")),
    h("div", { class: "row between tiny muted" }, h("span", {}, t("device.h24")), h("span", {}, t("device.now"))));
}

function paramRow(id, tag, redraw) {
  if (tag.states && Object.keys(tag.states).length) {
    const sel = h("select", { class: "input", "aria-label": name(tag.display_name) },
      Object.entries(tag.states).map(([v, label]) => h("option", { value: v, selected: Number(v) === Number(tag.value) }, label)));
    const save = h("button", { class: "btn" }, t("common.set"));
    save.addEventListener("click", busy(save, async () => { await command(id, tag.key, Number(sel.value)); redraw(); }));
    return h("div", { class: "stack" }, h("span", { class: "small muted" }, name(tag.display_name)),
      h("div", { class: "param" }, sel, save));
  }
  const input = h("input", { class: "input ltr", type: "number", value: tag.value ?? "",
    min: tag.min ?? undefined, max: tag.max ?? undefined, step: "any",
    "aria-label": name(tag.display_name) });
  const save = h("button", { class: "btn" }, t("common.set"));
  save.addEventListener("click", busy(save, async () => {
    await command(id, tag.key, Number(input.value));
    redraw();
  }));
  return h("div", { class: "stack" },
    h("span", { class: "small muted" }, name(tag.display_name),
      tag.min !== null ? ` (${num(tag.min)}–${num(tag.max)} ${tag.unit || ""})` : ""),
    h("div", { class: "param" }, input, save));
}

const CMD_BADGE = { confirmed: ["ok", "check"], executed: ["ok", "check"], failed: ["warn", "x"],
  expired: ["warn", "clock"] };

function source(reason) {
  if (!reason || reason === "app") return t("device.by_app");
  const m = reason.match(/^(automation|scene):\d+\s*(.*)$/);
  if (m) return t(m[1] === "automation" ? "device.by_automation" : "device.by_scene", { name: m[2] || "" });
  return reason;
}

async function history(id, tags) {
  let rows = [];
  try { rows = await api("GET", `/api/platform/my/devices/${id}/commands?limit=5`); }
  catch { /* history is optional */ }
  if (!rows.length) return h("span");
  return h("section", { class: "stack" }, h("h2", { class: "h2" }, t("device.commands")),
    rows.map((c) => {
      const [kind, ic] = CMD_BADGE[c.status] || ["wait", "clock"];
      const key = c.tag_path.split(".").pop();
      const tag = tags.find((x) => x.key === key);
      const what = typeof c.value === "boolean" ? t(c.value ? "device.turned_on" : "device.turned_off",
        { name: tag ? name(tag.display_name) : key }) : `${tag ? name(tag.display_name) : key} ← ${num(c.value)}`;
      return h("div", { class: "cmd" }, h("span", { class: `badge ${kind}` }, icon(ic)),
        h("div", { class: "stack tight grow" }, h("span", { class: "small" }, what),
          h("span", { class: "tiny muted" }, `${t(`command.${c.status}`)} · ${source(c.reason)} · ${when(c.created_at)}`)));
    }));
}

// --- device settings -----------------------------------------------------------------------------

export function deviceSettings(state, id) {
  const page = h("div", { class: "page" }, loading());
  async function draw() {
    let view, mine;
    try {
      [view, mine] = await Promise.all([api("GET", `/api/platform/my/devices/${id}/view`),
        api("GET", "/api/platform/my/devices")]);
    } catch (err) { page.replaceChildren(header("", `#/device/${id}`), h("p", {}, errorText(err))); return; }
    const d = view.device;
    const owner = d.access === "owner";
    const parts = [header(t("device.settings_page"), `#/device/${id}`, d.name)];

    // picture and facts
    const pic = h("span", { class: "pic" }, icon(deviceIcon(d, view), "i lg"));
    if (d.product_has_image) {
      imageUrl(`/api/platform/products/${d.product_id}/image`)
        .then((url) => pic.replaceChildren(h("img", { src: url, alt: view.product }))).catch(() => {});
    }
    parts.push(h("section", { class: "card stack" },
      h("div", { class: "row" }, pic, h("div", { class: "stack tight grow" }, h("span", { class: "h2" }, view.product),
        h("span", { class: "small muted ltr" }, d.identifier))),
      fact(t("device.protocol"), protoLabel(d), "ltr"),
      fact(t("device.firmware"), d.firmware_version || "—", "ltr"),
      fact(t("device.warranty"), d.warranty_until ? t("device.warranty_until", { date: jalali(d.warranty_until) })
        : d.warranty_months ? t("device.warranty_months", { n: num(d.warranty_months) }) : "—"),
      fact(t("device.access"), t(`access.${d.access}`)), brandFact(d)));

    // name and room
    const nameF = field(t("device.name"), { value: d.name, disabled: !owner });
    const rooms = [...new Set(mine.map((x) => x.room).filter(Boolean))].sort();
    const listId = `rooms-${id}`;
    const roomF = field(t("device.room"), { value: d.room || "", list: listId, placeholder: t("device.room_hint") });
    const save = h("button", { class: "btn primary block" }, t("common.save"));
    save.addEventListener("click", busy(save, async () => {
      if (owner && nameF.input.value.trim() && nameF.input.value.trim() !== d.name) {
        await api("PATCH", `/api/platform/my/devices/${id}`, { name: nameF.input.value.trim() });
      }
      await api("PUT", `/api/platform/my/devices/${id}/room`, { room: roomF.input.value.trim() || null });
      toast(t("common.saved"), "ok");
      draw();
    }));
    parts.push(h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("device.name_room")),
      nameF.el, roomF.el, h("datalist", { id: listId }, rooms.map((r) => h("option", { value: r }))), save));

    if (d.product_kind === "edge_hub" && owner) parts.push(edgeChildren(d, mine, draw));
    const outputs = view.tags.filter((x) => x.writable && x.data_type === "bool");
    if (d.protocol === "native" && d.access !== "viewer" && outputs.length) {
      parts.push(smsSection(d, outputs, owner));
    }
    if (d.protocol === "native") parts.push(localRules(d));
    if (owner) parts.push(ticketsSection(d));
    if (owner) parts.push(ownerTools(d));
    page.replaceChildren(...parts);
  }
  draw();
  return page;
}

/** The maker of the device as it presents itself (brand, support contact). */
function brandFact(d) {
  const box = h("div", { class: "reading hidden" });
  api("GET", `/api/public/brand/${d.maker_org_id}`, undefined, { auth: false }).then((b) => {
    box.classList.remove("hidden");
    box.replaceChildren(h("span", { class: "muted" }, t("device.maker")),
      h("span", { class: "stack tight end-text" }, h("span", {}, b.display_name),
        b.support_phone ? h("a", { class: "tiny ltr", href: `tel:${b.support_phone}` }, b.support_phone) : null,
        b.support_email ? h("a", { class: "tiny ltr", href: `mailto:${b.support_email}` }, b.support_email) : null));
  }).catch(() => box.remove());
  return box;
}

/** What the device decides by itself while cut off from the hub. */
function localRules(d) {
  const box = h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("local.title")));
  api("GET", `/api/platform/my/devices/${d.device_id}/local-rules`).then((r) => {
    box.append(h("p", { class: "small muted lh" }, t("local.help")),
      ...(r.rules.length ? r.rules.map((x) => h("div", { class: "reading" }, h("span", { class: "small" }, x.name),
        h("span", { class: "chip ok" }, t("local.on_device"))))
        : [h("p", { class: "small muted" }, t("local.none"))]),
      h("a", { class: "link", href: "#/auto/automations" }, t("local.how")));
  }).catch(() => box.remove());
  return box;
}

function fact(label, value, cls = "") {
  return h("div", { class: "reading" }, h("span", { class: "muted" }, label), h("span", { class: cls }, value));
}

function edgeChildren(hub, mine, redraw) {
  const children = mine.filter((x) => x.parent_device_id === hub.device_id);
  return h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("edge.behind")),
    children.length ? children.map((c) => {
      const remove = h("button", { class: "icon-btn", "aria-label": t("edge.remove") }, icon("trash"));
      remove.addEventListener("click", busy(remove, async () => {
        if (!confirm(t("edge.remove_confirm", { name: c.name }))) return;
        await api("DELETE", `/api/platform/my/devices/${hub.device_id}/children/${c.device_id}`);
        redraw();
      }));
      return h("div", { class: "reading" }, h("a", { href: `#/device/${c.device_id}` }, c.name),
        h("div", { class: "row" }, h("span", { class: "chip ltr" }, protoLabel(c)), remove));
    }) : h("p", { class: "small muted" }, t("edge.none")),
    h("a", { class: "btn block", href: `#/add/edge/${hub.device_id}` }, icon("plus"), t("edge.add")));
}

function ownerTools(d) {
  const email = field(t("share.email"), { type: "email", dir: "ltr" });
  const level = h("select", { class: "input", "aria-label": t("share.level") },
    h("option", { value: "operator" }, t("access.operator")),
    h("option", { value: "viewer" }, t("access.viewer")));
  const share = h("button", { class: "btn primary" }, t("share.send"));
  share.addEventListener("click", busy(share, async () => {
    await api("POST", `/api/platform/devices/${d.device_id}/shares`,
      { email: email.input.value.trim(), level: level.value });
    email.input.value = "";
    toast(t("share.sent"), "ok");
  }));
  const codeBox = h("div", { class: "stack hidden" });
  const transfer = h("button", { class: "btn" }, t("owner.transfer"));
  transfer.addEventListener("click", busy(transfer, async () => {
    if (!confirm(t("owner.transfer_confirm"))) return;
    const r = await api("POST", `/api/platform/my/devices/${d.device_id}/transfer`);
    codeBox.classList.remove("hidden");
    codeBox.replaceChildren(h("p", { class: "small muted" }, t("owner.transfer_hint")),
      h("div", { class: "code mono ltr" }, r.claim_code));
  }));
  const remove = h("button", { class: "btn danger" }, t("owner.remove"));
  remove.addEventListener("click", busy(remove, async () => {
    if (!confirm(t("owner.remove_confirm"))) return;
    await api("DELETE", `/api/platform/my/devices/${d.device_id}`);
    toast(t("owner.removed"), "ok");
    go("#/home");
  }));
  return h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("share.title")),
    sharesList(d), email.el, level, share, h("div", { class: "row wrap" }, transfer, remove), codeBox);
}

// --- add a device --------------------------------------------------------------------------------

export function add(state, prefill = {}) {
  const ident = field(t("add.identifier"), { dir: "ltr", value: prefill.identifier || "",
    autocapitalize: "characters", placeholder: "CTRL-8R-0001", class: "input code-input" });
  const code = field(t("add.code"), { dir: "ltr", value: prefill.code || "",
    autocapitalize: "characters", placeholder: "XXXX-XXXX-XXXX", class: "input code-input wide" });
  const label = field(t("add.name"), {});
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("add.submit"));
  const form = h("form", { class: "stack" }, ident.el, code.el, label.el, submit);
  form.addEventListener("submit", busy(submit, async () => {
    // into the selected home when the user owns it; else the personal home
    const target = home.id ? (await homes()).find((m) => m.org_id === home.id) : null;
    const organization = target && !target.personal && target.role_id === "customer_owner"
      ? target.org_id : undefined;
    const r = await api("POST", "/api/platform/claim", { identifier: ident.input.value.trim(),
      code: code.input.value.trim(), name: label.input.value.trim() || undefined, organization });
    toast(t("add.added"), "ok");
    go(`#/device/${r.device_id}/settings`);
  }));
  const frame = scanFrame();
  const scanArea = h("button", { class: "scan", type: "button", "aria-label": t("add.scan") }, frame);
  const canScan = "BarcodeDetector" in window && navigator.mediaDevices;
  const hint = h("span", { class: "small muted" }, canScan ? t("add.tap_to_scan") : t("add.scan_unavailable"));
  scanArea.addEventListener("click", busy(scanArea, async () => {
    if (!canScan) { toast(t("add.scan_unavailable"), "err"); return; }
    const found = await scan(scanArea, frame, state);
    const m = found && found.match(/#\/claim\/([^/]+)\/([^/?#]+)/);
    if (!m) { toast(t("add.not_a_label"), "err"); return; }
    ident.input.value = decodeURIComponent(m[1]);
    code.input.value = decodeURIComponent(m[2]);
    label.input.focus();
  }));
  return h("div", { class: "page" },
    h("div", { class: "row" }, h("a", { class: "icon-btn", href: "#/home", "aria-label": t("common.back") }, icon("back")),
      h("h1", { class: "h1 small-h1" }, t("nav.add"))),
    scanArea,
    h("div", { class: "stack tight center" }, h("span", { class: "strong" }, t("add.scan_title")),
      h("span", { class: "small muted lh" }, t("add.hint")), hint),
    h("div", { class: "divider" }, h("span"), t("add.or_manual"), h("span")),
    form,
    h("a", { class: "card tight row link-card", href: "#/add/edge" },
      h("span", { class: "ico ok-ico" }, icon("hub", "i lg")),
      h("span", { class: "stack tight grow" }, h("span", { class: "strong small" }, t("edge.link")),
        h("span", { class: "tiny muted ltr" }, "Modbus · OPC UA · SNMP · S7")),
      icon("next")));
}

function scanFrame() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 190 190");
  svg.setAttribute("class", "frame-svg");
  svg.setAttribute("aria-hidden", "true");
  const corners = document.createElementNS(ns, "path");
  corners.setAttribute("d", "M4 40V16a12 12 0 0 1 12-12h24M150 4h24a12 12 0 0 1 12 12v24M186 150v24a12 12 0 0 1-12 12h-24M40 186H16a12 12 0 0 1-12-12v-24");
  corners.setAttribute("class", "corners");
  const laser = document.createElementNS(ns, "path");
  laser.setAttribute("d", "M20 95h150");
  laser.setAttribute("class", "laser");
  const dots = document.createElementNS(ns, "path");
  dots.setAttribute("d", "M52 52h20M52 62h0M118 52h20M138 62h0M52 128h20M52 118h0M100 100h0M118 128h20M88 52h10M88 138h10M128 100h10");
  dots.setAttribute("class", "dots");
  svg.append(corners, laser, dots);
  return svg;
}

async function scan(area, frame, state) {
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
  const video = h("video", { playsinline: true, muted: true });
  video.srcObject = stream;
  area.replaceChildren(video, frame);
  await video.play();
  const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
  const stop = () => stream.getTracks().forEach((tr) => tr.stop());
  state.cleanup = stop;
  try {
    for (let i = 0; i < 300; i += 1) {
      const codes = await detector.detect(video);
      if (codes.length) return codes[0].rawValue;
      await new Promise((r) => setTimeout(r, 100));
    }
    return null;
  } finally {
    stop();
    area.replaceChildren(frame);
  }
}

/** Industrial equipment behind an edge hub: pick the hub, the product, its protocol address. */
export function addEdge(state, hubId = null) {
  const page = h("div", { class: "page" }, header(t("edge.link"), "#/add"), loading());
  (async () => {
    let mine, catalog, protocols;
    try {
      [mine, catalog, protocols] = await Promise.all([api("GET", "/api/platform/my/devices"),
        api("GET", "/api/platform/catalog"), api("GET", "/api/platform/protocols")]);
    } catch (err) { page.replaceChildren(header(t("edge.link"), "#/add"), h("p", {}, errorText(err))); return; }
    const hubs = mine.filter((d) => d.product_kind === "edge_hub" && d.access === "owner");
    if (!hubs.length) {
      page.replaceChildren(header(t("edge.link"), "#/add"), h("div", { class: "card empty" }, icon("hub", "i lg"),
        h("p", {}, t("edge.no_hub")), h("a", { class: "btn primary", href: "#/add" }, t("edge.add_hub_first"))));
      return;
    }
    const hubSel = h("select", { class: "input", "aria-label": t("edge.hub") },
      hubs.map((d) => h("option", { value: d.device_id, selected: d.device_id === hubId }, d.name)));
    const productSel = h("select", { class: "input", "aria-label": t("edge.product") },
      catalog.map((p) => h("option", { value: p.product_id }, `${p.name} · ${p.code}`)));
    const protoSel = h("select", { class: "input", "aria-label": t("edge.protocol") });
    const nameF = field(t("device.name"), { placeholder: t("edge.name_hint") });
    const fieldsBox = h("div", { class: "stack" });
    const values = {};
    const drawProtocols = () => {
      const p = catalog.find((x) => x.product_id === productSel.value);
      const list = (p?.protocols || []).filter((x) => x !== "native");
      protoSel.replaceChildren(...list.map((x) => h("option", { value: x }, t(`proto.${x}`) !== `proto.${x}` ? t(`proto.${x}`) : x)));
      drawFields();
    };
    const drawFields = () => {
      for (const k of Object.keys(values)) delete values[k];
      const spec = (protocols[protoSel.value]?.connection || []).filter((f) => f.required ||
        ["port", "unit_id"].includes(f.name));
      fieldsBox.replaceChildren(...spec.map((f) => {
        const numeric = f.type === "integer" || f.type === "number";
        const input = h("input", { class: "input ltr", type: numeric ? "number" : "text",
          placeholder: f.default !== undefined && f.default !== null ? String(f.default) : "",
          "aria-label": f.name, onchange: (ev) => {
            if (ev.target.value === "") delete values[f.name];
            else values[f.name] = numeric ? Number(ev.target.value) : ev.target.value;
          } });
        return h("div", { class: "field" }, h("label", {}, t(`conn.${f.name}`) !== `conn.${f.name}` ? t(`conn.${f.name}`) : f.name), input);
      }));
    };
    productSel.addEventListener("change", drawProtocols);
    protoSel.addEventListener("change", drawFields);
    drawProtocols();
    const submit = h("button", { class: "btn primary block" }, t("edge.add"));
    submit.addEventListener("click", busy(submit, async () => {
      const p = catalog.find((x) => x.product_id === productSel.value);
      const child = await api("POST", `/api/platform/my/devices/${hubSel.value}/children`, {
        product_id: p.product_id, version: p.latest_published, protocol: protoSel.value,
        name: nameF.input.value.trim() || undefined, connection: values, enabled: true });
      toast(t("edge.added"), "ok");
      go(`#/device/${child.device_id}`);
    }));
    page.replaceChildren(header(t("edge.link"), "#/add"),
      h("p", { class: "small muted lh" }, t("edge.lead")),
      h("section", { class: "card stack" },
        h("div", { class: "field" }, h("label", {}, t("edge.hub")), hubSel),
        h("div", { class: "field" }, h("label", {}, t("edge.product")), productSel),
        h("div", { class: "field" }, h("label", {}, t("edge.protocol")), protoSel),
        nameF.el, h("span", { class: "small strong" }, t("edge.address")), fieldsBox, submit));
  })();
  return page;
}

// --- notifications -------------------------------------------------------------------------------

const NOTE = { offline: ["warn", "alert"], command_failed: ["warn", "x"], command_expired: ["warn", "clock"],
  automation_failed: ["warn", "sliders"], automation_partial: ["wait", "sliders"] };

export function notifications() {
  const list = h("div", { class: "stack" }, loading());
  api("GET", `/api/platform/my/notifications${home.query()}`).then((rows) => {
    list.replaceChildren(...(rows.length ? rows.map((n) => {
      const [kind, ic] = NOTE[n.kind] || ["wait", "bell"];
      const text = t(`note.${n.kind}`, { device: n.device || "", automation: n.automation || "", tag: n.tag || "" });
      const body = h("div", { class: "cmd" }, h("span", { class: `badge ${kind}` }, icon(ic)),
        h("div", { class: "stack tight grow" }, h("span", { class: "small" }, text),
          h("span", { class: "tiny muted" }, when(n.at))));
      return n.device_id ? h("a", { class: "plain-link", href: `#/device/${n.device_id}` }, body) : body;
    }) : [h("div", { class: "card empty" }, icon("bell", "i lg"), h("p", {}, t("note.none")))]));
  }).catch((err) => list.replaceChildren(h("p", {}, errorText(err))));
  return h("div", { class: "page" }, header(t("nav.notifications")), list);
}

// --- me, organizations, members ------------------------------------------------------------------

const ACCENTS = [["", "#E8B86A"], ["teal", "#7FD1C7"], ["violet", "#A99BFF"], ["coral", "#FF9F7A"]];

export function me(state) {
  const orgs = h("div", { class: "stack" });
  const invites = h("div", { class: "stack" });
  const accentRow = h("div", { class: "row wrap" }, ACCENTS.map(([key, color]) => h("button", {
    class: `swatch sw-${key || "gold"} ${(document.documentElement.dataset.accent || "") === key ? "on" : ""}`,
    "aria-label": t("me.accent_x", { x: color }), onclick: () => { setAccent(key); go("#/me");
      window.dispatchEvent(new HashChangeEvent("hashchange")); } })));
  const page = h("div", { class: "page" },
    h("div", { class: "row" }, h("span", { class: "avatar lg" }, (state.me?.display_name || "?").trim().charAt(0)),
      h("div", { class: "stack tight grow" }, h("h1", { class: "h2" }, state.me?.display_name || ""),
        h("span", { class: "small muted ltr" }, state.me?.email || state.me?.username || ""))),
    invites,
    h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("me.homes")), orgs,
      h("button", { class: "btn block", onclick: async () => {
        try { if (await newHome()) { toast(t("homes.created"), "ok"); go("#/home"); } }
        catch (err) { toast(errorText(err), "err"); }
      } }, icon("plus"), t("homes.new"))),
    h("a", { class: "card tight row link-card", href: "#/energy" },
      h("span", { class: "ico" }, icon("bolt")), h("span", { class: "grow" }, t("energy.title")), icon("next")),
    h("a", { class: "card tight row link-card", href: "#/notifications" },
      h("span", { class: "ico" }, icon("bell")), h("span", { class: "grow" }, t("nav.notifications")), icon("next")),
    h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("me.preferences")),
      h("div", { class: "row between" }, h("span", { class: "row" }, icon("globe"), t("me.language")),
        h("button", { class: "btn", onclick: async () => {
          await loadLanguage(lang === "fa" ? "en" : "fa");
          go("#/me");
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        } }, lang === "fa" ? "English" : "فارسی")),
      h("div", { class: "row between" }, h("span", { class: "row" }, icon("moon"), t("me.theme")),
        h("button", { class: "btn", onclick: () => {
          const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
          document.documentElement.dataset.theme = next;
          saved.set("app.theme", next);
        } }, t("me.toggle"))),
      h("div", { class: "row between" }, h("span", { class: "row" }, icon("star"), t("me.accent")), accentRow),
      h("div", { class: "row between" }, h("span", { class: "row" }, icon("home"), t("me.presence")),
        h("button", { class: "btn", onclick: (ev) => {
          const off = saved.get("app.presence") === "off";
          saved.set("app.presence", off ? null : "off");
          ev.target.textContent = off ? t("me.on") : t("me.off");
        } }, saved.get("app.presence") === "off" ? t("me.off") : t("me.on"))),
      h("p", { class: "tiny muted lh" }, t("me.presence_help"))),
    h("button", { class: "btn danger block", onclick: async () => { await logout(); go("#/login"); } },
      icon("out"), " ", t("me.sign_out")),
    h("button", { class: "link danger-link", onclick: async () => {
      try {
        if (await deleteAccount()) { await logout(); toast(t("account.deleted"), "ok"); go("#/login"); }
      } catch (err) { toast(errorText(err), "err"); }
    } }, t("account.delete")));
  homes().then((rows) => {
    orgs.replaceChildren(...rows.map((m) => h("div", { class: "reading" },
      h("button", { class: "link", onclick: () => { home.set(m.org_id); go("#/home"); } },
        h("span", { class: "row" }, icon("home"), homeName(m))),
      h("div", { class: "row" }, h("span", { class: "chip" }, t(`roles.${m.role_id}`)),
        h("a", { class: "icon-btn", href: `#/home-settings/${m.org_id}`, "aria-label": t("homeset.title") },
          icon("gear")),
        m.role_id === "customer_owner" ? h("a", { class: "icon-btn", href: `#/members/${m.org_id}`,
          "aria-label": t("members.title") }, icon("people")) : null))));
  }).catch(() => {});
  api("GET", "/api/account/invitations").then((rows) => {
    invites.replaceChildren(...rows.map((i) => h("div", { class: "card tight row between" },
      h("span", { class: "small" }, i.kind === "device" ? t("invite.device", { org: i.org_name })
        : t("invite.organization", { org: i.org_name })),
      h("span", { class: "chip accent" }, t("invite.open_email")))));
  }).catch(() => {});
  return page;
}

export function members(state, orgId) {
  const list = h("div", { class: "stack" });
  const email = field(t("share.email"), { type: "email", dir: "ltr" });
  const role = h("select", { class: "input", "aria-label": t("members.role") },
    ["customer_member", "customer_viewer", "customer_owner"].map((r) =>
      h("option", { value: r }, t(`roles.${r}`))));
  const invite = h("button", { class: "btn primary" }, t("members.invite"));
  invite.addEventListener("click", busy(invite, async () => {
    await api("POST", `/api/platform/organizations/${orgId}/invitations`,
      { email: email.input.value.trim(), role: role.value });
    email.input.value = "";
    toast(t("share.sent"), "ok");
  }));
  async function draw() {
    let rows;
    try { rows = await api("GET", `/api/platform/organizations/${orgId}/members`); }
    catch (err) { list.replaceChildren(h("p", {}, errorText(err))); return; }
    list.replaceChildren(...rows.map((m) => h("div", { class: "reading" },
      h("div", { class: "stack tight" }, h("span", {}, m.display_name),
        h("span", { class: "tiny muted ltr" }, m.email || "")),
      h("div", { class: "row" }, h("span", { class: "chip" }, t(`roles.${m.role_id}`)),
        h("button", { class: "icon-btn", "aria-label": t("members.remove"), onclick: async () => {
          if (!confirm(t("members.remove_confirm"))) return;
          try {
            await api("DELETE", `/api/platform/organizations/${orgId}/members/${m.user_role_id}`);
            draw();
          } catch (err) { toast(errorText(err), "err"); }
        } }, icon("x"))))));
  }
  draw();
  return h("div", { class: "page" }, header(t("members.title"), "#/me"), list,
    h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("members.invite")),
      email.el, role, invite));
}

export { home_ as home };
