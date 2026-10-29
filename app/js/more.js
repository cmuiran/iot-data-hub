// The app's second round: device groups (combined or one device / one model at a time),
// energy and cost, a home's place and tariff, support tickets and warranty claims, the SMS
// fallback, countdown timers, commands with several parameters, shares, deleting the account.

import { api, changed, loading, dialog, errorText, h, home, icon, lang, name, num, saved, t, toast, when } from "./core.js";
import { outButton, sectionCard, sectionDialog } from "./outputs.js";

export const go = (route) => { location.hash = route; };

// replaceChildren / append, leaving out the parts a screen does not have (null, false)
const present = (x) => x !== null && x !== undefined && x !== false;
function fill(el, ...kids) { el.replaceChildren(...kids.flat().filter(present)); }
function put(el, ...kids) { el.append(...kids.flat().filter(present)); }

function busy(button, task) {
  return async (ev) => {
    ev?.preventDefault?.();
    button.disabled = true;
    try { await task(); } catch (err) { toast(errorText(err), "err"); }
    finally { button.disabled = false; }
  };
}

function header(title, back, sub = null, right = null) {
  return h("div", { class: "row between top-bar" },
    h("a", { class: "icon-btn", href: back, "aria-label": t("common.back") }, icon("back")),
    h("div", { class: "stack tight center grow" }, h("h1", { class: "h2" }, title),
      sub ? h("span", { class: "small muted" }, sub) : null),
    right || h("span", { class: "icon-btn hidden" }));
}

function labelled(label, control) {
  return h("div", { class: "field" }, h("span", { class: "label" }, label), control);
}

function input(attrs = {}) { return h("input", { class: "input", ...attrs }); }

function jalali(iso, time = false) {
  return new Intl.DateTimeFormat(lang === "fa" ? "fa-IR-u-ca-persian" : "en-GB",
    { dateStyle: "medium", ...(time ? { timeStyle: "short" } : {}), timeZone: "Asia/Tehran" })
    .format(new Date(iso));
}

export async function command(deviceId, key, value, quiet = false) {
  try {
    const r = await api("POST", "/api/control/write",
      { tag: `platform.devices.${deviceId}.${key}`, value, reason: "app" });
    if (r.result === "applied") { if (!quiet) toast(t("device.done"), "ok"); }
    else toast(t(`write.${r.result}`) !== `write.${r.result}` ? t(`write.${r.result}`) : r.message, "err");
    return r;
  } catch (err) { toast(errorText(err), "err"); return null; }
}

// --- groups --------------------------------------------------------------------------------------

export async function homeOrg() {
  if (home.id) return home.id;
  const rows = await api("GET", "/api/account/organizations");
  const own = rows.find((m) => m.personal) || rows[0];
  return own ? own.org_id : null;
}

/** The row of groups above the tiles on the home screen. */
export function groupsRow(devices) {
  const row = h("div", { class: "group-row" });
  (async () => {
    const org = await homeOrg();
    if (!org) return;
    const groups = await api("GET", `/api/platform/organizations/${org}/groups`).catch(() => []);
    const byId = new Map(devices.map((d) => [d.device_id, d]));
    fill(row, ...groups.map((g) => {
      const members = g.device_ids.map((id) => byId.get(id)).filter(Boolean);
      const on = members.filter((d) => d.state === "online").length;
      return h("a", { class: "group-card", href: `#/group/${g.group_id}` },
        h("span", { class: "ico" }, icon(g.icon || "relay")),
        h("span", { class: "stack tight" }, h("span", { class: "name" }, g.name),
          h("span", { class: "tiny muted" }, t("groups.count", { n: num(members.length), on: num(on) }))));
    }), h("button", { class: "group-card add", onclick: () => groupBuilder(org, devices, null, () => go("#/home")) },
      h("span", { class: "ico" }, icon("plus")), h("span", { class: "name" }, t("groups.new"))));
  })();
  return row;
}

export async function groupBuilder(org, devices, group, done) {
  const nameIn = input({ value: group?.name || "", placeholder: t("groups.name_hint"), "aria-label": t("groups.name") });
  const chosen = new Set(group?.device_ids || []);
  const list = h("div", { class: "stack" }, devices.map((d) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(d.device_id), onchange: () => {
      if (box.checked) chosen.add(d.device_id); else chosen.delete(d.device_id);
    } });
    return h("label", { class: "reading check-row" }, h("span", { class: "row" }, box, d.name),
      h("span", { class: "tiny muted" }, d.product_name || ""));
  }));
  const buttons = [["cancel", t("common.cancel")], ["save", t("common.save"), "primary"]];
  if (group) buttons.unshift(["delete", t("auto.delete"), "danger"]);
  for (;;) {
    const answer = await dialog(group ? t("groups.edit") : t("groups.new"),
      h("div", { class: "stack" }, labelled(t("groups.name"), nameIn),
        h("span", { class: "label" }, t("groups.devices")), list), buttons);
    if (answer === "delete") {
      await api("DELETE", `/api/platform/organizations/${org}/groups/${group.group_id}`).catch((e) => toast(errorText(e), "err"));
      done();
      return;
    }
    if (answer !== "save") return;
    if (!chosen.size) { toast(t("groups.pick_one"), "err"); continue; }
    const body = { name: nameIn.value.trim() || t("groups.untitled"), device_ids: [...chosen], icon: "relay" };
    try {
      if (group) await api("PUT", `/api/platform/organizations/${org}/groups/${group.group_id}`, body);
      else await api("POST", `/api/platform/organizations/${org}/groups`, body);
      toast(t("groups.saved"), "ok");
      done();
      return;
    } catch (err) { toast(errorText(err), "err"); }
  }
}

