// Outputs as the home sees them: its own names and icons for them ("relay 1" -> "chandelier"),
// favourites on the home screen, named parts inside a group (sub-groups), and the quick SMS
// sheet for when the internet is down.

import { api, changed, dialog, errorText, h, icon, name, t, toast } from "./core.js";
import { command, composer, connection, homeOrg, smsSettings } from "./more.js";
import { climateSheet } from "./climate.js";

export const OUTPUT_ICONS = ["power", "bulb", "chandelier", "lamp", "fan", "snow", "flame", "plug",
  "tv", "curtain", "door", "lock", "water", "pump", "leaf", "bell"];
// inputs a module can carry (a learnt 433 MHz remote, wireless PIR / magnet / smoke sensors,
// an IR receiver, a missed call that runs a command)
export const SENSOR_ICONS = ["motion", "magnet", "smoke", "remote", "ir", "call", "siren", "door", "water",
  "thermo", "sensor", "bolt"];
const ALERTING = new Set(["motion", "magnet", "smoke", "siren", "water"]);

/** An on/off input as a live state: its icon, its state in words, glowing while active. */
export function sensorRow(deviceId, x) {
  const ic = x.icon || x.ui?.icon || "sensor";
  const active = x.value === true;
  const words = x.states ? x.states[active ? 1 : 0] : null;
  const key = `sensor.${ic}_${active ? "on" : "off"}`;
  const named = t(key) !== key ? t(key) : t(active ? "sensor.active" : "sensor.idle");
  const state = x.value === null || x.value === undefined ? "—" : words || named;
  const row = h("div", { class: `reading sensor ${active ? "active" : ""} ${active && ALERTING.has(ic) ? "alert" : ""}` },
    h("span", { class: "row" }, h("span", { class: "sensor-ico" }, icon(ic)), h("span", {}, name(x.display_name))),
    h("span", { class: "sensor-state" }, state));
  return changed(row, `sensor:${deviceId}:${x.key}`, x.value);
}

/** A short tick under the finger where the phone can (Android); silently nothing elsewhere. */
export function buzz() {
  try { navigator.vibrate?.(12); } catch { /* not supported */ }
}

export const outIcon = (x) => x.icon || "power";
const isOutput = (x) => x.writable && x.data_type === "bool";

/** One output button: it answers the touch at once and the device confirms with the redraw.
 * While ``editing``, a touch opens the naming sheet instead. */
export function outButton(deviceId, x, redraw, { sub = null, disabled = false, editing = false,
  offline = false } = {}) {
  const on = x.value === true;
  if (!editing && x.mode === "dual") return dualButton(deviceId, x, redraw, { sub, disabled, offline });
  if (!editing && x.mode === "momentary") return pushButton(deviceId, x, redraw, { sub, disabled });
  const btn = h("button", { class: `out ${on ? "on" : ""} ${editing ? "editing" : ""}`, disabled,
    "aria-pressed": on,
    "aria-label": editing ? `${t("out.rename")}: ${name(x.display_name)}`
      : `${on ? t("device.turn_off") : t("device.turn_on")} ${name(x.display_name)}`,
    onclick: async () => {
      if (editing) {
        if (await labelDialog(deviceId, x)) redraw();
        return;
      }
      if (x.ui?.role === "climate_power") {           // an AC: its whole panel, not a toggle
        if (await climateSheet(deviceId, x.key)) { redraw(); return; }
      }
      buzz();
      if (await sendVia(deviceId, x, !on, offline, btn)) redraw();
    } },
  x.favorite ? h("span", { class: "fav-mark", "aria-hidden": "true" }, icon("star")) : null,
  editing ? h("span", { class: "edit-mark", "aria-hidden": "true" }, icon("edit")) : null,
  icon(outIcon(x)), h("span", { class: "out-name" }, name(x.display_name)),
  sub ? h("span", { class: "tiny dev-label" }, sub) : null);
  return changed(btn, `out:${deviceId}:${x.key}`, on);
}

