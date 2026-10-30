// Scenes and automations of a home (platform phase 6): run, switch on/off, build and edit,
// with the Persian calendar (weekdays from Saturday, Jalali dates, holidays).

import { api, dialog, errorText, h, home, icon, name, num, SCENE_ICONS, SCENE_TONE, t,
  toast } from "./core.js";
import { homes } from "./views.js";

const OPS = ["gt", "lt", "eq", "ne", "ge", "le"];

async function homeOrg() {
  if (home.id) return home.id;
  const rows = await homes();
  const own = rows.find((m) => m.personal) || rows[0];
  return own ? own.org_id : null;
}

async function catalog() {          // the home's devices with their tags (for the builders)
  const devices = await api("GET", `/api/platform/my/devices${home.query()}`);
  const views = await Promise.all(devices.map((d) =>
    api("GET", `/api/platform/my/devices/${d.device_id}/view`).catch(() => null)));
  return views.filter(Boolean);
}

function tagOf(views, deviceId, key) {
  const v = views.find((x) => x.device.device_id === deviceId);
  return { v, tag: v && v.tags.find((x) => x.key === key) };
}

function tagName(views, deviceId, key) {
  const { v, tag } = tagOf(views, deviceId, key);
  return tag ? `${name(tag.display_name)} (${v.device.name})` : key;
}

function valueText(v) {
  return typeof v === "boolean" ? (v ? t("device.on") : t("device.off")) : num(v);
}

function days(list) {
  if (!list || list.length === 7) return t("auto.every_day");
  const sorted = [...list].sort();
  const run = sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1);
  if (run && sorted.length > 2) {
    return t("auto.day_range", { from: t(`week.${sorted[0]}`), to: t(`week.${sorted[sorted.length - 1]}`) });
  }
  return sorted.map((d) => t(`week.${d}`)).join("، ");
}

/** Chips of an automation: [kind, text] for "when", the conditions, and "then". */
export function describe(def, views) {
  const tr = def.trigger;
  const when = [];
  if (tr.type === "time") {
    when.push(["violet", t("auto.at_time", { at: num(tr.at) })]);
    when.push(["violet", days(tr.weekdays)]);
    if (tr.holidays === "skip") when.push(["violet", t("auto.no_holidays")]);
    if (tr.holidays === "only") when.push(["violet", t("auto.only_holidays")]);
  } else if (tr.type === "interval") {
    when.push(["violet", t("auto.when_interval", { n: num(tr.every_min) })]);
    if (tr.between) when.push(["violet", t("auto.between", { a: num(tr.between[0]), b: num(tr.between[1]) })]);
  } else if (tr.type === "once") {
    when.push(["violet", t("auto.when_once", { date: num(tr.date), at: num(tr.at) })]);
  } else if (tr.type === "value") {
    when.push(["ok", t("auto.when_value", { tag: tagName(views, tr.device_id, tr.tag),
      op: t(`op.${tr.op}`), value: valueText(tr.value) })]);
  } else if (tr.type === "sun") {
    const off = tr.offset_min || 0;
    when.push(["violet", off ? t(off < 0 ? "auto.sun_before" : "auto.sun_after",
      { n: num(Math.abs(off)), event: t(`auto.${tr.event}`) }) : t(`auto.${tr.event}`)]);
    if (tr.weekdays) when.push(["violet", days(tr.weekdays)]);
  } else if (tr.type === "presence") {
    when.push(["ok", t(tr.event === "arrive" ? "auto.when_arrive" : "auto.when_leave")]);
  } else {
    const { v } = tagOf(views, tr.device_id, "");
    when.push(["ok", tr.event === "online" ? t("auto.when_online_x", { device: v ? v.device.name : "" })
      : t("auto.when_offline_x", { device: v ? v.device.name : "", n: num(Math.round((tr.for_s || 60) / 60)) })]);
  }
  if (tr.start || tr.end) {
    when.push(["violet", t("auto.range_x", { a: tr.start ? num(tr.start) : "…", b: tr.end ? num(tr.end) : "…" })]);
  }
  const and = [];
  for (const c of def.conditions || []) {
    and.push(["ok", t("auto.when_value", { tag: tagName(views, c.device_id, c.tag), op: t(`op.${c.op}`),
      value: valueText(c.value) })]);
  }
  if (def.window?.between) {
    and.push(["violet", t("auto.between", { a: num(def.window.between[0]), b: num(def.window.between[1]) })]);
  }
  if (def.window?.weekdays) and.push(["violet", days(def.window.weekdays)]);
  const then = def.actions.map((a) => {
    if (a.type === "write") {
      const { tag } = tagOf(views, a.device_id, a.tag);
      if (typeof a.value === "boolean") {
        return t(a.value ? "auto.turn_on_x" : "auto.turn_off_x", { x: tagName(views, a.device_id, a.tag) });
      }
      return `${tag ? tagName(views, a.device_id, a.tag) : a.tag} ← ${num(a.value)}`;
    }
    if (a.type === "scene") return t("auto.run_scene");
    if (a.type === "delay") return t("auto.wait", { n: num(a.seconds) });
    if (a.type === "cycle") return t("auto.cycle_x", { x: tagName(views, a.device_id, a.tag) });
    const via = (a.channels || ["email"]).map((c) => t(`auto.via_${c}`)).join("، ");
    return `${t("auto.notify_x", { text: a.text })} (${via})`;
  });
  return { when, and, then };
}