/** A group: every member's inputs and outputs together, or one device / one model only. */
export function groupPage(state, groupId) {
  const page = h("div", { class: "page" }, loading());
  let filter = saved.get(`app.group.${groupId}`) || "all";
  let editing = false;
  async function draw() {
    let org, group, devices;
    try {
      org = await homeOrg();
      const groups = await api("GET", `/api/platform/organizations/${org}/groups`);
      group = groups.find((g) => String(g.group_id) === String(groupId));
      devices = await api("GET", `/api/platform/my/devices${home.query()}`);
    } catch (err) { fill(page, header("", "#/home"), h("p", {}, errorText(err))); return; }
    if (!group) { fill(page, header("", "#/home"), h("p", {}, t("groups.gone"))); return; }
    const members = devices.filter((d) => group.device_ids.includes(d.device_id));
    const views = (await Promise.all(members.map((d) =>
      api("GET", `/api/platform/my/devices/${d.device_id}/view`).catch(() => null)))).filter(Boolean);
    const models = [...new Map(views.map((v) => [v.device.product_id, v.product])).entries()];
    const chips = [["all", t("groups.combined")],
      ...views.map((v) => [`device:${v.device.device_id}`, v.device.name]),
      ...(models.length > 1 ? models.map(([pid, label]) => [`model:${pid}`, t("groups.model", { name: label })]) : [])];
    if (!chips.some(([k]) => k === filter)) filter = "all";
    const shown = views.filter((v) => filter === "all" ||
      filter === `device:${v.device.device_id}` || filter === `model:${v.device.product_id}`);
    const combined = shown.length > 1;
    const outs = shown.flatMap((v) => v.tags.filter((x) => x.writable && x.data_type === "bool")
      .map((x) => ({ v, x })));
    const reads = shown.flatMap((v) => v.tags.filter((x) => !x.writable).map((x) => ({ v, x })));
    const onCount = outs.filter(({ x }) => x.value === true).length;
    const edit = h("button", { class: "icon-btn", "aria-label": t("groups.edit"),
      onclick: () => groupBuilder(org, devices, group, draw) }, icon("gear"));
    // the group's named parts first (each with its outputs), then whatever is in none of them
    const ref = (d, k) => `${d}:${k}`;
    const byRef = new Map(outs.map((o) => [ref(o.v.device.device_id, o.x.key), o]));
    const sections = (group.sections || []).map((sec, i) => ({ sec, i,
      outs: sec.outputs.map((o) => byRef.get(ref(o.device_id, o.key))).filter(Boolean) }))
      .filter(({ outs: mine }) => filter === "all" || mine.length);
    const inParts = new Set(sections.flatMap(({ outs: mine }) => mine.map(({ v, x }) => ref(v.device.device_id, x.key))));
    const rest = outs.filter(({ v, x }) => !inParts.has(ref(v.device.device_id, x.key)));
    const addPart = h("button", { class: "btn block dashed-btn" }, icon("plus"), t("groups.add_section"));
    addPart.addEventListener("click", async () => { if (await sectionDialog(org, group, views)) draw(); });
    const naming = h("button", { class: `chip ${editing ? "accent" : ""}`, "aria-pressed": editing,
      onclick: () => { editing = !editing; draw(); } }, icon(editing ? "check" : "edit"),
    editing ? t("out.edit_done") : t("out.edit_mode"));
    const allSet = (value) => busy(allOnBtn, async () => {
      await Promise.all(outs.filter(({ x }) => x.value !== value)
        .map(({ v, x }) => command(v.device.device_id, x.key, value, true)));
      toast(t("device.done"), "ok");
      draw();
    });
    const allOnBtn = h("button", { class: "btn primary" }, t("device.all_on"));
    allOnBtn.addEventListener("click", allSet(true));
    const allOffBtn = h("button", { class: "btn" }, t("device.all_off_btn"));
    allOffBtn.addEventListener("click", allSet(false));
    fill(page, 
      header(group.name, "#/home", t("groups.count", { n: num(members.length),
        on: num(members.filter((d) => d.state === "online").length) }), edit),
      h("div", { class: "tabs", role: "tablist" }, chips.map(([key, label]) => h("button", {
        class: `tab ${filter === key ? "on" : ""}`, role: "tab", "aria-selected": filter === key,
        onclick: () => { filter = key; saved.set(`app.group.${groupId}`, key); draw(); } }, label))),
      outs.length ? h("div", { class: "row between" }, h("h2", { class: "h2" }, t("groups.sections")), naming) : null,
      editing ? h("p", { class: "tiny muted lh" }, t("out.edit_hint")) : null,
      ...sections.map(({ sec, i, outs: mine }) => sectionCard(sec, mine, draw,
        async () => { if (await sectionDialog(org, group, views, i)) draw(); }, editing)),
      outs.length ? addPart : null,
      rest.length ? h("section", { class: "card stack big-card" },
        h("div", { class: "row between" }, h("h2", { class: "h2" },
          sections.length ? t("groups.others") : t("device.outputs")),
        h("span", { class: "small accent-text" }, t("device.on_count", { n: num(onCount), total: num(outs.length) }))),
        h("div", { class: "outputs" }, rest.map(({ v, x }) => outButton(v.device.device_id, x, draw,
          { sub: combined ? v.device.name : null, disabled: !editing && v.device.state !== "online", editing }))),
        h("div", { class: "two" }, allOnBtn, allOffBtn)) : null,
      outs.length && !rest.length ? h("div", { class: "two" }, allOnBtn, allOffBtn) : null,
      reads.length ? h("section", { class: "stack" }, h("h2", { class: "h2" }, t("device.readings")),
        reads.map(({ v, x }) => h("a", { class: "reading plain-link", href: `#/device/${v.device.device_id}` },
          h("span", { class: "stack tight" }, h("span", { class: "muted" }, name(x.display_name)),
            combined ? h("span", { class: "tiny muted" }, v.device.name) : null),
          h("b", {}, x.value === null || x.value === undefined ? "—" : typeof x.value === "boolean"
            ? (x.value ? t("device.on") : t("device.off")) : num(x.value), " ",
          h("span", { class: "small muted" }, x.unit || ""))))) : null,
      !outs.length && !reads.length ? h("p", { class: "muted" }, t("groups.empty")) : null);
  }
  draw();
  state.onLive = (type, data) => {
    if (type === "status" || (type === "samples" && data.length)) state.later(draw);
  };
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 30000);
  return page;
}