const marks = (x) => [x.favorite ? h("span", { class: "fav-mark", "aria-hidden": "true" }, icon("star")) : null];

/** Separate on and off keys (YanSon's "two keys"): no guessing what one touch will do. */
function dualButton(deviceId, x, redraw, { sub, disabled, offline }) {
  const on = x.value === true;
  const send = (value) => async (ev) => {
    ev.stopPropagation();
    if (x.value === value && !offline) return;
    buzz();
    if (await sendVia(deviceId, x, value, offline, box)) redraw();
  };
  const box = h("div", { class: `out dual ${on ? "on" : ""}`, role: "group", "aria-label": name(x.display_name) },
    ...marks(x), h("span", { class: "dual-head" }, icon(outIcon(x)), h("span", { class: "out-name" }, name(x.display_name))),
    sub ? h("span", { class: "tiny dev-label" }, sub) : null,
    h("span", { class: "dual-keys" },
      h("button", { class: `dual-key ${on ? "lit" : ""}`, disabled, "aria-pressed": on, onclick: send(true) }, t("device.on")),
      h("button", { class: `dual-key ${on ? "" : "lit"}`, disabled, "aria-pressed": !on, onclick: send(false) }, t("device.off"))));
  return changed(box, `out:${deviceId}:${x.key}`, on);
}

/** A momentary key (door opener, bell, gate): on for a moment, the hub turns it off. */
function pushButton(deviceId, x, redraw, { sub, disabled }) {
  const seconds = x.pulse_s || 1;
  const btn = h("button", { class: `out momentary ${x.value === true ? "on" : ""}`, disabled,
    "aria-label": `${t("out.push")}: ${name(x.display_name)}`, onclick: async () => {
      buzz();
      btn.classList.add("pushing", "on");
      try {
        const r = await api("POST", `/api/platform/my/devices/${deviceId}/pulse`, { tag: x.key, seconds });
        if (r.result !== "applied") toast(t(`write.${r.result}`) !== `write.${r.result}` ? t(`write.${r.result}`) : r.message, "err");
      } catch (err) { toast(errorText(err), "err"); }
      setTimeout(() => { btn.classList.remove("pushing", "on"); redraw(); }, seconds * 1000 + 400);
    } },
  ...marks(x), h("span", { class: "push-ring", "aria-hidden": "true" }), icon(outIcon(x)),
  h("span", { class: "out-name" }, name(x.display_name)), sub ? h("span", { class: "tiny dev-label" }, sub) : null);
  return btn;
}

/** The naming sheet: the home's name for the output, its icon, a favourite on the home screen.
 * Resolves true when something was saved. */