export function automationsPage(tab = "scenes") {
  const body = h("div", { class: "stack" });
  const counter = h("span", { class: "small muted" });
  let org = null;
  let views = [];
  let scenes = [];
  let autos = [];

  async function draw() {
    try {
      org = org || (await homeOrg());
      if (!org) { body.replaceChildren(h("p", { class: "muted" }, t("auto.no_home"))); return; }
      [views, scenes, autos] = await Promise.all([catalog(),
        api("GET", `/api/platform/organizations/${org}/scenes`),
        api("GET", `/api/platform/organizations/${org}/automations`)
          .then((rows) => rows.filter((a) => !a.definition?.timer))]);    // timers: on the device page
    } catch (err) {
      body.replaceChildren(h("p", {}, errorText(err)));
      return;
    }
    if (tab === "scenes") {
      body.replaceChildren(scenes.length ? h("div", { class: "grid" }, scenes.map(sceneCard))
        : h("div", { class: "card empty" }, icon("star", "i lg"), h("p", {}, t("auto.no_scenes"))));
    } else {
      const active = autos.filter((a) => a.enabled).length;
      counter.textContent = t("auto.active_of", { n: num(active), total: num(autos.length) });
      body.replaceChildren(
        h("div", { class: "row between" }, h("h2", { class: "h2" }, t("auto.active")), counter),
        ...(autos.length ? autos.map(autoCard)
          : [h("div", { class: "card empty" }, icon("sliders", "i lg"), h("p", {}, t("auto.none")))]));
    }
  }

  function sceneCard(s) {
    const run = h("button", { class: `scene-card tone-${SCENE_TONE[s.icon] || "accent"}`, onclick: async () => {
      run.disabled = true;
      try {
        const r = await api("POST", `/api/platform/organizations/${org}/scenes/${s.scene_id}/run`);
        const bad = r.detail.filter((d) => d.tag && d.result !== "applied").length;
        toast(bad ? t("auto.scene_partial", { n: num(bad) }) : t("auto.scene_done"), bad ? "err" : "ok");
      } catch (err) { toast(errorText(err), "err"); }
      run.disabled = false;
    } }, icon(s.icon || "power", "i lg"),
    h("span", { class: "stack tight" }, h("span", { class: "name" }, s.name),
      h("span", { class: "tiny sub" }, t("auto.actions", { n: num(s.actions.length) }))));
    const edit = h("button", { class: "icon-btn edit-btn", "aria-label": t("auto.edit_x", { x: s.name }),
      onclick: () => sceneBuilder(org, views, s, draw) }, icon("edit"));
    const plan = h("button", { class: "icon-btn plan-btn", "aria-label": t("auto.schedule_x", { x: s.name }),
      onclick: () => scheduleScene(org, s, draw) }, icon("clock"));
    return h("div", { class: "scene-wrap" }, run, edit, plan);
  }

  function autoCard(a) {
    const { when, and, then } = describe(a.definition, views);
    const sw = h("button", { class: `switch ${a.enabled ? "on" : ""}`, "aria-pressed": a.enabled,
      "aria-label": a.enabled ? t("auto.disable") : t("auto.enable"), onclick: async () => {
        sw.disabled = true;
        try {
          await api("PUT", `/api/platform/organizations/${org}/automations/${a.automation_id}`,
            { name: a.name, enabled: !a.enabled, ...a.definition });
          draw();
        } catch (err) { toast(errorText(err), "err"); sw.disabled = false; }
      } }, h("span", { class: "track" }, h("span", { class: "knob" })));
    const edit = h("button", { class: "icon-btn", "aria-label": t("auto.edit_x", { x: a.name }),
      onclick: () => autoBuilder(org, views, scenes, a, draw) }, icon("edit"));
    const chips = (list) => list.map(([kind, text]) => h("span", { class: `chip ${kind}` }, text));
    return h("div", { class: "card stack auto-card" },
      h("div", { class: "row between" }, h("span", { class: "strong" }, a.name),
        h("div", { class: "row" }, edit, sw)),
      h("div", { class: "row wrap small" },
        h("span", { class: "muted" }, t("auto.if")), chips(when),
        and.length ? [h("span", { class: "muted" }, t("auto.and")), chips(and)] : null,
        h("span", { class: "muted" }, t("auto.then")), then.map((x) => h("span", { class: "chip accent" }, x))),
      a.next_runs?.length ? h("span", { class: "tiny muted" }, t("auto.next", {
        at: new Intl.DateTimeFormat("fa-IR-u-ca-persian", { dateStyle: "medium",
          timeStyle: "short", timeZone: "Asia/Tehran" }).format(new Date(a.next_runs[0])) })) : null);
  }

  const plus = h("button", { class: "icon-btn accent-btn", "aria-label": t(tab === "scenes" ? "auto.new_scene" : "auto.new"),
    onclick: () => (tab === "scenes" ? sceneBuilder(org, views, null, draw) : autoBuilder(org, views, scenes, null, draw)) },
  icon("plus"));
  const seg = h("div", { class: "seg", role: "tablist" },
    h("a", { href: "#/auto/scenes", role: "tab", class: tab === "scenes" ? "on" : "", "aria-selected": tab === "scenes" },
      t("auto.scenes")),
    h("a", { href: "#/auto/automations", role: "tab", class: tab !== "scenes" ? "on" : "",
      "aria-selected": tab !== "scenes" }, t("auto.automations")));
  draw();
  return h("div", { class: "page" },
    h("div", { class: "row between" }, h("h1", { class: "h1" }, t("auto.title")), plus), seg, body);
}