// --- energy and cost -----------------------------------------------------------------------------

export function energyPage() {
  const page = h("div", { class: "page" }, header(t("energy.title"), "#/home"), loading());
  (async () => {
    const org = await homeOrg();
    let data;
    try { data = await api("GET", `/api/platform/my/energy?org=${encodeURIComponent(org)}&days=30`); }
    catch (err) { fill(page, header(t("energy.title"), "#/home"), h("p", {}, errorText(err))); return; }
    const max = Math.max(1, ...data.days.map((d) => d.kwh));
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${data.days.length * 10} 80`);
    svg.setAttribute("class", "bars");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", t("energy.chart"));
    data.days.forEach((d, i) => {
      const rect = document.createElementNS(ns, "rect");
      const hgt = Math.max(1, (d.kwh / max) * 72);
      rect.setAttribute("x", String(i * 10 + 2));
      rect.setAttribute("y", String(78 - hgt));
      rect.setAttribute("width", "6");
      rect.setAttribute("height", String(hgt));
      rect.setAttribute("rx", "2");
      const title = document.createElementNS(ns, "title");
      title.textContent = `${num(d.jalali)}: ${num(d.kwh)} kWh`;
      put(rect, title);
      put(svg, rect);
    });
    const m = data.month;
    fill(page, header(t("energy.title"), "#/home"),
      h("div", { class: "stats" },
        h("div", { class: "stat" }, h("span", { class: "tiny label" }, t("energy.month", { m: num(m.jalali) })),
          h("b", {}, num(m.kwh), h("span", { class: "unit" }, " kWh"))),
        h("div", { class: "stat" }, h("span", { class: "tiny label" }, t("energy.today")),
          h("b", {}, num(data.days[data.days.length - 1].kwh), h("span", { class: "unit" }, " kWh"))),
        h("div", { class: `stat ${m.cost ? "" : "dim"}` }, h("span", { class: "tiny label" }, t("energy.cost")),
          h("b", {}, m.cost !== null ? num(m.cost) : "—", h("span", { class: "unit" }, ` ${m.currency || ""}`)))),
      h("section", { class: "card stack" }, h("div", { class: "row between" }, h("h2", { class: "h2" }, t("energy.days")),
        h("span", { class: "tiny muted" }, t("energy.max", { n: num(Math.round(max * 10) / 10) }))), svg,
      h("div", { class: "row between tiny muted" }, h("span", {}, num(data.days[0].jalali)),
        h("span", {}, num(data.days[data.days.length - 1].jalali)))),
      m.peak_kwh ? h("p", { class: "small muted" }, t("energy.peak", { n: num(m.peak_kwh) })) : null,
      m.cost === null ? h("a", { class: "card tight row link-card", href: `#/home-settings/${org}` },
        h("span", { class: "ico" }, icon("bolt")), h("span", { class: "grow small" }, t("energy.set_tariff")), icon("next")) : null,
      h("section", { class: "stack" }, h("h2", { class: "h2" }, t("energy.by_device")),
        data.devices.length ? data.devices.map((d) => h("a", { class: "reading plain-link", href: `#/device/${d.device_id}` },
          h("span", {}, d.name), h("b", {}, num(d.kwh), h("span", { class: "small muted" }, " kWh"))))
          : h("p", { class: "small muted" }, t("energy.none"))));
  })();
  return page;
}

