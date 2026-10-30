// Air conditioners and thermostats: the tags a product marks with a climate role (power, mode,
// set point, fan speed, room temperature) become one panel — a large set point with − / +,
// the modes and fan speeds as chips, the room temperature — on the device page, and as a sheet
// wherever the AC's power output is touched (groups, sub-groups, shortcuts).

import { api, dialog, h, icon, name, num, t } from "./core.js";
import { command } from "./more.js";

const ROLES = { climate_power: "power", climate_mode: "mode", climate_setpoint: "setpoint",
  climate_fan: "fan", climate_temp: "temp" };

/** The device's climate sets: tags with a climate role, one set per ``ui.group``. */
export function climateSets(view) {
  const sets = new Map();
  for (const x of view.tags) {
    const role = ROLES[x.ui?.role];
    if (!role) continue;
    const key = x.ui.group || "";
    if (!sets.has(key)) sets.set(key, { group: x.ui.group || null });
    sets.get(key)[role] = x;
  }
  return [...sets.values()].filter((s) => s.power || s.setpoint || s.mode);
}

export const climateKeys = (sets) => new Set(sets.flatMap((s) =>
  ["power", "mode", "setpoint", "fan", "temp"].map((r) => s[r]?.key).filter(Boolean)));

// a mode's icon from its label (the maker writes the labels: "سرمایش", "Cool", ...)
function modeIcon(label) {
  const l = String(label).toLowerCase();
  if (/cool|سرما|سرد/.test(l)) return "snow";
  if (/heat|گرما|گرم/.test(l)) return "flame";
  if (/dry|خشک/.test(l)) return "water";
  if (/fan|فن|باد/.test(l)) return "fan";
  if (/auto|خودکار/.test(l)) return "sliders";
  return "thermo";
}

function tone(set) {
  const label = set.mode?.states?.[set.mode.value] || "";
  const ic = modeIcon(label);
  return ic === "flame" ? "heat" : ic === "snow" ? "cool" : "neutral";
}