// --- builders ------------------------------------------------------------------------------------

function select(options, label, value = null) {
  return h("select", { class: "input", "aria-label": label },
    options.map(([v, text]) => h("option", { value: v, selected: value !== null && String(v) === String(value) }, text)));
}

function labelled(label, control) {
  return h("div", { class: "field" }, h("span", { class: "label" }, label), control);
}

function outputs(views) {
  const out = [];
  for (const v of views) {
    for (const tag of v.tags) {
      if (tag.writable && tag.ui?.scenario !== false) {
        out.push({ id: `${v.device.device_id}|${tag.key}`, text: `${name(tag.display_name)} — ${v.device.name}`, tag });
      }
    }
  }
  return out;
}

function readings(views) {
  const out = [];
  for (const v of views) {
    for (const tag of v.tags) {
      if (tag.data_type !== "string" && tag.ui?.scenario !== false) {
        out.push({ id: `${v.device.device_id}|${tag.key}`, text: `${name(tag.display_name)} — ${v.device.name}`, tag });
      }
    }
  }
  return out;
}

/** One action row: write a tag (on/off or a value), a delay, a scene, a notice. */
function actionRow(views, scenes, initial, allowed, remove) {
  const outs = outputs(views);
  const types = allowed.map((x) => [x, t(`auto.action_${x}`)]);
  const kind = select(types, t("auto.action"), initial?.type || "write");
  const box = h("div", { class: "stack" });
  const row = h("div", { class: "card tight stack action" },
    h("div", { class: "row" }, h("div", { class: "grow" }, kind),
      h("button", { type: "button", class: "icon-btn", "aria-label": t("auto.remove_action"), onclick: remove }, icon("trash"))),
    box);
  let read = () => null;
  const drawBox = () => {
    if (kind.value === "write") {
      const target = select(outs.map((o) => [o.id, o.text]), t("auto.output"),
        initial?.type === "write" ? `${initial.device_id}|${initial.tag}` : null);
      const valueBox = h("div");
      let value = () => null;
      const drawValue = () => {
        const o = outs.find((x) => x.id === target.value);
        if (!o) { valueBox.replaceChildren(); value = () => null; return; }
        if (o.tag.data_type === "bool") {
          const s = select([["true", t("device.turn_on")], ["false", t("device.turn_off")]], t("auto.state"),
            initial?.type === "write" && typeof initial.value === "boolean" ? String(initial.value) : "true");
          valueBox.replaceChildren(s);
          value = () => s.value === "true";
        } else if (o.tag.states && Object.keys(o.tag.states).length) {
          const s = select(Object.entries(o.tag.states), t("auto.state"), initial?.value);
          valueBox.replaceChildren(s);
          value = () => Number(s.value);
        } else {
          const n = h("input", { class: "input ltr", type: "number", step: "any", min: o.tag.min ?? undefined,
            max: o.tag.max ?? undefined, value: initial?.type === "write" ? initial.value : o.tag.min ?? 0,
            "aria-label": t("auto.value") });
          valueBox.replaceChildren(n);
          value = () => Number(n.value);
        }
      };
      target.addEventListener("change", drawValue);
      drawValue();
      box.replaceChildren(target, valueBox);
      read = () => {
        if (!target.value) return null;
        const [device_id, tag] = target.value.split("|");
        return { type: "write", device_id, tag, value: value() };
      };
    } else if (kind.value === "delay") {
      const s = h("input", { class: "input ltr", type: "number", min: 1, max: 3600,
        value: initial?.type === "delay" ? initial.seconds : 10, "aria-label": t("auto.seconds") });
      box.replaceChildren(labelled(t("auto.seconds"), s));
      read = () => ({ type: "delay", seconds: Number(s.value) || 1 });
    } else if (kind.value === "scene") {
      const s = select(scenes.map((x) => [x.scene_id, x.name]), t("auto.scene"), initial?.scene_id);
      box.replaceChildren(s);
      read = () => (s.value ? { type: "scene", scene_id: Number(s.value) } : null);
    } else {
      const text = h("input", { class: "input", value: initial?.type === "notify" ? initial.text : "",
        placeholder: t("auto.notify_hint"), "aria-label": t("auto.notify_text") });
      const had = new Set(initial?.type === "notify" ? initial.channels || ["email"] : ["email"]);
      const boxes = [["email", "out"], ["sms", "chat"], ["call", "call"]].map(([c, ic]) => {
        const cb = h("input", { type: "checkbox", checked: had.has(c) });
        return [c, cb, h("label", { class: "check-row chip-check" }, cb, icon(ic), h("span", { class: "small" }, t(`auto.via_${c}`)))];
      });
      box.replaceChildren(text, h("div", { class: "row wrap" }, boxes.map((b) => b[2])),
        h("span", { class: "tiny muted lh" }, t("auto.via_help")));
      read = () => {
        const channels = boxes.filter((b) => b[1].checked).map((b) => b[0]);
        return text.value.trim() ? { type: "notify", text: text.value.trim(), channels: channels.length ? channels : ["email"] } : null;
      };
    }
  };
  kind.addEventListener("change", () => { initial = null; drawBox(); });
  drawBox();
  return { el: row, read: () => read() };
}