// --- a home's settings: name, place, tariff ---------------------------------------------------

export function homeSettings(state, orgId) {
  const page = h("div", { class: "page" }, header(t("homeset.title"), "#/me"), loading());
  (async () => {
    let s, homes;
    try {
      [s, homes] = await Promise.all([api("GET", `/api/platform/organizations/${orgId}/settings`),
        api("GET", "/api/account/organizations")]);
    } catch (err) { fill(page, header(t("homeset.title"), "#/me"), h("p", {}, errorText(err))); return; }
    const me = homes.find((m) => m.org_id === orgId);
    const owner = me?.role_id === "customer_owner";
    const nameIn = input({ value: me?.personal ? t("me.my_home") : me?.name || "", disabled: !owner || me?.personal });
    const lat = input({ type: "number", step: "any", dir: "ltr", value: s.latitude ?? "", disabled: !owner });
    const lon = input({ type: "number", step: "any", dir: "ltr", value: s.longitude ?? "", disabled: !owner });
    const radius = input({ type: "number", min: 30, max: 5000, dir: "ltr", value: s.radius_m ?? 150, disabled: !owner });
    const here = h("button", { class: "btn", disabled: !owner || !navigator.geolocation }, icon("home"), t("homeset.here"));
    here.addEventListener("click", busy(here, () => new Promise((resolve, reject) => {
      navigator.geolocation.getCurrentPosition((pos) => {
        lat.value = pos.coords.latitude.toFixed(5);
        lon.value = pos.coords.longitude.toFixed(5);
        resolve();
      }, () => reject(new Error(t("homeset.no_location"))), { enableHighAccuracy: true, timeout: 15000 });
    })));
    const tariff = s.tariff || {};
    const tiers = h("div", { class: "stack" });
    const addTier = (tier = {}) => {
      const upTo = input({ type: "number", min: 1, dir: "ltr", value: tier.up_to_kwh ?? "", placeholder: t("homeset.last_tier"), "aria-label": t("homeset.up_to") });
      const price = input({ type: "number", min: 0, dir: "ltr", value: tier.price ?? "", "aria-label": t("homeset.price") });
      const remove = h("button", { type: "button", class: "icon-btn", "aria-label": t("auto.delete"), onclick: () => row.remove() }, icon("trash"));
      const row = h("div", { class: "tier" }, labelled(t("homeset.up_to"), upTo), labelled(t("homeset.price"), price), remove);
      row.read = () => (price.value === "" ? null : { up_to_kwh: upTo.value === "" ? null : Number(upTo.value), price: Number(price.value) });
      put(tiers, row);
    };
    (tariff.tiers && tariff.tiers.length ? tariff.tiers : [{ up_to_kwh: 100, price: "" }, { up_to_kwh: null, price: "" }]).forEach(addTier);
    const more = h("button", { type: "button", class: "btn block", disabled: !owner, onclick: () => addTier() }, icon("plus"), t("homeset.add_tier"));
    const peakFrom = input({ type: "time", dir: "ltr", value: tariff.peak_from || "" });
    const peakTo = input({ type: "time", dir: "ltr", value: tariff.peak_to || "" });
    const factor = input({ type: "number", step: "0.1", min: 0.1, max: 10, dir: "ltr", value: tariff.peak_factor ?? 1 });
    const fixed = input({ type: "number", min: 0, dir: "ltr", value: tariff.fixed ?? 0 });
    const tax = input({ type: "number", min: 0, max: 100, dir: "ltr", value: tariff.tax_percent ?? 0 });
    const save = h("button", { class: "btn primary block", disabled: !owner }, t("common.save"));
    save.addEventListener("click", busy(save, async () => {
      if (!me?.personal && nameIn.value.trim() && nameIn.value.trim() !== me?.name) {
        await api("PATCH", `/api/account/homes/${orgId}`, { name: nameIn.value.trim() });
      }
      const list = [...tiers.children].map((r) => r.read()).filter(Boolean);
      await api("PUT", `/api/platform/organizations/${orgId}/settings`, {
        latitude: lat.value === "" ? null : Number(lat.value), longitude: lon.value === "" ? null : Number(lon.value),
        radius_m: Number(radius.value) || 150,
        tariff: { tiers: list, peak_from: peakFrom.value || null, peak_to: peakTo.value || null,
          peak_factor: Number(factor.value) || 1, fixed: Number(fixed.value) || 0, tax_percent: Number(tax.value) || 0,
          currency: lang === "fa" ? "ریال" : "IRR" } });
      toast(t("common.saved"), "ok");
    }));
    fill(page, header(t("homeset.title"), "#/me", me ? (me.personal ? t("me.my_home") : me.name) : ""),
      h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("homeset.name")), nameIn),
      h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("homeset.place")),
        h("p", { class: "small muted lh" }, t("homeset.place_help")),
        h("div", { class: "two" }, labelled(t("homeset.lat"), lat), labelled(t("homeset.lon"), lon)),
        labelled(t("homeset.radius"), radius), here),
      h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("homeset.tariff")),
        h("p", { class: "small muted lh" }, t("homeset.tariff_help")), tiers, more,
        h("div", { class: "two" }, labelled(t("homeset.peak_from"), peakFrom), labelled(t("homeset.peak_to"), peakTo)),
        h("div", { class: "two" }, labelled(t("homeset.factor"), factor), labelled(t("homeset.tax"), tax)),
        labelled(t("homeset.fixed"), fixed)),
      save);
  })();
  return page;
}

