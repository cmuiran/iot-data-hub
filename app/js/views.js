// Screens of the consumer app. Each returns a DOM node; `go(route)` navigates.

import { api, errorText, h, icon, lang, loadLanguage, logout, name, num, saved, session, t,
  toast, when } from "./core.js";

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

function header(title, back = "#/home") {
  return h("div", { class: "row between" },
    h("a", { class: "icon-btn", href: back, "aria-label": t("common.back") }, icon("back")),
    h("h1", { class: "h2" }, title), h("span", { class: "icon-btn hidden" }));
}

export function nav(active) {
  const item = (route, key, ic) => h("a", { href: `#/${route}`, class: active === route ? "on" : "" },
    icon(ic), t(key));
  return h("nav", { class: "nav", "aria-label": t("nav.label") },
    item("home", "nav.home", "home"),
    item("auto", "nav.auto", "clock"),
    h("a", { href: "#/add", class: "fab", "aria-label": t("nav.add") }, icon("plus", "i lg")),
    item("me", "nav.me", "user"));
}

const KIND_ICON = { edge_hub: "hub" };
function deviceIcon(dev) { return KIND_ICON[dev.product_kind] || "relay"; }

function online(dev) { return dev.state === "online"; }

// --- sign-in and account links -------------------------------------------------------------------

export function login() {
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
  return h("div", { class: "page" },
    h("div", { class: "hero" }, h("div", { class: "brand" }, icon("home", "i lg")),
      h("h1", { class: "h1" }, t("auth.welcome")), h("p", { class: "muted" }, t("auth.lead"))),
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
  const box = h("div", { class: "card" }, t("common.loading"));
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
  const box = h("div", { class: "card stack" }, t("common.loading"));
  api("POST", "/api/account/invitations/accept", { token })
    .then((r) => {
      toast(t("invite.accepted"), "ok");
      go(r.kind === "device" ? `#/device/${r.device_id}` : "#/home");
    })
    .catch((err) => box.replaceChildren(h("p", {}, errorText(err))));
  return h("div", { class: "page" }, header(t("invite.title")), box);
}

// --- home ----------------------------------------------------------------------------------------

export function home(state) {
  const grid = h("div", { class: "grid" });
  const stats = h("div", { class: "stats" });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const page = h("div", { class: "page" },
    h("div", { class: "row between" },
      h("div", { class: "stack" },
        h("span", { class: "greet" }, t(greeting())),
        h("h1", { class: "h1" }, state.me ? state.me.display_name : "")),
      h("a", { class: "icon-btn", href: "#/me", "aria-label": t("nav.me") }, icon("user"))),
    stats, tabs, grid);
  let filter = saved.get("app.filter") || "all";

  async function draw() {
    let devices;
    try { devices = await api("GET", "/api/platform/my/devices"); }
    catch (err) { grid.replaceChildren(h("p", { class: "muted" }, errorText(err))); return; }
    const views = await Promise.all(devices.slice(0, 60).map((d) =>
      api("GET", `/api/platform/my/devices/${d.device_id}/view`).catch(() => null)));
    const groups = [["all", t("home.all")], ["online", t("home.online")],
      ["shared", t("home.shared")]];
    tabs.replaceChildren(...groups.map(([key, label]) => h("button", {
      class: `tab ${filter === key ? "on" : ""}`, role: "tab", "aria-selected": filter === key,
      onclick: () => { filter = key; saved.set("app.filter", key); draw(); } }, label)));
    const on = devices.filter(online).length;
    stats.replaceChildren(
      stat(t("home.devices"), num(devices.length)), stat(t("home.online"), num(on)),
      stat(t("home.offline"), num(devices.length - on), devices.length - on ? "warn" : ""));
    const shown = devices.map((d, i) => [d, views[i]]).filter(([d]) =>
      filter === "all" || (filter === "online" && online(d)) ||
      (filter === "shared" && d.access !== "owner"));
    if (!devices.length) {
      grid.replaceWith(empty());
      return;
    }
    grid.replaceChildren(...shown.map(([d, v]) => card(d, v, draw)));
  }
  draw();
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 5000);
  return page;
}

function greeting() {
  const hour = new Date().getHours();
  return hour < 12 ? "home.morning" : hour < 18 ? "home.afternoon" : "home.evening";
}

function stat(label, value, kind = "") {
  return h("div", { class: `stat ${kind}` }, h("span", { class: "tiny muted" }, label), h("b", {}, value));
}

function empty() {
  return h("div", { class: "card empty" }, icon("qr", "i lg"), h("p", {}, t("home.empty")),
    h("a", { href: "#/add", class: "btn primary" }, t("nav.add")));
}