function actionList(views, scenes, initial, allowed) {
  const list = h("div", { class: "stack" });
  const rows = [];
  const addRow = (a) => {
    const entry = actionRow(views, scenes, a, allowed, () => {
      rows.splice(rows.indexOf(entry), 1);
      entry.el.remove();
    });
    rows.push(entry);
    list.append(entry.el);
  };
  (initial.length ? initial : [null]).forEach(addRow);
  const more = h("button", { type: "button", class: "btn block" }, icon("plus"), t("auto.add_action"));
  more.addEventListener("click", () => addRow(null));
  return { el: h("div", { class: "stack" }, list, more), read: () => rows.map((r) => r.read()).filter(Boolean) };
}

async function sceneBuilder(org, views, scene, done) {
  const nameIn = h("input", { class: "input", value: scene?.name || "", placeholder: t("auto.scene_name_hint"),
    "aria-label": t("auto.name") });
  let iconName = scene?.icon || "moon";
  const icons = h("div", { class: "row wrap" });
  const drawIcons = () => icons.replaceChildren(...SCENE_ICONS.map((x) => h("button", { type: "button",
    class: `icon-pick tone-${SCENE_TONE[x]} ${x === iconName ? "on" : ""}`, "aria-pressed": x === iconName,
    "aria-label": t(`icons.${x}`), onclick: () => { iconName = x; drawIcons(); } }, icon(x))));
  drawIcons();
  const actions = actionList(views, [], scene?.actions || [], ["write", "delay"]);
  const keyword = h("input", { class: "input", value: scene?.sms_keyword || "", maxlength: 20,
    placeholder: t("auto.keyword_hint"), "aria-label": t("auto.keyword") });
  const body = h("div", { class: "stack" }, labelled(t("auto.name"), nameIn),
    labelled(t("auto.icon"), icons), h("span", { class: "label" }, t("auto.scene_actions")), actions.el,
    h("details", { class: "more", open: Boolean(scene?.sms_keyword) }, h("summary", {}, t("auto.keyword")),
      h("div", { class: "stack" }, keyword, h("span", { class: "tiny muted lh" }, t("auto.keyword_help")),
        h("a", { class: "link small", href: `#/home-settings/${org}` }, t("auto.keyword_setup")))));
  const buttons = [["cancel", t("common.cancel")], ["save", t("auto.save"), "primary"]];
  if (scene) buttons.unshift(["delete", t("auto.delete"), "danger"]);
  for (;;) {
    const answer = await dialog(scene ? t("auto.edit_scene") : t("auto.new_scene"), body, buttons);
    if (answer === "delete") {
      if (!confirm(t("auto.delete_scene_confirm"))) continue;
      await api("DELETE", `/api/platform/organizations/${org}/scenes/${scene.scene_id}`).catch((err) => toast(errorText(err), "err"));
      done();
      return;
    }
    if (answer !== "save") return;
    const payload = { name: nameIn.value.trim() || t("auto.untitled"), icon: iconName, actions: actions.read(),
      sms_keyword: keyword.value.trim() || null };
    try {
      if (scene) await api("PUT", `/api/platform/organizations/${org}/scenes/${scene.scene_id}`, payload);
      else await api("POST", `/api/platform/organizations/${org}/scenes`, payload);
      toast(t("auto.scene_saved"), "ok");
      done();
      return;
    } catch (err) { toast(errorText(err), "err"); }
  }
}