/** Presence: while the app is open, compare the phone's place with the home's and report a
 * change (arrive / leave) once; a PWA cannot do this in the background. */
export async function checkPresence() {
  if (!navigator.geolocation || saved.get("app.presence") === "off") return;
  try {
    const org = await homeOrg();
    if (!org) return;
    const s = await api("GET", `/api/platform/organizations/${org}/settings`);
    if (s.latitude === null || s.longitude === null) return;
    const pos = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(
      resolve, reject, { maximumAge: 120000, timeout: 10000 }));
    const R = 6371000;
    const toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(pos.coords.latitude - s.latitude);
    const dLon = toRad(pos.coords.longitude - s.longitude);
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(s.latitude)) * Math.cos(toRad(pos.coords.latitude)) * Math.sin(dLon / 2) ** 2;
    const inside = 2 * R * Math.asin(Math.sqrt(a)) <= (s.radius_m || 150);
    const key = `app.presence.${org}`;
    const before = saved.get(key);
    const now = inside ? "in" : "out";
    if (before && before !== now) {
      await api("POST", `/api/platform/organizations/${org}/presence`, { event: inside ? "arrive" : "leave" });
    }
    saved.set(key, now);
  } catch { /* no permission or no fix: nothing to report */ }
}

// --- tickets -------------------------------------------------------------------------------------

const TICKET_TONE = { open: "", answered: "accent", in_progress: "violet", approved: "ok", rejected: "warn", closed: "" };

export function ticketsSection(d) {
  const box = h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("tickets.title")));
  const list = h("div", { class: "stack" });
  api("GET", `/api/platform/my/tickets?device=${d.device_id}`).then((rows) => {
    fill(list, ...rows.map((r) => h("a", { class: "reading plain-link", href: `#/ticket/${r.ticket_id}` },
      h("span", { class: "stack tight" }, h("span", { class: "small" }, r.subject),
        h("span", { class: "tiny muted" }, `${t(`tickets.kind_${r.kind}`)} · ${when(r.updated_at)}`)),
      h("span", { class: `chip ${TICKET_TONE[r.status] || ""}` }, t(`tickets.status_${r.status}`)))));
  }).catch(() => {});
  const warranty = d.warranty_until && new Date(d.warranty_until) > new Date();
  const newBtn = h("button", { class: "btn block" }, icon("plus"), t("tickets.new"));
  newBtn.addEventListener("click", busy(newBtn, async () => {
    const kind = h("select", { class: "input", "aria-label": t("tickets.kind") },
      h("option", { value: "support" }, t("tickets.kind_support")),
      warranty ? h("option", { value: "warranty" }, t("tickets.kind_warranty")) : null);
    const subject = input({ placeholder: t("tickets.subject_hint"), "aria-label": t("tickets.subject") });
    const body = h("textarea", { class: "input area", rows: 5, placeholder: t("tickets.body_hint"), "aria-label": t("tickets.body") });
    const answer = await dialog(t("tickets.new"), h("div", { class: "stack" }, labelled(t("tickets.kind"), kind),
      labelled(t("tickets.subject"), subject), labelled(t("tickets.body"), body),
      h("p", { class: "tiny muted lh" }, t("tickets.privacy"))),
    [["cancel", t("common.cancel")], ["send", t("tickets.send"), "primary"]]);
    if (answer !== "send") return;
    const r = await api("POST", `/api/platform/my/devices/${d.device_id}/tickets`,
      { kind: kind.value, subject: subject.value.trim(), body: body.value.trim() });
    toast(t("tickets.sent"), "ok");
    go(`#/ticket/${r.ticket.ticket_id}`);
  }));
  put(box, warranty ? h("p", { class: "small muted" }, t("tickets.warranty_until", { date: jalali(d.warranty_until) })) : null,
    list, newBtn);
  return box;
}

