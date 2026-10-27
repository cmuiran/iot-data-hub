// Scenes and automations of the home (platform phase 6): list, run, switch on/off, build.

import { api, errorText, h, icon, name, num, t, toast } from "./core.js";

const OPS = ["gt", "lt", "eq"];

async function homeOrg() {
  const rows = await api("GET", "/api/account/organizations");
  const own = rows.find((m) => m.personal) || rows[0];
  return own ? own.org_id : null;
}

async function catalog() {          // the caller's devices with their tags (for the builder)
  const devices = await api("GET", "/api/platform/my/devices");
  const views = await Promise.all(devices.map((d) =>
    api("GET", `/api/platform/my/devices/${d.device_id}/view`).catch(() => null)));
  return views.filter(Boolean);
}

function tagName(views, deviceId, key) {
  const v = views.find((x) => x.device.device_id === deviceId);
  const tag = v && v.tags.find((x) => x.key === key);
  return tag ? `${name(tag.display_name)} (${v.device.name})` : key;
}

function valueText(v) {
  return typeof v === "boolean" ? (v ? t("device.on") : t("device.off")) : num(v);
}

export function describe(def, views) {
  const tr = def.trigger;
  let when;
  if (tr.type === "time") {
    const days = tr.weekdays ? tr.weekdays.map((d) => t(`week.${d}`)).join("، ") : t("auto.every_day");
    when = t("auto.when_time", { days, at: num(tr.at) });
  } else if (tr.type === "interval") {
    when = t("auto.when_interval", { n: num(tr.every_min) });
  } else if (tr.type === "once") {
    when = t("auto.when_once", { date: num(tr.date), at: num(tr.at) });
  } else if (tr.type === "value") {
    when = t("auto.when_value", { tag: tagName(views, tr.device_id, tr.tag),
      op: t(`op.${tr.op}`), value: valueText(tr.value) });
  } else {
    when = t("auto.when_offline");
  }
  const then = def.actions.map((a) => a.type === "write"
    ? `${tagName(views, a.device_id, a.tag)} ← ${valueText(a.value)}`
    : a.type === "scene" ? t("auto.run_scene") : a.type === "delay"
      ? t("auto.wait", { n: num(a.seconds) }) : t("auto.notify")).join("، ");
  return { when, then };
}

export function automationsPage() {
  const scenesBox = h("div", { class: "grid" });
  const list = h("div", { class: "stack" });
  const builderBox = h("div");
  let org = null;
  let views = [];
  let scenes = [];

  async function draw() {
    try {
      org = org || (await homeOrg());
      [views, scenes] = await Promise.all([catalog(),
        api("GET", `/api/platform/organizations/${org}/scenes`)]);
      const autos = await api("GET", `/api/platform/organizations/${org}/automations`);
      scenesBox.replaceChildren(...(scenes.length ? scenes.map(sceneCard)
        : [h("p", { class: "small muted" }, t("auto.no_scenes"))]));
      list.replaceChildren(...(autos.length ? autos.map(autoCard)
        : [h("p", { class: "small muted" }, t("auto.none"))]));
    } catch (err) {
      list.replaceChildren(h("p", {}, errorText(err)));
    }
  }

  function sceneCard(s) {
    const btn = h("button", { class: "dev", onclick: async () => {
      btn.disabled = true;
      try {
        const r = await api("POST", `/api/platform/organizations/${org}/scenes/${s.scene_id}/run`);
        const bad = r.detail.filter((d) => d.tag && d.result !== "applied").length;
        toast(bad ? t("auto.scene_partial", { n: num(bad) }) : t("auto.scene_done"),
          bad ? "err" : "ok");
      } catch (err) { toast(errorText(err), "err"); }
      btn.disabled = false;
    } }, h("span", { class: "ico" }, icon(s.icon === "moon" ? "moon" : "power", "i lg")),
    h("span", { class: "name" }, s.name),
    h("span", { class: "state" }, t("auto.actions", { n: num(s.actions.length) })));
    return btn;
  }

  function autoCard(a) {
    const { when, then } = describe(a.definition, views);
    const sw = h("button", { class: `switch ${a.enabled ? "on" : ""}`, "aria-pressed": a.enabled,
      "aria-label": a.enabled ? t("auto.disable") : t("auto.enable"), onclick: async () => {
        sw.disabled = true;
        try {
          await api("PUT", `/api/platform/organizations/${org}/automations/${a.automation_id}`,
            { name: a.name, enabled: !a.enabled, ...a.definition });
          draw();
        } catch (err) { toast(errorText(err), "err"); sw.disabled = false; }
      } }, h("span", { class: "track" }, h("span", { class: "knob" })));
    const remove = h("button", { class: "icon-btn", "aria-label": t("auto.delete"),
      onclick: async () => {
        if (!confirm(t("auto.delete_confirm"))) return;
        await api("DELETE", `/api/platform/organizations/${org}/automations/${a.automation_id}`)
          .catch((err) => toast(errorText(err), "err"));
        draw();
      } }, icon("x"));
    return h("div", { class: "card stack" },
      h("div", { class: "row between" }, h("span", { class: "h2" }, a.name),
        h("div", { class: "row" }, remove, sw)),
      h("div", { class: "row wrap small" },
        h("span", { class: "muted" }, t("auto.if")), h("span", { class: "chip accent" }, when),
        h("span", { class: "muted" }, t("auto.then")), h("span", { class: "chip ok" }, then)),
      a.next_runs.length ? h("span", { class: "tiny muted" }, t("auto.next", {
        at: new Intl.DateTimeFormat("fa-IR-u-ca-persian", { dateStyle: "medium",
          timeStyle: "short", timeZone: "Asia/Tehran" }).format(new Date(a.next_runs[0])) })) : null);
  }

  const add = h("button", { class: "btn primary block", onclick: () => {
    builderBox.replaceChildren(builder(org, views, () => { builderBox.replaceChildren(); draw(); }));
  } }, t("auto.new"));
  draw();
  return h("div", { class: "page" }, h("h1", { class: "h1" }, t("auto.title")),
    h("h2", { class: "h2" }, t("auto.scenes")), scenesBox,
    h("h2", { class: "h2" }, t("auto.automations")), list, add, builderBox);
}