/** Run a scene at a time of day (Jalali weekdays, holidays): an automation with one action. */
async function scheduleScene(org, scene, done) {
  const at = h("input", { class: "input ltr", type: "time", value: "20:00", "aria-label": t("auto.at") });
  const chosen = dayChips(null);
  const holidays = select([["any", t("auto.holidays_any")], ["skip", t("auto.holidays_skip")],
    ["only", t("auto.holidays_only")]], t("auto.holidays"), "any");
  const answer = await dialog(t("auto.schedule_x", { x: scene.name }), h("div", { class: "stack" },
    labelled(t("auto.at"), at), chosen.el, labelled(t("auto.holidays"), holidays),
    h("span", { class: "tiny muted lh" }, t("auto.schedule_help"))),
  [["cancel", t("common.cancel")], ["save", t("auto.save"), "primary"]]);
  if (answer !== "save") return;
  const trigger = { type: "time", at: at.value || "20:00" };
  const d = chosen.read();
  if (d) trigger.weekdays = d;
  if (holidays.value !== "any") trigger.holidays = holidays.value;
  try {
    await api("POST", `/api/platform/organizations/${org}/automations`, {
      name: t("auto.scene_at", { name: scene.name, at: trigger.at }), trigger,
      actions: [{ type: "scene", scene_id: scene.scene_id }] });
    toast(t("auto.saved"), "ok");
    done();
  } catch (err) { toast(errorText(err), "err"); }
}

function dayChips(selected) {
  const chosen = new Set(selected || [0, 1, 2, 3, 4, 5, 6]);
  const el = h("div", { class: "row wrap" }, [0, 1, 2, 3, 4, 5, 6].map((d) => {
    const chip = h("button", { class: `tab ${chosen.has(d) ? "on" : ""}`, type: "button", "aria-pressed": chosen.has(d),
      onclick: () => {
        if (chosen.has(d)) chosen.delete(d); else chosen.add(d);
        chip.classList.toggle("on", chosen.has(d));
        chip.setAttribute("aria-pressed", String(chosen.has(d)));
      } }, t(`week.${d}`));
    return chip;
  }));
  return { el, read: () => (chosen.size && chosen.size < 7 ? [...chosen].sort() : null) };
}