export async function labelDialog(deviceId, x) {
  const maker = name(x.default_name || x.display_name);
  const nameIn = h("input", { class: "input", value: x.default_name ? name(x.display_name) : "",
    placeholder: t("out.name_hint"), maxlength: 40, "aria-label": t("out.name") });
  let chosen = x.icon || null;
  const picks = OUTPUT_ICONS.map((ic) => {
    const b = h("button", { type: "button", class: `icon-pick ${(chosen || "power") === ic ? "on" : ""}`,
      "aria-label": ic, "aria-pressed": (chosen || "power") === ic, onclick: () => {
        chosen = ic === "power" ? null : ic;
        for (const other of picks) {
          const mine = other === b;
          other.classList.toggle("on", mine);
          other.setAttribute("aria-pressed", String(mine));
        }
        buzz();
      } }, icon(ic));
    return b;
  });
  let favorite = !!x.favorite;
  const star = h("button", { type: "button", class: `switch ${favorite ? "on" : ""}`, "aria-pressed": favorite,
    "aria-label": t("out.favorite"), onclick: () => {
      favorite = !favorite;
      star.classList.toggle("on", favorite);
      star.setAttribute("aria-pressed", String(favorite));
      buzz();
    } }, h("span", { class: "track" }, h("span", { class: "knob" })));
  // how it is switched: one key, two keys (on / off), or a momentary push
  let mode = x.mode || "toggle";
  const pulse = h("input", { class: "input", type: "number", min: 0.2, max: 600, step: 0.1, dir: "ltr",
    value: x.pulse_s || 1, "aria-label": t("out.pulse") });
  const pulseBox = h("div", { class: `field ${mode === "momentary" ? "" : "hidden"}` },
    h("span", { class: "label" }, t("out.pulse")), pulse, h("span", { class: "tiny muted" }, t("out.pulse_help")));
  const modes = [["toggle", t("out.mode_toggle"), "power"], ["dual", t("out.mode_dual"), "sliders"],
    ["momentary", t("out.mode_momentary"), "bell"]];
  const seg = h("div", { class: "choice", role: "radiogroup", "aria-label": t("out.mode") }, modes.map(([m, label, ic]) => {
    const b = h("button", { type: "button", class: `choice-item ${m === mode ? "on" : ""}`, role: "radio", "aria-checked": m === mode,
      onclick: () => {
        mode = m;
        for (const o of seg.children) { o.classList.toggle("on", o === b); o.setAttribute("aria-checked", String(o === b)); }
        pulseBox.classList.toggle("hidden", m !== "momentary");
        buzz();
      } }, icon(ic), h("span", {}, label));
    return b;
  }));
  const body = h("div", { class: "stack" },
    h("div", { class: "field" }, h("span", { class: "label" }, t("out.name")), nameIn,
      h("span", { class: "tiny muted" }, t("out.default", { name: maker }))),
    h("span", { class: "label" }, t("out.icon")), h("div", { class: "icon-grid" }, picks),
    h("span", { class: "label" }, t("out.mode")), seg, pulseBox,
    h("div", { class: "reading" }, h("span", { class: "row" }, icon("star"), t("out.favorite")), star));
  const buttons = [["cancel", t("common.cancel")], ["save", t("common.save"), "primary"]];
  if (x.default_name || x.icon || x.favorite || (x.mode && x.mode !== "toggle")) buttons.unshift(["reset", t("out.reset")]);
  const answer = await dialog(t("out.rename"), body, buttons);
  if (answer !== "save" && answer !== "reset") return false;
  const payload = answer === "reset" ? {} : { name: nameIn.value.trim() || null, icon: chosen, favorite, mode,
    pulse_s: mode === "momentary" ? Math.min(600, Math.max(0.2, Number(pulse.value) || 1)) : null };
  try {
    await api("PUT", `/api/platform/my/devices/${deviceId}/labels/${x.key}`, payload);
    toast(t("out.saved"), "ok");
    return true;
  } catch (err) { toast(errorText(err), "err"); return false; }
}

/** The favourites row on the home screen: one touch each, wherever the output lives. */
export function favoritesRow(views, redraw) {
  const favs = views.filter(Boolean).flatMap((v) => v.tags.filter((x) => x.favorite && isOutput(x))
    .map((x) => ({ v, x })));
  if (!favs.length) return null;
  return h("section", { class: "stack tight" }, h("h2", { class: "h2" }, t("home.favorites")),
    h("div", { class: "fav-row" }, favs.map(({ v, x }) => {
      const on = x.value === true;
      const offline = v.device.state !== "online";
      const b = h("button", { class: `fav ${on ? "on" : ""}`, disabled: offline, "aria-pressed": on,
        "aria-label": `${name(x.display_name)} · ${v.device.name}`, onclick: async () => {
          if (x.ui?.role === "climate_power" && await climateSheet(v.device.device_id, x.key)) { redraw(); return; }
          buzz();
          b.classList.toggle("on", !on);
          b.classList.add("busy");
          await command(v.device.device_id, x.key, !on, true);
          redraw();
        } },
      h("span", { class: "ico" }, icon(outIcon(x))),
      h("span", { class: "stack tight" }, h("span", { class: "name" }, name(x.display_name)),
        h("span", { class: "tiny muted" }, offline ? t("device.offline") : on ? t("device.on") : t("device.off"))));
      return changed(b, `fav:${v.device.device_id}:${x.key}`, on);
    })));
}

// --- named parts inside a group ------------------------------------------------------------------