export function ticketPage(state, ticketId) {
  const page = h("div", { class: "page" }, loading());
  async function draw() {
    let data;
    try { data = await api("GET", `/api/platform/my/tickets/${ticketId}`); }
    catch (err) { fill(page, header("", "#/home"), h("p", {}, errorText(err))); return; }
    const tk = data.ticket;
    const reply = h("textarea", { class: "input area", rows: 3, placeholder: t("tickets.reply_hint"), "aria-label": t("tickets.reply") });
    const send = h("button", { class: "btn primary block", disabled: tk.status === "closed" }, t("tickets.send"));
    send.addEventListener("click", busy(send, async () => {
      if (!reply.value.trim()) return;
      await api("POST", `/api/platform/my/tickets/${ticketId}/messages`, { body: reply.value.trim() });
      draw();
    }));
    fill(page, header(tk.subject, `#/device/${tk.device_id}/settings`, `${tk.product_name} · ${tk.identifier}`),
      h("div", { class: "row wrap" }, h("span", { class: "chip" }, t(`tickets.kind_${tk.kind}`)),
        h("span", { class: `chip ${TICKET_TONE[tk.status] || ""}` }, t(`tickets.status_${tk.status}`))),
      h("div", { class: "stack" }, data.messages.map((m) => h("div", { class: `bubble ${m.side}` },
        h("span", { class: "tiny muted" }, `${m.side === "maker" ? m.author_name : t("tickets.you")} · ${when(m.created_at)}`),
        h("span", { class: "small lh pre" }, m.body)))),
      tk.status === "closed" ? h("p", { class: "small muted" }, t("tickets.closed")) : h("div", { class: "stack" }, reply, send));
  }
  draw();
  state.timer = setInterval(() => { if (!document.hidden) draw(); }, 30000);
  return page;
}

// --- the SMS fallback ----------------------------------------------------------------------------

export function smsSection(d, outputs, owner) {
  const box = h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("sms.title")),
    h("p", { class: "small muted lh" }, t("sms.help")));
  (async () => {
    const s = await smsSettings(d.device_id);
    if (!s) { box.remove(); return; }
    const phone = input({ type: "tel", dir: "ltr", value: s.phone || "", placeholder: "+98912…", disabled: !owner });
    const save = h("button", { class: "btn", disabled: !owner }, s.enabled ? t("common.save") : t("sms.enable"));
    const send = (body) => async () => {
      const r = await api("PUT", `/api/platform/my/devices/${d.device_id}/sms`, { phone: phone.value.trim() || null, ...body });
      toast(r.sent_to_device ? t("sms.saved_sent") : t("sms.saved_later"), "ok");
      box.replaceWith(smsSection(d, outputs, owner));
    };
    save.addEventListener("click", busy(save, send({})));
    const renew = s.enabled && owner ? h("button", { class: "btn" }, t("sms.new_key")) : null;
    renew?.addEventListener("click", busy(renew, async () => {
      const ok = await dialog(t("sms.new_key"), h("p", { class: "small lh" }, t("sms.new_key_help")),
        [[null, t("common.cancel")], [true, t("sms.new_key"), "primary"]]);
      if (ok) await send({ new_key: true })();
    }));
    const off = s.enabled && owner ? h("button", { class: "btn danger" }, t("sms.disable")) : null;
    off?.addEventListener("click", busy(off, async () => {
      await api("DELETE", `/api/platform/my/devices/${d.device_id}/sms`);
      forget(d.device_id);
      box.replaceWith(smsSection(d, outputs, owner));
    }));
    const parts = [labelled(t("sms.phone"), phone), h("div", { class: "row wrap" }, save, renew, off)];
    if (s.enabled) parts.push(h("p", { class: "tiny muted lh" }, t("sms.key_kept")), composer(d, outputs, s));
    put(box, ...parts);
  })();
  return box;
}

/** The device's SMS settings (number, key), or the copy kept on this phone while offline. */
export async function smsSettings(id) {
  try {
    const s = await api("GET", `/api/platform/my/devices/${id}/sms`);
    remember(id, s);
    return s;
  } catch { return recall(id); }
}

// The device's SMS settings are kept on this phone, so a message can be signed with no internet.
const SMS_KEY = (id) => `hub.sms.${id}`;
function remember(id, s) {
  try { if (s.enabled) localStorage.setItem(SMS_KEY(id), JSON.stringify(s)); else localStorage.removeItem(SMS_KEY(id)); }
  catch { /* private mode: signing still works while online */ }
}
function recall(id) {
  try { return JSON.parse(localStorage.getItem(SMS_KEY(id)) || "null"); } catch { return null; }
}
function forget(id) { try { localStorage.removeItem(SMS_KEY(id)); } catch { /* nothing kept */ } }

/** A counter above every earlier one from this phone: the time in seconds. */
function nextCounter(id) {
  const k = `${SMS_KEY(id)}.n`;
  let last = 0;
  try { last = Number(localStorage.getItem(k)) || 0; } catch { /* none */ }
  const n = Math.max(Math.floor(Date.now() / 1000), last + 1);
  try { localStorage.setItem(k, String(n)); } catch { /* none */ }
  return n;
}

function b64url(text) {
  const b = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}