async function autoBuilder(org, views, scenes, record, done) {
  const def = record?.definition || { trigger: { type: "time", at: "08:00" }, actions: [] };
  const tr = def.trigger;
  const nameIn = h("input", { class: "input", value: record?.name || "", placeholder: t("auto.name_hint"),
    "aria-label": t("auto.name") });
  const kind = select(["time", "sun", "interval", "value", "offline", "presence", "once"]
    .map((k) => [k, t(`auto.kind_${k}`)]), t("auto.trigger"), tr.type);
  const boxes = {};
  // time
  const at = h("input", { class: "input ltr", type: "time", value: tr.at || "08:00", "aria-label": t("auto.at") });
  const tDays = dayChips(tr.type === "time" ? tr.weekdays : null);
  const holidays = select([["any", t("auto.holidays_any")], ["skip", t("auto.holidays_skip")],
    ["only", t("auto.holidays_only")]], t("auto.holidays"), tr.holidays || "any");
  // the days the schedule is in force: from / until (Jalali), empty = always
  const jIn = (v, label) => h("input", { class: "input ltr", value: v || "", placeholder: "1405/09/01", "aria-label": label });
  const rangeFrom = jIn(tr.start, t("auto.range_from"));
  const rangeTo = jIn(tr.end, t("auto.range_to"));
  const range = h("div", { class: "stack" }, h("div", { class: "two" }, labelled(t("auto.range_from"), rangeFrom),
    labelled(t("auto.range_to"), rangeTo)), h("span", { class: "tiny muted" }, t("auto.range_help")));
  const latin = (v) => v.trim().replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d));
  const withRange = (trigger) => {
    if (rangeFrom.value.trim()) trigger.start = latin(rangeFrom.value);
    if (rangeTo.value.trim()) trigger.end = latin(rangeTo.value);
    return trigger;
  };
  boxes.time = h("div", { class: "stack" }, labelled(t("auto.at"), at), tDays.el, labelled(t("auto.holidays"), holidays));
  // interval
  const every = h("input", { class: "input ltr", type: "number", min: 1, max: 1440, value: tr.every_min || 15,
    "aria-label": t("auto.every_min") });
  const iFrom = h("input", { class: "input ltr", type: "time", value: tr.between?.[0] || "", "aria-label": t("auto.from") });
  const iTo = h("input", { class: "input ltr", type: "time", value: tr.between?.[1] || "", "aria-label": t("auto.to") });
  boxes.interval = h("div", { class: "stack" }, labelled(t("auto.every_min"), every),
    h("div", { class: "two" }, labelled(t("auto.from"), iFrom), labelled(t("auto.to"), iTo)));
  // value
  const reads = readings(views);
  const source = select(reads.map((o) => [o.id, o.text]), t("auto.value_of"),
    tr.type === "value" ? `${tr.device_id}|${tr.tag}` : null);
  const op = select(OPS.map((o) => [o, t(`op.${o}`)]), t("auto.op"), tr.op || "gt");
  const threshold = h("input", { class: "input ltr", value: tr.type === "value" ? String(tr.value) : "0",
    "aria-label": t("auto.threshold") });
  const forMin = h("input", { class: "input ltr", type: "number", min: 0, max: 1440,
    value: tr.type === "value" ? Math.round((tr.for_s || 0) / 60) : 0, "aria-label": t("auto.for_min") });
  boxes.value = h("div", { class: "stack" }, source, h("div", { class: "two" }, op, threshold),
    labelled(t("auto.for_min"), forMin));
  // offline
  const devSel = select(views.map((v) => [v.device.device_id, v.device.name]), t("auto.device"),
    tr.type === "offline" ? tr.device_id : null);
  const offMin = h("input", { class: "input ltr", type: "number", min: 1, max: 1440,
    value: tr.type === "offline" ? Math.round((tr.for_s || 60) / 60) : 5, "aria-label": t("auto.for_min") });
  const offEvent = select([["offline", t("auto.went_offline")], ["online", t("auto.came_online")]], t("auto.device_event"),
    tr.type === "offline" ? tr.event || "offline" : "offline");
  boxes.offline = h("div", { class: "stack" }, devSel, offEvent, labelled(t("auto.for_min"), offMin));
  // once
  const date = h("input", { class: "input ltr", value: tr.date || "", placeholder: "1405/08/15", "aria-label": t("auto.date") });
  const onceAt = h("input", { class: "input ltr", type: "time", value: tr.type === "once" ? tr.at : "08:00",
    "aria-label": t("auto.at") });
  boxes.once = h("div", { class: "two" }, labelled(t("auto.date"), date), labelled(t("auto.at"), onceAt));
  // sunrise / sunset at the home's place
  const sunEvent = select([["sunset", t("auto.sunset")], ["sunrise", t("auto.sunrise")]], t("auto.sun_event"),
    tr.type === "sun" ? tr.event : "sunset");
  const sunOffset = h("input", { class: "input ltr", type: "number", min: -180, max: 180,
    value: tr.type === "sun" ? tr.offset_min || 0 : 0, "aria-label": t("auto.sun_offset") });
  const sDays = dayChips(tr.type === "sun" ? tr.weekdays : null);
  const place = { latitude: tr.latitude ?? null, longitude: tr.longitude ?? null };
  const placeNote = h("span", { class: "tiny muted" });
  api("GET", `/api/platform/organizations/${org}/settings`).then((st) => {
    if (place.latitude === null && st.latitude !== null) {
      place.latitude = st.latitude;
      place.longitude = st.longitude;
    }
    placeNote.textContent = place.latitude === null ? t("auto.sun_no_place") : t("auto.sun_place");
  }).catch(() => {});
  boxes.sun = h("div", { class: "stack" }, sunEvent, labelled(t("auto.sun_offset"), sunOffset), sDays.el,
    placeNote, h("a", { class: "link small", href: `#/home-settings/${org}` }, t("auto.sun_set_place")));
  // presence
  const presence = select([["arrive", t("auto.when_arrive")], ["leave", t("auto.when_leave")]],
    t("auto.presence"), tr.type === "presence" ? tr.event : "arrive");
  boxes.presence = h("div", { class: "stack" }, presence,
    h("span", { class: "tiny muted lh" }, t("auto.presence_help")));
  const triggerBox = h("div");
  const showTrigger = () => triggerBox.replaceChildren(boxes[kind.value],
    ...(["time", "interval", "sun"].includes(kind.value) ? [range] : []));
  kind.addEventListener("change", showTrigger);
  showTrigger();

  // conditions: a time window, weekdays, one value condition
  const wFrom = h("input", { class: "input ltr", type: "time", value: def.window?.between?.[0] || "", "aria-label": t("auto.from") });
  const wTo = h("input", { class: "input ltr", type: "time", value: def.window?.between?.[1] || "", "aria-label": t("auto.to") });
  // value conditions on any device of the home (an input of one device, a relay of another)
  const condList = h("div", { class: "stack" });
  const condRows = [];
  const addCond = (c = null) => {
    const src = select(reads.map((o) => [o.id, o.text]), t("auto.value_of"), c ? `${c.device_id}|${c.tag}` : null);
    const cop = select(OPS.map((o) => [o, t(`op.${o}`)]), t("auto.op"), c?.op || "gt");
    const cval = h("input", { class: "input ltr", value: c ? String(c.value) : "0", "aria-label": t("auto.threshold") });
    const entry = { read: () => {
      const [device_id, tag] = src.value.split("|");
      return { device_id, tag, op: cop.value, value: parseValue(cval.value, tagOf(views, device_id, tag).tag) };
    } };
    const row = h("div", { class: "card tight stack action" }, src, h("div", { class: "cond" }, cop, cval,
      h("button", { type: "button", class: "icon-btn", "aria-label": t("auto.remove_condition"), onclick: () => {
        condRows.splice(condRows.indexOf(entry), 1);
        row.remove();
      } }, icon("trash"))));
    condRows.push(entry);
    condList.append(row);
  };
  (def.conditions || []).forEach((c) => addCond(c));
  const addCondBtn = h("button", { type: "button", class: "btn block", disabled: !reads.length,
    onclick: () => { if (condRows.length < 10) addCond(); } }, icon("plus"), t("auto.add_condition"));
  const local = h("input", { type: "checkbox", checked: Boolean(def.local) });
  const actions = actionList(views, scenes, def.actions, ["write", "scene", "delay", "notify"]);
  const respect = h("input", { class: "input ltr", type: "number", min: 0, max: 1440,
    value: def.respect_manual_min ?? 60, "aria-label": t("auto.respect") });
  const missed = select([["skip", t("auto.missed_skip")], ["catch_up", t("auto.missed_catch_up")]],
    t("auto.missed"), def.missed || "skip");

  function parseValue(text, tag) {
    if (tag?.data_type === "bool") return ["1", "true", "on", "روشن"].includes(text.trim().toLowerCase());
    const n = Number(text);
    return Number.isNaN(n) ? text : n;
  }
  const read = () => {
    let trigger;
    if (kind.value === "time") {
      trigger = { type: "time", at: at.value || "08:00" };
      const d = tDays.read();
      if (d) trigger.weekdays = d;
      if (holidays.value !== "any") trigger.holidays = holidays.value;
      withRange(trigger);
    } else if (kind.value === "interval") {
      trigger = withRange({ type: "interval", every_min: Number(every.value) || 15 });
      if (iFrom.value && iTo.value) trigger.between = [iFrom.value, iTo.value];
    } else if (kind.value === "value") {
      const [device_id, tag] = source.value.split("|");
      trigger = { type: "value", device_id, tag, op: op.value,
        value: parseValue(threshold.value, tagOf(views, device_id, tag).tag), for_s: (Number(forMin.value) || 0) * 60 };
    } else if (kind.value === "offline") {
      trigger = { type: "offline", device_id: devSel.value, event: offEvent.value,
        for_s: offEvent.value === "online" ? 0 : Math.max(10, (Number(offMin.value) || 1) * 60) };
    } else if (kind.value === "sun") {
      if (place.latitude === null) throw new Error(t("auto.sun_no_place"));
      trigger = { type: "sun", event: sunEvent.value, offset_min: Number(sunOffset.value) || 0,
        latitude: place.latitude, longitude: place.longitude };
      const d = sDays.read();
      if (d) trigger.weekdays = d;
      withRange(trigger);
    } else if (kind.value === "presence") {
      trigger = { type: "presence", event: presence.value };
    } else {
      trigger = { type: "once", date: date.value.trim().replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d)),
        at: onceAt.value || "08:00" };
    }
    const out = { name: nameIn.value.trim() || t("auto.untitled"), enabled: record ? record.enabled : true,
      trigger, actions: actions.read(), respect_manual_min: Number(respect.value) || 0, missed: missed.value,
      local: local.checked };
    if (wFrom.value && wTo.value) out.window = { between: [wFrom.value, wTo.value] };
    const conditions = condRows.map((r) => r.read()).filter((c) => c.device_id && c.tag);
    if (conditions.length) out.conditions = conditions;
    for (const k of ["priority", "catch_up_min"]) if (def[k] !== undefined) out[k] = def[k];
    return out;
  };
  const body = h("div", { class: "stack" },
    labelled(t("auto.name"), nameIn),
    h("span", { class: "section-label" }, t("auto.if")), kind, triggerBox,
    h("details", { class: "more", open: Boolean(def.window || def.conditions?.length) },
      h("summary", {}, t("auto.and_only")),
      h("div", { class: "stack" }, h("div", { class: "two" }, labelled(t("auto.from"), wFrom), labelled(t("auto.to"), wTo)),
        condList, addCondBtn)),
    h("span", { class: "section-label" }, t("auto.then")), actions.el,
    h("details", { class: "more" }, h("summary", {}, t("auto.advanced")),
      h("div", { class: "stack" }, labelled(t("auto.respect"), respect), h("span", { class: "tiny muted" }, t("auto.respect_help")),
        labelled(t("auto.missed"), missed),
        h("label", { class: "check-row" }, local, h("span", { class: "small" }, t("auto.local"))),
        h("span", { class: "tiny muted lh" }, t("auto.local_help")))));
  const buttons = [["cancel", t("common.cancel")], ["save", t("auto.save"), "primary"]];
  if (record) buttons.unshift(["test", t("auto.test")], ["delete", t("auto.delete"), "danger"]);
  for (;;) {
    const answer = await dialog(record ? t("auto.edit") : t("auto.new"), body, buttons);
    if (answer === "delete") {
      if (!confirm(t("auto.delete_confirm"))) continue;
      await api("DELETE", `/api/platform/organizations/${org}/automations/${record.automation_id}`)
        .catch((err) => toast(errorText(err), "err"));
      done();
      return;
    }
    if (answer === "test") {
      try {
        const r = await api("POST", `/api/platform/organizations/${org}/automations/${record.automation_id}/test`);
        toast(t("auto.tested", { n: num((r.detail || []).length) }), "ok");
      } catch (err) { toast(errorText(err), "err"); }
      continue;
    }
    if (answer !== "save") return;
    let payload;
    try { payload = read(); } catch (err) { toast(err.message, "err"); continue; }
    if (!payload.actions.length) { toast(t("auto.no_actions"), "err"); continue; }
    try {
      if (record) await api("PUT", `/api/platform/organizations/${org}/automations/${record.automation_id}`, payload);
      else await api("POST", `/api/platform/organizations/${org}/automations`, payload);
      toast(t("auto.saved"), "ok");
      done();
      return;
    } catch (err) { toast(errorText(err), "err"); }
  }
}