/** Add, change or remove one part of a group ("chandelier": these relays). Resolves true when
 * the group was saved. */
export async function sectionDialog(org, group, views, index = null) {
  const sec = index === null ? null : group.sections[index];
  const nameIn = h("input", { class: "input", value: sec?.name || "", maxlength: 40,
    placeholder: t("groups.section_hint"), "aria-label": t("groups.section_name") });
  let chosenIcon = sec?.icon || "bulb";
  const picks = OUTPUT_ICONS.map((ic) => {
    const b = h("button", { type: "button", class: `icon-pick ${chosenIcon === ic ? "on" : ""}`, "aria-label": ic,
      onclick: () => { chosenIcon = ic; for (const o of picks) o.classList.toggle("on", o === b); buzz(); } }, icon(ic));
    return b;
  });
  const ref = (d, k) => `${d}:${k}`;
  const chosen = new Set((sec?.outputs || []).map((o) => ref(o.device_id, o.key)));
  const multi = views.length > 1;
  const list = h("div", { class: "stack tight" }, views.flatMap((v) => v.tags.filter(isOutput).map((x) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(ref(v.device.device_id, x.key)), onchange: () => {
      if (box.checked) chosen.add(ref(v.device.device_id, x.key)); else chosen.delete(ref(v.device.device_id, x.key));
    } });
    return h("label", { class: "reading check-row" }, h("span", { class: "row" }, box, icon(outIcon(x)), name(x.display_name)),
      multi ? h("span", { class: "tiny muted" }, v.device.name) : null);
  })));
  const buttons = [["cancel", t("common.cancel")], ["save", t("common.save"), "primary"]];
  if (sec) buttons.unshift(["delete", t("auto.delete"), "danger"]);
  for (;;) {
    const answer = await dialog(sec ? t("groups.section_edit") : t("groups.add_section"), h("div", { class: "stack" },
      h("div", { class: "field" }, h("span", { class: "label" }, t("groups.section_name")), nameIn),
      h("span", { class: "label" }, t("out.icon")), h("div", { class: "icon-grid" }, picks),
      h("span", { class: "label" }, t("groups.section_outputs")), list), buttons);
    if (answer !== "save" && answer !== "delete") return false;
    const sections = [...(group.sections || [])];
    if (answer === "delete") sections.splice(index, 1);
    else {
      if (!chosen.size) { toast(t("groups.pick_output"), "err"); continue; }
      const part = { name: nameIn.value.trim() || t("groups.section"), icon: chosenIcon,
        outputs: [...chosen].map((r) => { const [device_id, key] = r.split(":"); return { device_id, key }; }) };
      if (sec) sections[index] = part; else sections.push(part);
    }
    try {
      await api("PUT", `/api/platform/organizations/${org}/groups/${group.group_id}`, {
        name: group.name, icon: group.icon, device_ids: group.device_ids, position: group.position || 0, sections });
      toast(t("groups.section_saved"), "ok");
      return true;
    } catch (err) { toast(errorText(err), "err"); }
  }
}

/** One part of a group as a card: its outputs and a switch for all of them at once. */
export function sectionCard(sec, outs, redraw, onEdit, editing = false) {
  const on = outs.filter(({ x }) => x.value === true).length;
  const allOn = outs.length > 0 && on === outs.length;
  const canUse = outs.some(({ v }) => v.device.state === "online");
  const master = h("button", { class: `switch ${on ? "on" : ""}`, disabled: !canUse, "aria-pressed": on > 0,
    "aria-label": `${sec.name}: ${on ? t("device.turn_off") : t("device.turn_on")}`, onclick: async () => {
      buzz();
      const value = !on;
      master.classList.toggle("on", value);
      master.disabled = true;
      await Promise.all(outs.filter(({ x }) => x.value !== value)
        .map(({ v, x }) => command(v.device.device_id, x.key, value, true)));
      redraw();
    } }, h("span", { class: "track" }, h("span", { class: "knob" })));
  const card = h("section", { class: `card stack section-card ${on ? "lit" : ""}` },
    h("div", { class: "row between" },
      h("button", { class: "section-head", onclick: onEdit, "aria-label": `${t("groups.section_edit")}: ${sec.name}` },
        h("span", { class: "ico" }, icon(sec.icon || "bulb")),
        h("span", { class: "stack tight" }, h("span", { class: "name" }, sec.name),
          h("span", { class: "tiny muted" }, allOn ? t("device.all_on_state")
            : t("device.on_count", { n: on, total: outs.length })))),
      master),
    outs.length ? h("div", { class: "outputs" }, outs.map(({ v, x }) => outButton(v.device.device_id, x, redraw,
      { offline: v.device.state !== "online", editing }))) : h("p", { class: "tiny muted" }, t("groups.section_empty")));
  return changed(card, `sec:${sec.name}`, on);
}