/** HMAC-SHA256(key, "<device_id> <counter> <items...>"), first 8 bytes in hex. */
export async function smsSignature(key, deviceId, counter, items) {
  const k = await crypto.subtle.importKey("raw", b64url(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", k,
    new TextEncoder().encode([deviceId, String(counter), ...items].join(" "))));
  return [...mac.slice(0, 8)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** Pick output states; the app writes and signs the SMS (`HUB <counter> relay_1=1 relay_2=0 <sig>`). */
export function composer(d, outputs, s) {
  const states = new Map();
  const text = h("div", { class: "code mono ltr sms-text" });
  const link = h("a", { class: "btn primary", href: "#" }, icon("share"), t("sms.send"));
  const copy = h("button", { class: "btn" }, t("sms.copy"));
  let seq = 0;
  const update = async () => {
    const mine = ++seq;
    const items = [...states.entries()].map(([k, v]) => `${k}=${v ? 1 : 0}`);
    let msg = "";
    if (items.length) {
      const n = nextCounter(d.device_id);
      msg = `HUB ${n} ${items.join(" ")} ${await smsSignature(s.key, d.device_id, n, items)}`;
    }
    if (mine !== seq) return;                       // a newer choice is being signed
    text.textContent = msg || t("sms.pick");
    text.classList.remove("flash"); void text.offsetWidth; if (msg) text.classList.add("flash");
    link.setAttribute("href", msg && s.phone ? `sms:${s.phone}?body=${encodeURIComponent(msg)}` : "#");
    link.classList.toggle("disabled", !msg || !s.phone);
    copy.disabled = !msg;
    copy.dataset.msg = msg;
  };
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(copy.dataset.msg); toast(t("sms.copied"), "ok"); }
    catch { toast(t("sms.copy_failed"), "err"); }
  });
  const selects = [];
  const rows = outputs.map((x) => {
    const sel = h("select", { class: "input sm-input", "aria-label": name(x.display_name), onchange: () => {
      if (sel.value === "") states.delete(x.key); else states.set(x.key, sel.value === "1");
      update();
    } }, h("option", { value: "" }, t("sms.no_change")), h("option", { value: "1" }, t("device.on")),
    h("option", { value: "0" }, t("device.off")));
    selects.push([x, sel]);
    return h("div", { class: "reading" }, h("span", { class: "row" }, icon(x.icon || "power"), name(x.display_name)), sel);
  });
  const every = (value) => () => {
    for (const [x, sel] of selects) {
      sel.value = value;
      if (value === "") states.delete(x.key); else states.set(x.key, value === "1");
    }
    update();
  };
  const quick = h("div", { class: "row wrap" },
    h("button", { type: "button", class: "chip accent", onclick: every("1") }, t("sms.all_on")),
    h("button", { type: "button", class: "chip", onclick: every("0") }, t("sms.all_off")),
    h("button", { type: "button", class: "chip", onclick: every("") }, t("sms.clear")));
  update();
  return h("div", { class: "stack" }, h("span", { class: "label" }, t("sms.compose")), quick, ...rows, text,
    h("div", { class: "two" }, link, copy), h("p", { class: "tiny muted lh" }, t("sms.security")));
}

// --- timers --------------------------------------------------------------------------------------

export function timersSection(d, outputs, redraw) {
  const box = h("section", { class: "card stack" }, h("div", { class: "row between" },
    h("h2", { class: "h2" }, t("timer.title")), h("span", { class: "row tiny muted" }, icon("clock"))));
  const list = h("div", { class: "stack" });
  const load = () => api("GET", `/api/platform/my/devices/${d.device_id}/timers`).then((rows) => {
    fill(list, ...(rows.length ? rows.map((r) => {
      const tag = outputs.find((x) => x.key === r.tag);
      const cancel = h("button", { class: "icon-btn", "aria-label": t("timer.cancel") }, icon("x"));
      cancel.addEventListener("click", busy(cancel, async () => {
        await api("DELETE", `/api/platform/my/devices/${d.device_id}/timers/${r.automation_id}`);
        load();
      }));
      return h("div", { class: "reading" }, h("span", { class: "stack tight" },
        h("span", { class: "small" }, t(r.value ? "timer.will_on" : "timer.will_off", { name: tag ? name(tag.display_name) : r.tag })),
        h("span", { class: "tiny muted" }, t("timer.at", { at: jalali(r.at, true) }))), cancel);
    }) : [h("p", { class: "small muted" }, t("timer.none"))]));
  }).catch(() => box.remove());
  load();
  const add = h("button", { class: "btn block" }, icon("plus"), t("timer.new"));
  add.addEventListener("click", busy(add, async () => {
    const tag = h("select", { class: "input", "aria-label": t("auto.output") },
      outputs.map((x) => h("option", { value: x.key }, name(x.display_name))));
    const state = h("select", { class: "input", "aria-label": t("auto.state") },
      h("option", { value: "false" }, t("device.turn_off")), h("option", { value: "true" }, t("device.turn_on")));
    const minutes = input({ type: "number", min: 1, max: 1440, value: 30, dir: "ltr", "aria-label": t("timer.minutes") });
    const quick = h("div", { class: "row wrap" }, [15, 30, 60, 120].map((m) => h("button", { type: "button", class: "tab",
      onclick: () => { minutes.value = m; } }, t("timer.min_n", { n: num(m) }))));
    const answer = await dialog(t("timer.new"), h("div", { class: "stack" }, labelled(t("auto.output"), tag),
      labelled(t("auto.state"), state), labelled(t("timer.minutes"), minutes), quick),
    [["cancel", t("common.cancel")], ["ok", t("timer.start"), "primary"]]);
    if (answer !== "ok") return;
    await api("POST", `/api/platform/my/devices/${d.device_id}/timers`,
      { tag: tag.value, value: state.value === "true", minutes: Number(minutes.value) || 30 });
    toast(t("timer.started", { n: num(Number(minutes.value) || 30) }), "ok");
    load();
    redraw?.();
  }));
  put(box, list, add);
  return box;
}