function select(options, label) {
  return h("select", { class: "input", "aria-label": label },
    options.map(([value, text]) => h("option", { value }, text)));
}

function builder(org, views, done) {
  const nameIn = h("input", { class: "input", "aria-label": t("auto.name"),
    placeholder: t("auto.name") });
  const kind = select([["time", t("auto.kind_time")], ["value", t("auto.kind_value")]],
    t("auto.trigger"));
  const at = h("input", { class: "input ltr", type: "time", value: "08:00",
    "aria-label": t("auto.at") });
  const days = new Set([0, 1, 2, 3, 4, 5, 6]);
  const dayChips = h("div", { class: "row wrap" }, [0, 1, 2, 3, 4, 5, 6].map((d) => {
    const chip = h("button", { class: "tab on", type: "button", "aria-pressed": true,
      onclick: () => {
        if (days.has(d)) days.delete(d); else days.add(d);
        chip.classList.toggle("on", days.has(d));
        chip.setAttribute("aria-pressed", String(days.has(d)));
      } }, t(`week.${d}`));
    return chip;
  }));
  const readings = [];
  const outputs = [];
  for (const v of views) {
    for (const tag of v.tags) {
      const entry = [`${v.device.device_id}|${tag.key}`, `${name(tag.display_name)} — ${v.device.name}`];
      if (tag.writable && tag.data_type === "bool") outputs.push(entry);
      else if (tag.data_type !== "bool" && tag.data_type !== "string") readings.push(entry);
    }
  }
  const source = select(readings, t("auto.value_of"));
  const op = select(OPS.map((o) => [o, t(`op.${o}`)]), t("auto.op"));
  const threshold = h("input", { class: "input ltr", type: "number", step: "any", value: "0",
    "aria-label": t("auto.threshold") });
  const timeBox = h("div", { class: "stack" }, at, dayChips);
  const valueBox = h("div", { class: "stack hidden" }, source, h("div", { class: "param" },
    op, threshold));
  kind.addEventListener("change", () => {
    timeBox.classList.toggle("hidden", kind.value !== "time");
    valueBox.classList.toggle("hidden", kind.value !== "value");
  });
  const target = select(outputs, t("auto.output"));
  const state = select([["true", t("device.turn_on")], ["false", t("device.turn_off")]],
    t("auto.state"));
  const save = h("button", { class: "btn primary block" }, t("auto.save"));
  save.addEventListener("click", async () => {
    if (!outputs.length) { toast(t("auto.no_outputs"), "err"); return; }
    const [deviceId, tag] = target.value.split("|");
    let trigger;
    if (kind.value === "time") {
      trigger = { type: "time", at: at.value || "08:00" };
      if (days.size && days.size < 7) trigger.weekdays = [...days].sort();
    } else {
      const [sd, st] = source.value.split("|");
      trigger = { type: "value", device_id: sd, tag: st, op: op.value,
        value: Number(threshold.value) };
    }
    save.disabled = true;
    try {
      await api("POST", `/api/platform/organizations/${org}/automations`, {
        name: nameIn.value.trim() || t("auto.untitled"), trigger,
        actions: [{ type: "write", device_id: deviceId, tag, value: state.value === "true" }] });
      toast(t("auto.saved"), "ok");
      done();
    } catch (err) { toast(errorText(err), "err"); save.disabled = false; }
  });
  return h("section", { class: "card stack" }, h("h2", { class: "h2" }, t("auto.new")), nameIn,
    h("span", { class: "small muted" }, t("auto.if")), kind, timeBox, valueBox,
    h("span", { class: "small muted" }, t("auto.then")), target, state, save);
}