function card(dev, view, redraw) {
  const toggleTag = view?.tags.find((x) => x.writable && x.data_type === "bool");
  const reading = view?.tags.find((x) => typeof x.value === "number");
  const isOn = toggleTag ? toggleTag.value === true : false;
  const state = !online(dev) ? t("device.offline") :
    toggleTag ? (isOn ? t("device.on") : t("device.off")) :
    reading ? `${num(reading.value)} ${reading.unit || ""}` : t("device.online");
  const body = h("button", { class: `dev ${isOn ? "on" : ""} ${online(dev) ? "" : "off-line"}`,
    onclick: () => go(`#/device/${dev.device_id}`) },
    h("div", { class: "row between" }, h("span", { class: "ico" }, icon(deviceIcon(dev), "i lg"))),
    h("div", { class: "stack" }, h("span", { class: "name" }, dev.name),
      h("span", { class: "state" }, state)),
    dev.access !== "owner" ? h("span", { class: "chip" }, t(`access.${dev.access}`)) : null);
  if (!toggleTag) return body;
  const sw = h("button", { class: `switch ${isOn ? "on" : ""}`, "aria-pressed": isOn,
    "aria-label": `${name(toggleTag.display_name)}: ${isOn ? t("device.turn_off") : t("device.turn_on")}`,
    onclick: async (ev) => {
      ev.stopPropagation();
      sw.disabled = true;
      await command(dev.device_id, toggleTag.key, !isOn);
      redraw();
    } }, h("span", { class: "track" }, h("span", { class: "knob" })));
  body.firstChild.append(sw);
  return body;
}

export async function command(deviceId, key, value) {
  try {
    const r = await api("POST", "/api/control/write",
      { tag: `platform.devices.${deviceId}.${key}`, value, reason: "app" });
    if (r.result === "applied") toast(t("device.done"), "ok");
    else if (r.status === "pending_confirmation") toast(t("device.pending"));
    else toast(t(`write.${r.result}`) !== `write.${r.result}` ? t(`write.${r.result}`) : r.message,
      "err");
    return r;
  } catch (err) {
    toast(errorText(err), "err");
    return null;
  }
}

// --- device --------------------------------------------------------------------------------------

export function device(state, id) {
  const page = h("div", { class: "page" }, t("common.loading"));
  async function draw() {
    let view;
    try { view = await api("GET", `/api/platform/my/devices/${id}/view`); }
    catch (err) { page.replaceChildren(header(""), h("p", {}, errorText(err))); return; }
    const d = view.device;
    const outputs = view.tags.filter((x) => x.writable && x.data_type === "bool");
    const params = view.tags.filter((x) => x.writable && x.data_type !== "bool");
    const readings = view.tags.filter((x) => !x.writable);
    const parts = [header(d.name),
      h("div", { class: "row wrap" },
        h("span", { class: `chip ${online(d) ? "ok" : "warn"}` }, h("span", { class: "dot" }),
          online(d) ? t("device.online") : t("device.offline"),
          d.last_seen ? ` · ${when(d.last_seen)}` : ""),
        h("span", { class: "chip" }, view.product),
        h("span", { class: "chip ltr mono" }, d.identifier),
        d.access !== "owner" ? h("span", { class: "chip accent" }, t(`access.${d.access}`)) : null)];
    if (outputs.length) {
      const onCount = outputs.filter((x) => x.value === true).length;
      parts.push(h("section", { class: "card stack" },
        h("div", { class: "row between" }, h("h2", { class: "h2" }, t("device.outputs")),
          h("span", { class: "small muted" }, t("device.on_count",
            { n: num(onCount), total: num(outputs.length) }))),
        h("div", { class: "outputs" }, outputs.map((x) => {
          const btn = h("button", { class: `out ${x.value === true ? "on" : ""}`,
            "aria-pressed": x.value === true, "aria-label": name(x.display_name),
            onclick: async () => {
              btn.classList.add("busy");
              await command(id, x.key, x.value !== true);
              draw();
            } }, icon("power"), name(x.display_name));
          return btn;
        }))));
    }
    if (params.length) {
      parts.push(h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("device.settings")),
        params.map((x) => paramRow(id, x, draw))));
    }
    if (readings.length) {
      parts.push(h("section", { class: "stack" }, h("h2", { class: "h2" }, t("device.readings")),
        readings.map((x) => h("div", { class: "reading" },
          h("span", { class: "muted" }, name(x.display_name)),
          h("b", {}, display(x), " ", h("span", { class: "small muted" }, x.unit || ""))))));
    }
    parts.push(await history(id, view.tags));
    if (d.access === "owner") parts.push(ownerTools(d));
    page.replaceChildren(...parts);
  }
  draw();
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 5000);
  return page;
}