/** The climate panel of one set; ``redraw`` after a change is confirmed. */
export function climateCard(deviceId, set, redraw, { online = true, canWrite = true, sheet = false } = {}) {
  const on = set.power ? set.power.value === true : true;
  // a control only for what this module can really do: its tag is there and controllable
  const can = (tag) => !!tag && online && canWrite && tag.writable !== false;
  const title = set.group || (set.power ? name(set.power.display_name) : t("climate.title"));
  const card = h("section", { class: `card stack climate ${on ? "on" : "off"} ${tone(set)} ${sheet ? "in-sheet" : ""}` });

  // the set point: shown at once, sent when the touches stop
  let target = typeof set.setpoint?.value === "number" ? set.setpoint.value : null;
  const step = set.setpoint && set.setpoint.data_type?.startsWith("float") ? 0.5 : 1;
  const big = h("b", { class: "climate-target ltr" }, target === null ? "—" : num(target));
  let timer = null;
  const nudge = (dir) => () => {
    if (!set.setpoint || target === null) return;
    const lo = set.setpoint.min ?? 16;
    const hi = set.setpoint.max ?? 30;
    target = Math.min(hi, Math.max(lo, Math.round((target + dir * step) * 2) / 2));
    big.textContent = num(target);
    big.classList.remove("flash"); void big.offsetWidth; big.classList.add("flash");
    try { navigator.vibrate?.(8); } catch { /* none */ }
    clearTimeout(timer);
    timer = setTimeout(async () => {
      await command(deviceId, set.setpoint.key, target, true);
      redraw();
    }, 700);
  };
  const minus = h("button", { class: "climate-step", disabled: !can(set.setpoint) || !on,
    "aria-label": t("climate.colder"), onclick: nudge(-1) }, h("span", { class: "sign" }, "−"));
  const plus = h("button", { class: "climate-step", disabled: !can(set.setpoint) || !on,
    "aria-label": t("climate.warmer"), onclick: nudge(1) }, h("span", { class: "sign" }, "+"));

  const power = set.power ? h("button", { class: `climate-power ${on ? "on" : ""}`, disabled: !can(set.power),
    "aria-pressed": on, "aria-label": on ? t("device.turn_off") : t("device.turn_on"), onclick: async () => {
      try { navigator.vibrate?.(12); } catch { /* none */ }
      power.classList.toggle("on", !on);
      card.classList.toggle("on", !on);
      card.classList.toggle("off", on);
      await command(deviceId, set.power.key, !on, true);
      redraw();
    } }, icon("power")) : null;

  const chips = (tag, iconOf) => (tag?.states ? h("div", { class: "climate-chips", role: "radiogroup",
    "aria-label": name(tag.display_name) }, Object.entries(tag.states).map(([v, label]) => {
    const chosen = String(tag.value) === String(v);
    const b = h("button", { class: `climate-chip ${chosen ? "on" : ""}`, role: "radio", "aria-checked": chosen,
      disabled: !can(tag) || !on, onclick: async () => {
        if (chosen) return;
        try { navigator.vibrate?.(8); } catch { /* none */ }
        for (const other of b.parentNode.children) other.classList.toggle("on", other === b);
        await command(deviceId, tag.key, Number(v), true);
        redraw();
      } }, iconOf ? icon(iconOf(label)) : null, h("span", {}, label));
    return b;
  })) : null);

  const room = set.temp && typeof set.temp.value === "number"
    ? h("span", { class: "chip" }, icon("thermo"), t("climate.room", { n: num(set.temp.value) })) : null;
  const unit = set.setpoint?.unit || "°C";
  card.append(...[
    h("div", { class: "row between" },
      h("div", { class: "stack tight" }, h("h2", { class: "h2" }, title),
        h("span", { class: "tiny muted" }, on ? (set.mode?.states?.[set.mode.value] || t("device.on")) : t("device.off"))),
      power),
    set.setpoint ? h("div", { class: "climate-dial" }, plus,
      h("div", { class: "climate-read" }, h("span", { class: "tiny muted" }, t("climate.target")),
        h("div", { class: "row center-row" }, big, h("span", { class: "climate-unit" }, unit))),
      minus) : null,
    room ? h("div", { class: "row wrap center-row" }, room) : null,
    !online ? h("p", { class: "tiny warn-text center" }, t("climate.offline")) : null,
    set.power && !set.setpoint && !set.mode && !set.fan
      ? h("p", { class: "tiny muted lh" }, t("climate.power_only")) : null,
    set.mode ? h("span", { class: "label" }, name(set.mode.display_name)) : null,
    chips(set.mode, modeIcon),
    set.fan ? h("span", { class: "label" }, name(set.fan.display_name)) : null,
    chips(set.fan, () => "fan")].filter(Boolean));   // DOM append would print "null"
  return card;
}

/** The panel as a sheet, for an AC's power output touched outside the device page. */
export async function climateSheet(deviceId, powerKey) {
  let view;
  try { view = await api("GET", `/api/platform/my/devices/${deviceId}/view`); } catch { return false; }
  const set = climateSets(view).find((s) => s.power?.key === powerKey) || climateSets(view)[0];
  if (!set) return false;
  const box = h("div", { class: "stack" });
  const draw = async () => {
    try { view = await api("GET", `/api/platform/my/devices/${deviceId}/view`); } catch { return; }
    const fresh = climateSets(view).find((s) => s.power?.key === set.power?.key) || set;
    box.replaceChildren(climateCard(deviceId, fresh, draw, { online: view.device.state === "online",
      canWrite: view.device.access !== "viewer", sheet: true }));
  };
  box.append(climateCard(deviceId, set, draw, { online: view.device.state === "online",
    canWrite: view.device.access !== "viewer", sheet: true }));
  await dialog(`${view.device.name}`, box);
  return true;
}