// --- commands with several parameters -----------------------------------------------------------

export function commandsSection(view, redraw) {
  if (!view.commands?.length) return h("span");
  const tags = new Map(view.tags.map((x) => [x.key, x]));
  return h("section", { class: "stack" }, h("h2", { class: "h2" }, t("cmd.title")),
    h("div", { class: "grid" }, view.commands.map((c) => {
      const btn = h("button", { class: "scene-card tone-accent" }, icon(c.icon || "sliders", "i lg"),
        h("span", { class: "stack tight" }, h("span", { class: "name" }, name(c.display_name)),
          h("span", { class: "tiny sub" }, t("cmd.params", { n: num(c.params.length) }))));
      btn.addEventListener("click", busy(btn, async () => {
        const asks = c.params.filter((p) => p.value === null || p.value === undefined);
        const values = {};
        if (asks.length) {
          const inputs = asks.map((p) => {
            const tag = tags.get(p.tag);
            const el = tag?.data_type === "bool"
              ? h("select", { class: "input" }, h("option", { value: "true" }, t("device.on")), h("option", { value: "false" }, t("device.off")))
              : input({ type: "number", step: "any", dir: "ltr", min: tag?.min ?? undefined, max: tag?.max ?? undefined, value: tag?.value ?? "" });
            return [p.tag, el, tag];
          });
          const answer = await dialog(name(c.display_name), h("div", { class: "stack" }, inputs.map(([key, el, tag]) =>
            labelled(`${tag ? name(tag.display_name) : key}${tag?.unit ? ` (${tag.unit})` : ""}`, el))),
          [["cancel", t("common.cancel")], ["ok", t("cmd.send"), "primary"]]);
          if (answer !== "ok") return;
          for (const [key, el, tag] of inputs) values[key] = tag?.data_type === "bool" ? el.value === "true" : Number(el.value);
        }
        const r = await api("POST", `/api/platform/my/devices/${view.device.device_id}/actions/${c.key}`, { values });
        toast(r.ok ? t("device.done") : t("cmd.partial"), r.ok ? "ok" : "err");
        redraw();
      }));
      return btn;
    })));
}

// --- shares --------------------------------------------------------------------------------------

export function sharesList(d) {
  const list = h("div", { class: "stack" });
  const draw = () => api("GET", `/api/platform/devices/${d.device_id}/shares`).then((r) => {
    const rows = [...(r.shares || []), ...(r.invitations || []).map((i) => ({ ...i, pending: true }))];
    fill(list, ...(rows.length ? rows.map((s) => {
      const revoke = h("button", { class: "icon-btn", "aria-label": t("share.revoke") }, icon("x"));
      revoke.addEventListener("click", busy(revoke, async () => {
        if (!confirm(t("share.revoke_confirm"))) return;
        if (s.pending) await api("DELETE", `/api/platform/organizations/${s.org_id}/invitations/${s.invitation_id}`);
        else await api("DELETE", `/api/platform/devices/${d.device_id}/shares/${s.access_id}`);
        draw();
      }));
      return h("div", { class: "reading" }, h("span", { class: "stack tight" },
        h("span", { class: "small" }, s.org_name || s.email || s.name || "—"),
        h("span", { class: "tiny muted" }, `${t(`access.${s.level}`)}${s.pending ? ` · ${t("share.pending")}` : ""}`)), revoke);
    }) : [h("p", { class: "small muted" }, t("share.none"))]));
  }).catch(() => fill(list, ));
  draw();
  return h("div", { class: "stack" }, h("span", { class: "label" }, t("share.current")), list);
}

// --- deleting the account ------------------------------------------------------------------------

export async function deleteAccount() {
  const password = input({ type: "password", autocomplete: "current-password", "aria-label": t("auth.password") });
  const typed = input({ dir: "ltr", placeholder: "DELETE", "aria-label": t("account.type_delete") });
  const answer = await dialog(t("account.delete"), h("div", { class: "stack" },
    h("p", { class: "small lh" }, t("account.delete_help")), labelled(t("auth.password"), password),
    labelled(t("account.type_delete"), typed)), [["cancel", t("common.cancel")], ["delete", t("account.delete"), "danger"]]);
  if (answer !== "delete") return false;
  if (typed.value.trim() !== "DELETE") { toast(t("account.type_delete"), "err"); return false; }
  await api("POST", "/api/account/delete", { password: password.value, confirm: "DELETE" });
  return true;
}

export async function newHome() {
  const nameIn = input({ placeholder: t("homes.name_hint"), "aria-label": t("homes.name") });
  const answer = await dialog(t("homes.new"), h("div", { class: "stack" }, labelled(t("homes.name"), nameIn),
    h("p", { class: "tiny muted lh" }, t("homes.help"))), [["cancel", t("common.cancel")], ["ok", t("homes.create"), "primary"]]);
  if (answer !== "ok" || !nameIn.value.trim()) return null;
  const r = await api("POST", "/api/account/homes", { name: nameIn.value.trim() });
  home.set(r.org_id);
  return r;
}