function display(tag) {
  if (tag.value === null || tag.value === undefined) return "—";
  if (tag.states && String(Number(tag.value)) in tag.states) return tag.states[Number(tag.value)];
  if (typeof tag.value === "boolean") return tag.value ? t("device.yes") : t("device.no");
  return num(tag.value);
}

function paramRow(id, tag, redraw) {
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
      return h("div", { class: "cmd" }, h("span", { class: `badge ${kind}` }, icon(ic)),
        h("div", { class: "stack grow" },
          h("span", { class: "small" }, `${tag ? name(tag.display_name) : key} → ${typeof c.value === "boolean"
            ? (c.value ? t("device.on") : t("device.off")) : num(c.value)}`),
          h("span", { class: "tiny muted" }, `${t(`command.${c.status}`)} · ${when(c.created_at)}`)));
    }));
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
    email.el, level, share, h("div", { class: "row wrap" }, transfer, remove), codeBox);
}

// --- add a device --------------------------------------------------------------------------------

export function add(state, prefill = {}) {
  const ident = field(t("add.identifier"), { dir: "ltr", value: prefill.identifier || "",
    autocapitalize: "characters", placeholder: "CTRL-8R-0001" });
  const code = field(t("add.code"), { dir: "ltr", value: prefill.code || "",
    autocapitalize: "characters", placeholder: "XXXX-XXXX-XXXX" });
  const label = field(t("add.name"), {});
  const submit = h("button", { class: "btn primary block", type: "submit" }, t("add.submit"));
  const form = h("form", { class: "stack" }, ident.el, code.el, label.el, submit);
  form.addEventListener("submit", busy(submit, async () => {
    const r = await api("POST", "/api/platform/claim", { identifier: ident.input.value.trim(),
      code: code.input.value.trim(), name: label.input.value.trim() || undefined });
    toast(t("add.added"), "ok");
    go(`#/device/${r.device_id}`);
  }));
  const scanArea = h("div", { class: "scan" }, icon("qr", "i lg"));
  const scanBtn = h("button", { class: "btn block" }, t("add.scan"));
  if (!("BarcodeDetector" in window) || !navigator.mediaDevices) {
    scanBtn.disabled = true;
    scanBtn.textContent = t("add.scan_unavailable");
  }
  scanBtn.addEventListener("click", busy(scanBtn, async () => {
    const found = await scan(scanArea, state);
    const m = found && found.match(/#\/claim\/([^/]+)\/([^/?#]+)/);
    if (!m) { toast(t("add.not_a_label"), "err"); return; }
    ident.input.value = decodeURIComponent(m[1]);
    code.input.value = decodeURIComponent(m[2]);
    label.input.focus();
  }));
  return h("div", { class: "page" }, header(t("nav.add")), scanArea, scanBtn,
    h("p", { class: "small muted center" }, t("add.hint")), form);
}

async function scan(area, state) {
  const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
  const video = h("video", { playsinline: true, muted: true });
  video.srcObject = stream;
  area.replaceChildren(video, h("span", { class: "frame" }));
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
    area.replaceChildren(icon("qr", "i lg"));
  }
}

// --- me, organizations, members ------------------------------------------------------------------

export function me(state) {
  const orgs = h("div", { class: "stack" });
  const invites = h("div", { class: "stack" });
  const page = h("div", { class: "page" },
    h("div", { class: "row" }, h("span", { class: "brand" }, icon("user", "i lg")),
      h("div", { class: "stack grow" }, h("h1", { class: "h2" }, state.me?.display_name || ""),
        h("span", { class: "small muted ltr" }, state.me?.email || state.me?.username || ""))),
    invites,
    h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("me.homes")), orgs),
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
        } }, t("me.toggle")))),
    h("button", { class: "btn danger block", onclick: async () => { await logout(); go("#/login"); } },
      icon("out"), " ", t("me.sign_out")));
  api("GET", "/api/account/organizations").then((rows) => {
    const byOrg = new Map();
    for (const m of rows) if (!byOrg.has(m.org_id)) byOrg.set(m.org_id, m);
    orgs.replaceChildren(...[...byOrg.values()].map((m) => h("a", {
      class: "reading", href: m.role_id === "customer_owner" || m.role_id === "manufacturer_admin"
        ? `#/members/${m.org_id}` : "#/me" },
      h("span", {}, m.personal ? t("me.my_home") : m.name),
      h("span", { class: "chip" }, t(`roles.${m.role_id}`)))));
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
      h("div", { class: "stack" }, h("span", {}, m.display_name),
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