// --- the way to the device: internet, the home's local hub, SMS ---------------------------------

/** Send one output change the way this phone is set to (home settings > connection): over the
 * internet; if that fails or the device is offline, the home's local hub or a signed SMS.
 * Resolves true when the page should redraw. */
export async function sendVia(deviceId, x, value, offline, el) {
  const org = await homeOrg().catch(() => null);
  const way = connection(org);
  if (way.order === "sms" || (offline && way.order === "auto")) {
    await smsFor(deviceId, x, value);
    return false;
  }
  el.classList.toggle("on", value);
  el.classList.add("busy");
  const r = await command(deviceId, x.key, value, false, way.order === "auto");
  if (r === null && way.order === "auto") {             // no way through the internet
    el.classList.remove("busy");
    el.classList.toggle("on", !value);
    await fallback(deviceId, x, value, way);
    return false;
  }
  return true;
}

async function fallback(deviceId, x, value, way) {
  const buttons = [["cancel", t("common.cancel")]];
  if (way.local) buttons.push(["local", t("connect.open_local")]);
  buttons.push(["sms", t("sms.quick"), "primary"]);
  const answer = await dialog(t("connect.failed"), h("div", { class: "stack" },
    h("div", { class: "connect-steps" }, [["globe", t("connect.step_cloud"), "fail"],
      ["hub", t("connect.step_local"), way.local ? "" : "skip"], ["chat", t("connect.step_sms"), "next"]]
      .map(([ic, label, st], i) => h("span", { class: `connect-step ${st}` }, h("span", { class: "n" }, String(i + 1)),
        icon(ic), h("span", { class: "small" }, label)))),
    h("p", { class: "small lh" }, way.local ? t("connect.failed_local") : t("connect.failed_help"))), buttons);
  if (answer === "local") window.open(`${way.local}/app/#/device/${deviceId}`, "_blank", "noopener");
  if (answer === "sms") await smsFor(deviceId, x, value);
}

/** The SMS sheet with this one change chosen (the device's other outputs when known). */
export async function smsFor(deviceId, x, value) {
  let outputs = [x];
  let device = { device_id: deviceId };
  try {
    const view = await api("GET", `/api/platform/my/devices/${deviceId}/view`);
    device = view.device;
    outputs = view.tags.filter((t2) => t2.writable && t2.data_type === "bool");
  } catch { /* offline: the one output we know */ }
  await smsSheet(device, outputs, false, { [x.key]: value });
}

// --- the quick SMS sheet -------------------------------------------------------------------------

/** From the device page: the signed SMS for chosen outputs, to copy or send (no internet needed
 * once the key is on this phone). */
export async function smsSheet(d, outputs, canEdit, preset = {}) {
  const s = await smsSettings(d.device_id);
  if (!s || !s.enabled) {
    const answer = await dialog(t("sms.quick"), h("p", { class: "small lh" }, t("sms.not_enabled")),
      canEdit ? [["cancel", t("common.cancel")], ["go", t("sms.go_settings"), "primary"]] : []);
    if (answer === "go") location.hash = `#/device/${d.device_id}/settings`;
    return;
  }
  await dialog(t("sms.quick"), composer(d, outputs, s, preset));
}
