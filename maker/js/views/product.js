// One product: details, inputs/outputs (the template's tags), commands, protocol bindings,
// versions and firmware. Edits go to the draft version; publishing freezes it.

import { api, busy, confirmAction, date, dialog, errorText, h, icon, name, num, t,
  toast } from "../core.js";
import { can, checkbox, crumbs, ctx, field, input, picture, protocols, select } from "../ui.js";

const TABS = ["spec", "io", "commands", "protocols", "versions"];
const KINDS = ["digital_output", "digital_input", "analog_input", "analog_output", "variable",
  "parameter", "calculated"];
const KIND_CHIP = { digital_output: "accent", analog_output: "accent", digital_input: "teal",
  analog_input: "teal", variable: "teal", parameter: "violet", calculated: "orange" };
const READ_ONLY = new Set(["digital_input", "analog_input", "variable", "calculated"]);
const NUMERIC = ["int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"];
const TYPES = ["bool", ...NUMERIC, "string"];
const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
const faNum = (n) => String(n).replace(/\d/g, (d) => FA_DIGITS[d]);

function typesFor(kind) {
  if (kind === "calculated") return ["float64", "float32", "bool"];
  if (kind === "digital_output" || kind === "digital_input") return ["bool"];
  if (kind === "analog_output" || kind === "analog_input") return NUMERIC;
  return TYPES;
}

const clone = (x) => JSON.parse(JSON.stringify(x));

export default async function productPage(args) {
  const [productId, tabArg] = args;
  const state = { tab: TABS.includes(tabArg) ? tabArg : "io", sel: null, dirty: false, notes: "" };
  const manage = can("product.manage");
  const body = h("div", { class: "stack loose" });
  let schema = {};
  try { schema = await protocols(); } catch { /* forms without the protocol schema */ }

  async function load() {
    state.product = await api("GET", `/api/platform/products/${productId}`);
    state.versions = await api("GET", `/api/platform/products/${productId}/versions`);
    const draft = state.versions.find((v) => v.status === "draft");
    const published = state.versions.filter((v) => v.status === "published");
    state.draft = draft ? draft.version : null;
    state.latest = published.length ? Math.max(...published.map((v) => v.version)) : null;
    state.publishedKeys = new Set(published.flatMap((v) => (v.definition.tags || []).map((x) => x.key)));
    const base = draft || published.find((v) => v.version === state.latest);
    state.def = normalize(base ? clone(base.definition) : { tags: [], bindings: { native: { tags: {} } } });
    state.notes = draft?.notes || "";
    state.dirty = false;
    state.sel = state.def.tags[0]?.key || null;
  }

  function normalize(def) {
    def.tags = def.tags || [];
    def.bindings = def.bindings || {};
    for (const tag of def.tags) {
      tag.ui = tag.ui || {};
      tag.display_name = tag.display_name || {};
    }
    for (const b of Object.values(def.bindings)) {
      b.tags = b.tags || {};
      b.connection = b.connection || {};
      b.device = b.device || {};
      for (const tag of def.tags) b.tags[tag.key] = b.tags[tag.key] || {};
    }
    return def;
  }

  function sorted() {
    return [...state.def.tags].sort((a, b) => (a.ui.order || 0) - (b.ui.order || 0) ||
      a.key.localeCompare(b.key));
  }

  function changed() {
    state.dirty = true;
    header();
  }

  window.onbeforeunload = () => (state.dirty ? true : undefined);

  // --- header and tabs ---------------------------------------------------------------------

  const headBox = h("div", { class: "stack loose" });
  const tabBox = h("div", { class: "stack loose" });
  function header() {
    const p = state.product;
    const nextVersion = state.draft || (state.latest || 0) + 1;
    const pic = h("button", { class: "pic-btn", type: "button", "aria-label": t("product.change_picture"),
      disabled: !manage, onclick: () => pickImage() }, picture(p));
    const save = h("button", { class: "btn", disabled: !manage }, t("product.save_draft"));
    save.addEventListener("click", busy(save, saveDraft));
    const publish = h("button", { class: "btn primary", disabled: !manage },
      t("product.publish_n", { n: num(nextVersion) }));
    publish.addEventListener("click", busy(publish, publishDraft));
    headBox.replaceChildren(
      crumbs([t("menu.products"), "#/products"], p.name),
      h("div", { class: "head" },
        h("div", { class: "product-head" }, pic,
          h("div", { class: "stack tight" }, h("h1", { class: "h1" }, p.name),
            h("div", { class: "row wrap" },
              h("span", { class: "chip ltr" }, p.code),
              p.category ? h("span", { class: "chip" }, t("product.category_x", { x: p.category })) : null,
              state.dirty ? h("span", { class: "chip orange" }, t("product.unsaved"))
                : state.draft ? h("span", { class: "chip violet" }, t("product.draft_chip", { n: num(state.draft) }))
                  : null,
              state.latest ? h("span", { class: "chip teal" }, t("product.published_n",
                { n: num(state.latest), devices: num(p.devices) })) : h("span", { class: "chip" },
                t("product.unpublished"))))),
        h("div", { class: "row wrap" }, save, publish)),
      h("div", { class: "tabs", role: "tablist" }, TABS.map((key) => h("button", {
        role: "tab", class: `tab ${state.tab === key ? "on" : ""}`, "aria-selected": state.tab === key,
        onclick: () => {
          state.tab = key;
          history.replaceState(null, "", `#/product/${productId}/${key}`);
          header();
          drawTab();
        } }, t(`product.tab_${key}`)))));
  }

  async function pickImage() {
    const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp" });
    file.addEventListener("change", async () => {
      const f = file.files[0];
      if (!f) return;
      try {
        await api("PUT", `/api/platform/products/${productId}/image`, undefined, { raw: f, type: f.type });
        state.product = await api("GET", `/api/platform/products/${productId}`);
        ctx.products = null;
        toast(t("product.picture_saved"), "ok");
        header();
      } catch (err) { toast(errorText(err), "err"); }
    });
    file.click();
  }

  function definition() {
    const def = clone(state.def);
    for (const tag of def.tags) {
      for (const k of Object.keys(tag)) if (tag[k] === "" || tag[k] === null) delete tag[k];
      if (!tag.display_name.en) delete tag.display_name.en;
      if (!tag.display_name.fa) delete tag.display_name.fa;
      for (const k of Object.keys(tag.ui)) if (tag.ui[k] === "" || tag.ui[k] === null) delete tag.ui[k];
    }
    const calculated = def.tags.filter((x) => x.kind === "calculated").map((x) => x.key);
    for (const b of Object.values(def.bindings)) {
      for (const key of calculated) delete b.tags[key];        // the hub computes them
      for (const part of [b.connection, b.device, ...Object.values(b.tags)]) {
        for (const k of Object.keys(part)) if (part[k] === "" || part[k] === null) delete part[k];
      }
    }
    if (!def.commands?.length) delete def.commands;
    return def;
  }

  async function saveDraft() {
    const record = await api("PUT", `/api/platform/products/${productId}/draft`,
      { definition: definition(), notes: state.notes || undefined });
    state.draft = record.version;
    state.versions = await api("GET", `/api/platform/products/${productId}/versions`);
    state.dirty = false;
    toast(t("product.draft_saved", { n: num(record.version) }), "ok");
    header();
  }

  async function publishDraft() {
    if (state.dirty || !state.draft) await saveDraft();
    if (!await confirmAction(t("product.publish_confirm", { n: num(state.draft) }),
      t("product.publish"))) return;
    await api("POST", `/api/platform/products/${productId}/versions/${state.draft}/publish`);
    ctx.products = null;
    toast(t("product.published_ok"), "ok");
    await load();
    header();
    drawTab();
  }

  function drawTab() {
    const views = { spec, io, commands, protocolsTab, versions };
    const view = views[state.tab === "protocols" ? "protocolsTab" : state.tab];
    tabBox.replaceChildren(h("div", { class: "loading", role: "status" }, t("common.loading")));
    Promise.resolve(view()).then((node) => tabBox.replaceChildren(node))
      .catch((err) => tabBox.replaceChildren(h("p", { class: "muted" }, errorText(err))));
  }

  // --- spec ----------------------------------------------------------------------------------

  function spec() {
    const p = state.product;
    const nameIn = input({ value: p.name, disabled: !manage });
    const category = input({ value: p.category || "", disabled: !manage });
    const description = h("textarea", { class: "input", rows: 4, disabled: !manage }, p.description || "");
    const kind = select([["device", t("product.kind_device")], ["edge_hub", t("product.kind_edge_hub")]],
      { value: p.kind, disabled: !manage });
    const pattern = input({ value: p.identifier_pattern, dir: "ltr", class: "input key",
      disabled: !manage || p.devices > 0 });
    const catalog = checkbox(t("product.catalog"), p.catalog);
    catalog.box.disabled = !manage;
    const save = h("button", { class: "btn primary", disabled: !manage }, t("common.save"));
    save.addEventListener("click", busy(save, async () => {
      const changes = { name: nameIn.value.trim(), category: category.value, description: description.value,
        kind: kind.value, catalog: catalog.box.checked };
      if (!pattern.disabled) changes.identifier_pattern = pattern.value.trim().toUpperCase();
      state.product = await api("PATCH", `/api/platform/products/${productId}`, changes);
      ctx.products = null;
      toast(t("common.saved"), "ok");
      header();
    }));
    const removePic = h("button", { class: "btn sm danger", disabled: !manage || !p.has_image },
      icon("trash"), t("product.remove_picture"));
    removePic.addEventListener("click", busy(removePic, async () => {
      await api("DELETE", `/api/platform/products/${productId}/image`);
      state.product = await api("GET", `/api/platform/products/${productId}`);
      ctx.products = null;
      header();
      drawTab();
    }));
    return h("div", { class: "columns" },
      h("section", { class: "card pad-lg col-main" }, h("span", { class: "h3" }, t("product.tab_spec")),
        h("div", { class: "form-grid" },
          field(t("product.code"), input({ value: p.code, dir: "ltr", disabled: true }), t("product.code_fixed")),
          field(t("product.name"), nameIn),
          field(t("product.category"), category),
          field(t("product.kind"), kind, t("product.kind_help")),
          h("div", { class: "span" }, field(t("product.pattern"), pattern,
            p.devices > 0 ? t("product.pattern_fixed") : t("products.pattern_help"))),
          h("div", { class: "span" }, field(t("product.description"), description)),
          h("div", { class: "span" }, catalog.el, h("span", { class: "help" }, t("product.catalog_help")))),
        h("div", { class: "row end" }, save)),
      h("aside", { class: "col-side" },
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("product.picture_title")),
          h("div", { class: "row" }, h("button", { class: "pic-btn", type: "button", disabled: !manage,
            onclick: pickImage, "aria-label": t("product.change_picture") }, picture(p, "lg"))),
          h("p", { class: "small muted" }, t("product.picture_help")),
          h("div", { class: "row wrap" }, h("button", { class: "btn sm", disabled: !manage, onclick: pickImage },
            icon("upload"), t("product.upload_picture")), removePic)),
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("product.facts")),
          h("div", { class: "kv" }, h("span", {}, t("product.created")), h("span", {}, date(p.created_at))),
          h("div", { class: "kv" }, h("span", {}, t("product.updated")), h("span", {}, date(p.updated_at, true))),
          h("div", { class: "kv" }, h("span", {}, t("stage.registered")), h("span", {}, num(p.devices))),
          h("div", { class: "kv" }, h("span", {}, t("product.published")),
            h("span", {}, state.latest ? t("product.version_n", { n: num(state.latest) }) : "—")))));
  }

  // --- inputs and outputs -------------------------------------------------------------------

  function io() {
    const tags = sorted();
    const tableBody = h("tbody");
    const side = h("aside", { class: "col-side" });
    const drawRows = () => tableBody.replaceChildren(...tags.map((tag) => {
      const rw = tag.access === "read_write";
      const range = [tag.unit, tag.min !== undefined && tag.min !== null && tag.max !== undefined &&
        tag.max !== null ? t("product.range", { min: num(tag.min), max: num(tag.max) }) : null,
      tag.states ? Object.values(tag.states).join(" · ") : null].filter(Boolean).join(" · ") || "—";
      return h("tr", { class: `click ${state.sel === tag.key ? "sel" : ""}`, onclick: () => {
        state.sel = tag.key;
        drawRows();
        drawSide();
      } },
      h("td", { class: "key ltr" }, tag.key),
      h("td", {}, name(tag.display_name) || "—"),
      h("td", {}, h("span", { class: `chip sm ${KIND_CHIP[tag.kind]}` }, t(`kind.${tag.kind}`))),
      h("td", { class: "sub ltr" }, tag.data_type),
      h("td", { class: "sub" }, range),
      h("td", {}, h("span", { class: `chip sm ${rw ? "orange" : "muted"}` },
        rw ? t("access.read_write") : t("access.read"))),
      h("td", {}, h("span", { class: `mark ${tag.ui.chart !== false ? "on" : ""}` }, tag.ui.chart !== false ? "●" : "○")),
      h("td", {}, h("span", { class: `mark ${tag.ui.scenario !== false ? "on" : ""}` }, tag.ui.scenario !== false ? "●" : "○")));
    }));
    const drawSide = () => side.replaceChildren(editor(), mapping(), preview());

    function refresh() {
      tags.splice(0, tags.length, ...sorted());
      drawRows();
      drawSide();
    }

    function editor() {
      const tag = state.def.tags.find((x) => x.key === state.sel);
      if (!tag) return h("section", { class: "card pad" }, h("p", { class: "small muted" }, t("io.pick")));
      const locked = state.publishedKeys.has(tag.key);
      const key = input({ value: tag.key, dir: "ltr", class: "input key", disabled: !manage || locked });
      const fa = input({ value: tag.display_name.fa || "", disabled: !manage });
      const en = input({ value: tag.display_name.en || "", dir: "ltr", disabled: !manage });
      const kind = select(KINDS.map((k) => [k, t(`kind.${k}`)]), { value: tag.kind, disabled: !manage });
      const type = select(typesFor(tag.kind).map((x) => [x, x]), { value: tag.data_type, disabled: !manage });
      const unit = input({ value: tag.unit || "", dir: "ltr", disabled: !manage });
      const min = input({ type: "number", step: "any", value: tag.min ?? "", dir: "ltr", disabled: !manage });
      const max = input({ type: "number", step: "any", value: tag.max ?? "", dir: "ltr", disabled: !manage });
      const access = select([["read", t("access.read")], ["read_write", t("access.read_write")]],
        { value: tag.access || "read", disabled: !manage || READ_ONLY.has(tag.kind) });
      const states = h("textarea", { class: "input", rows: 2, dir: "auto", disabled: !manage,
        placeholder: t("io.states_example") },
      tag.states ? Object.entries(tag.states).map(([v, l]) => `${v}=${l}`).join("\n") : "");
      const def = input({ value: tag.default ?? "", dir: "ltr", disabled: !manage });
      const order = input({ type: "number", value: tag.ui.order || 0, dir: "ltr", disabled: !manage });
      const visible = checkbox(t("io.visible"), tag.ui.visible !== false);
      const chart = checkbox(t("io.chart"), tag.ui.chart !== false);
      const scenario = checkbox(t("io.scenario"), tag.ui.scenario !== false);
      const description = input({ value: tag.description || "", disabled: !manage });
      const ROLES = ["", "climate_power", "climate_mode", "climate_setpoint", "climate_fan", "climate_temp"];
      const role = select(ROLES.map((r) => [r, t(`io.role_${r || "none"}`)]), { value: tag.ui.role || "", disabled: !manage });
      const uiGroup = input({ value: tag.ui.group || "", maxlength: 40, disabled: !manage, placeholder: t("io.group_hint") });
      // the icon the buyer's app shows: outputs (lamp, fan, ...) and inputs a module carries
      // (a 433 MHz remote, PIR, magnet, smoke detector, IR, a missed call that runs a command)
      const ICONS = ["", "bulb", "chandelier", "lamp", "fan", "snow", "flame", "plug", "tv", "curtain", "door",
        "lock", "water", "pump", "leaf", "bell", "motion", "magnet", "smoke", "remote", "ir", "call", "siren",
        "thermo", "sensor", "bolt"];
      const uiIcon = select(ICONS.map((n) => [n, t(`io.icon_${n || "none"}`)]), { value: tag.ui.icon || "", disabled: !manage });
      const formula = h("textarea", { class: "input mono ltr", rows: 2, disabled: !manage,
        placeholder: "integrate(power)" }, tag.formula || "");
      const formulaBox = h("div", { class: `span ${tag.kind === "calculated" ? "" : "hidden"}` },
        field(t("io.formula"), formula, t("io.formula_help", {
          keys: state.def.tags.filter((x) => x.kind !== "calculated").map((x) => x.key).slice(0, 12).join("، ") })));
      for (const c of [visible, chart, scenario]) c.box.disabled = !manage;
      kind.addEventListener("change", () => {
        const allowed = typesFor(kind.value);
        type.replaceChildren(...allowed.map((x) => h("option", { value: x, selected: x === type.value }, x)));
        if (!allowed.includes(type.value)) type.value = allowed[0];
        access.disabled = READ_ONLY.has(kind.value);
        if (access.disabled) access.value = "read";
        formulaBox.classList.toggle("hidden", kind.value !== "calculated");
      });
      const apply = h("button", { class: "btn primary sm", disabled: !manage }, t("io.apply"));
      apply.addEventListener("click", () => {
        const newKey = key.value.trim();
        if (!/^[a-z0-9_]{1,64}$/.test(newKey)) { toast(t("io.bad_key"), "err"); return; }
        if (newKey !== tag.key && state.def.tags.some((x) => x.key === newKey)) {
          toast(t("io.duplicate_key"), "err"); return;
        }
        if (!fa.value.trim() && !en.value.trim()) { toast(t("io.need_name"), "err"); return; }
        if (kind.value === "calculated" && !formula.value.trim()) { toast(t("io.need_formula"), "err"); return; }
        const parsedStates = {};
        for (const line of states.value.split(/\n|,/)) {
          const m = line.trim().match(/^(-?\d+)\s*=\s*(.+)$/);
          if (m) parsedStates[m[1]] = m[2].trim();
        }
        if (newKey !== tag.key) {
          for (const b of Object.values(state.def.bindings)) {
            b.tags[newKey] = b.tags[tag.key] || {};
            delete b.tags[tag.key];
          }
          tag.key = newKey;
        }
        tag.display_name = { fa: fa.value.trim(), en: en.value.trim() };
        tag.kind = kind.value;
        tag.data_type = type.value;
        tag.unit = unit.value.trim() || null;
        tag.description = description.value.trim() || null;
        tag.formula = kind.value === "calculated" ? formula.value.trim() : null;
        tag.min = min.value === "" ? null : Number(min.value);
        tag.max = max.value === "" ? null : Number(max.value);
        tag.access = access.value;
        if (tag.access !== "read_write") delete tag.write_class;
        tag.states = Object.keys(parsedStates).length ? parsedStates : null;
        const raw = def.value.trim();
        tag.default = raw === "" ? null : tag.data_type === "bool" ? raw === "true" || raw === "1"
          : tag.data_type === "string" ? raw : Number(raw);
        tag.ui = { ...tag.ui, order: Number(order.value) || 0, visible: visible.box.checked,
          chart: chart.box.checked, scenario: scenario.box.checked,
          role: role.value || null, group: uiGroup.value.trim() || null, icon: uiIcon.value || null };
        state.sel = tag.key;
        changed();
        refresh();
      });
      const remove = h("button", { class: "btn sm danger", disabled: !manage }, icon("trash"), t("common.delete"));
      remove.addEventListener("click", async () => {
        if (!await confirmAction(t(locked ? "io.delete_published" : "io.delete_confirm", { key: tag.key }),
          t("common.delete"), true)) return;
        state.def.tags = state.def.tags.filter((x) => x !== tag);
        for (const b of Object.values(state.def.bindings)) delete b.tags[tag.key];
        state.sel = state.def.tags[0]?.key || null;
        changed();
        refresh();
      });
      return h("section", { class: "card pad" },
        h("div", { class: "row between" }, h("span", { class: "h3" }, t("io.edit")),
          h("span", { class: "small accent-text mono ltr" }, tag.key)),
        h("div", { class: "form-grid" },
          field(t("io.key"), key, locked ? t("io.key_locked") : t("io.key_help")),
          field(t("io.kind"), kind),
          field(t("io.name_fa"), fa), field(t("io.name_en"), en),
          field(t("io.type"), type), field(t("io.unit"), unit),
          field(t("io.min"), min), field(t("io.max"), max),
          field(t("io.access"), access), field(t("io.order"), order),
          h("div", { class: "span" }, field(t("io.states"), states, t("io.states_help"))),
          formulaBox,
          field(t("io.default"), def), field(t("io.description"), description),
          field(t("io.role"), role, t("io.role_help")), field(t("io.group"), uiGroup),
          field(t("io.icon"), uiIcon, t("io.icon_help")),
          h("div", { class: "span row wrap" }, visible.el, chart.el, scenario.el)),
        h("div", { class: "row between" }, remove, apply));
    }

    function mapping() {
      const tag = state.def.tags.find((x) => x.key === state.sel);
      const bound = Object.keys(state.def.bindings);
      const all = Object.keys(schema).length ? Object.keys(schema) : bound;
      return h("section", { class: "card pad" },
        h("div", { class: "row between" }, h("span", { class: "h3" }, t("io.mapping")),
          tag ? h("span", { class: "small accent-text mono ltr" }, tag.key) : null),
        h("div", { class: "row wrap" }, all.map((proto) => bound.includes(proto)
          ? h("span", { class: "chip teal ltr" }, `✓ ${t(`protocol.${proto}`)}`)
          : h("button", { class: "chip dashed ltr", type: "button", disabled: !manage, onclick: () => {
            state.def.bindings[proto] = { connection: {}, device: {}, tags: Object.fromEntries(
              state.def.tags.map((x) => [x.key, {}])) };
            changed();
            drawSide();
          } }, `+ ${t(`protocol.${proto}`)}`))),
        tag?.kind === "calculated" ? h("div", { class: "note" }, t("io.calculated_note")) : null,
        tag && tag.kind !== "calculated" ? h("div", { class: "stack" }, bound.map((proto) => {
          const fields = (schema[proto]?.tag || []).filter((f) => f.required);
          const entry = state.def.bindings[proto].tags[tag.key] || {};
          if (!fields.length) {
            return h("div", { class: "kv" }, h("span", {}, t(`protocol.${proto}`)),
              h("span", { class: "ltr mono" }, proto === "native" ? t("io.native_key", { key: tag.key })
                : Object.entries(entry).map(([k, v]) => `${k}=${v}`).join(" · ") || "—"));
          }
          return h("div", { class: "stack tight" }, h("span", { class: "small muted" }, t(`protocol.${proto}`)),
            h("div", { class: "form-grid" }, fields.map((f) => schemaInput(f, entry, () => changed()))));
        })) : null,
        h("span", { class: "help" }, t("io.mapping_help")));
    }

    function preview() {
      const outs = state.def.tags.filter((x) => x.access === "read_write" && x.data_type === "bool" &&
        x.ui.visible !== false);
      const readings = state.def.tags.filter((x) => x.access !== "read_write" && x.ui.visible !== false &&
        x.data_type !== "bool");
      const onCount = outs.filter((x) => x.default === true).length;
      return h("section", { class: "card pad" },
        h("div", { class: "row between" }, h("span", { class: "h3" }, t("io.preview")),
          h("span", { class: "tiny muted" }, t("io.preview_auto"))),
        h("div", { class: "phone" },
          outs.length ? [h("div", { class: "row between small" }, h("span", { class: "strong" }, t("io.outputs")),
            h("span", { class: "accent-text" }, t("io.on_count", { n: num(onCount), total: num(outs.length) }))),
          h("div", { class: "outs" }, outs.slice(0, 16).map((x) => h("span", {
            class: `out ${x.default === true ? "on" : ""}` }, name(x.display_name))))] : null,
          readings.slice(0, 6).map((x) => h("div", { class: "kv" }, h("span", {}, name(x.display_name)),
            h("span", {}, x.default !== undefined && x.default !== null ? num(x.default) : "—", " ",
              x.unit || ""))),
          !outs.length && !readings.length ? h("p", { class: "small muted" }, t("io.preview_empty")) : null));
    }

    const bulk = h("button", { class: "btn sm", disabled: !manage }, t("io.bulk"));
    bulk.addEventListener("click", () => bulkAdd(refresh));
    const add = h("button", { class: "btn sm soft", disabled: !manage }, `+ ${t("io.add")}`);
    add.addEventListener("click", () => {
      let n = state.def.tags.length + 1;
      while (state.def.tags.some((x) => x.key === `param_${n}`)) n += 1;
      addTag({ key: `param_${n}`, kind: "parameter", data_type: "float32", access: "read",
        display_name: { fa: `پارامتر ${faNum(n)}`, en: `Parameter ${n}` }, ui: {} });
      refresh();
    });
    drawRows();
    drawSide();
    return h("div", { class: "columns" },
      h("section", { class: "card col-main" },
        h("div", { class: "bar" }, h("span", { class: "h3" }, t("io.title"), " ",
          h("span", { class: "small muted" }, t("io.count", { n: num(state.def.tags.length) }))),
        h("div", { class: "row wrap" }, bulk, add)),
        state.def.tags.length ? h("div", { class: "scroll" }, h("table", { class: "grid wide" },
          h("thead", {}, h("tr", {}, ["key", "name", "kind", "type", "range", "access", "chart", "scenario"]
            .map((c) => h("th", {}, t(`io.col_${c}`))))), tableBody))
          : h("div", { class: "empty" }, h("p", {}, t("io.empty"))),
        h("div", { class: "foot-note" }, t("io.footnote"))),
      side);
  }

  function addTag(tag) {
    tag.ui = { order: Math.max(0, ...state.def.tags.map((x) => x.ui.order || 0)) + 1, ...tag.ui };
    state.def.tags.push(tag);
    for (const b of Object.values(state.def.bindings)) b.tags[tag.key] = b.tags[tag.key] || {};
    state.sel = tag.key;
    changed();
  }

  async function bulkAdd(refresh) {
    const base = input({ value: "relay", dir: "ltr", class: "input key" });
    const count = input({ type: "number", value: 8, min: 1, max: 256, dir: "ltr" });
    const start = input({ type: "number", value: 1, min: 0, dir: "ltr" });
    const kind = select(KINDS.map((k) => [k, t(`kind.${k}`)]), { value: "digital_output" });
    const fa = input({ value: "رله" });
    const en = input({ value: "Relay", dir: "ltr" });
    const controllable = checkbox(t("io.controllable"), true);
    const answer = await dialog(t("io.bulk_title"), h("div", { class: "form-grid" },
      field(t("io.bulk_base"), base, t("io.bulk_base_help")), field(t("io.kind"), kind),
      field(t("io.bulk_count"), count), field(t("io.bulk_start"), start),
      field(t("io.name_fa"), fa), field(t("io.name_en"), en),
      h("div", { class: "span" }, controllable.el)),
    [["cancel", t("common.cancel")], ["ok", t("io.bulk_add"), "primary"]]);
    if (answer !== "ok") return;
    const n = Math.max(1, Math.min(256, Number(count.value) || 1));
    const from = Math.max(0, Number(start.value) || 0);
    const prefix = base.value.trim().toLowerCase().replace(/[^a-z0-9_]/g, "_") || "tag";
    let added = 0;
    for (let i = from; i < from + n; i += 1) {
      const key = `${prefix}_${i}`;
      if (state.def.tags.some((x) => x.key === key)) continue;
      const k = kind.value;
      const rw = controllable.box.checked && !READ_ONLY.has(k);
      const type = typesFor(k)[0] === "bool" ? "bool" : "float32";
      const tag = { key, kind: k, data_type: type, access: rw ? "read_write" : "read",
        display_name: { fa: `${fa.value.trim()} ${faNum(i)}`.trim(), en: `${en.value.trim()} ${i}`.trim() },
        ui: {} };
      if (rw && type !== "bool") { tag.min = 0; tag.max = 100; }
      addTag(tag);
      added += 1;
    }
    toast(t("io.bulk_done", { n: num(added) }), "ok");
    refresh();
  }

  // --- commands -----------------------------------------------------------------------------

  function commands() {
    const writable = sorted().filter((x) => !READ_ONLY.has(x.kind));
    const native = state.def.bindings.native;
    const timeout = input({ type: "number", min: 1, max: 3600, dir: "ltr", disabled: !manage || !native,
      value: native?.connection?.command_timeout_s ?? "" , placeholder: "30" });
    timeout.addEventListener("change", () => {
      if (!native) return;
      native.connection.command_timeout_s = timeout.value === "" ? null : Number(timeout.value);
      changed();
    });
    const rows = writable.map((tag) => {
      const rw = checkbox("", tag.access === "read_write", () => {
        tag.access = rw.box.checked ? "read_write" : "read";
        if (rw.box.checked && tag.data_type !== "bool" && tag.data_type !== "string" &&
            (tag.min === undefined || tag.min === null)) { tag.min = 0; tag.max = 100; }
        changed();
        tabBox.replaceChildren(commands());
      });
      rw.box.disabled = !manage;
      rw.box.setAttribute("aria-label", t("commands.controllable_x", { x: tag.key }));
      const cls = select([["", t("commands.class_auto")], ["setpoint", t("commands.class_setpoint")],
        ["command", t("commands.class_command")]], { value: tag.write_class || "",
        disabled: !manage || tag.access !== "read_write" });
      cls.setAttribute("aria-label", t("commands.class"));
      cls.addEventListener("change", () => { tag.write_class = cls.value || null; changed(); });
      return h("tr", {},
        h("td", { class: "key ltr" }, tag.key),
        h("td", {}, name(tag.display_name)),
        h("td", {}, h("span", { class: `chip sm ${KIND_CHIP[tag.kind]}` }, t(`kind.${tag.kind}`))),
        h("td", {}, rw.el),
        h("td", {}, cls),
        h("td", { class: "sub" }, tag.data_type === "bool" ? t("commands.on_off")
          : tag.min !== undefined && tag.min !== null ? t("product.range", { min: num(tag.min), max: num(tag.max) })
            : "—"));
    });
    return h("div", { class: "columns" },
      h("section", { class: "card col-main" },
        h("div", { class: "bar" }, h("span", { class: "h3" }, t("commands.title")),
          h("span", { class: "small muted" }, t("commands.count", { n: num(writable.filter((x) => x.access === "read_write").length) }))),
        writable.length ? h("div", { class: "scroll" }, h("table", { class: "grid mid" },
          h("thead", {}, h("tr", {}, ["key", "name", "kind", "controllable", "class", "range"]
            .map((c) => h("th", {}, t(`commands.col_${c}`))))), h("tbody", {}, rows)))
          : h("div", { class: "empty" }, h("p", {}, t("commands.empty"))),
        h("div", { class: "foot-note" }, t("commands.footnote"))),
      h("aside", { class: "col-side" },
        composites(),
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("commands.timeout_title")),
          field(t("commands.timeout"), timeout, native ? t("commands.timeout_help") : t("commands.timeout_native"))),
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("commands.lifecycle")),
          ["created", "sent", "delivered", "executed", "confirmed", "failed", "expired"].map((s) =>
            h("div", { class: "kv" }, h("span", { class: "ltr mono" }, s), h("span", {}, t(`command.${s}`)))))));
  }

  /** Commands that set several parameters at once ("evening mode": relay 1 on, relay 8 off). */
  function composites() {
    state.def.commands = state.def.commands || [];
    const list = state.def.commands;
    const controllable = sorted().filter((x) => x.access === "read_write");
    const rows = list.map((c, i) => {
      const edit = h("button", { class: "icon-btn", "aria-label": t("composite.edit"), disabled: !manage,
        onclick: () => editComposite(i) }, icon("settings"));
      const remove = h("button", { class: "icon-btn", "aria-label": t("common.delete"), disabled: !manage,
        onclick: () => { list.splice(i, 1); changed(); tabBox.replaceChildren(commands()); } }, icon("trash"));
      return h("div", { class: "kv" }, h("span", { class: "stack tight" }, h("span", { class: "strong" }, name(c.display_name)),
        h("span", { class: "tiny muted ltr" }, c.params.map((p) => `${p.tag}=${p.value ?? "?"}`).join(" · "))),
      h("span", { class: "row" }, edit, remove));
    });
    async function editComposite(index) {
      const current = index === null ? { key: "", display_name: { fa: "", en: "" }, params: [] } : clone(list[index]);
      const key = input({ value: current.key, dir: "ltr", class: "input key", placeholder: "evening_mode" });
      const fa = input({ value: current.display_name.fa || "" });
      const en = input({ value: current.display_name.en || "", dir: "ltr" });
      const box = h("div", { class: "stack" });
      const paramRows = [];
      const addParam = (prm = null) => {
        const tag = select(controllable.map((x) => [x.key, `${name(x.display_name)} (${x.key})`]), { value: prm?.tag });
        const value = input({ dir: "ltr", value: prm?.value ?? "", placeholder: t("composite.value_hint") });
        const entry = { tag, value };
        const row = h("div", { class: "form-grid" }, field(t("composite.param"), tag), field(t("composite.value"), value),
          h("button", { type: "button", class: "btn sm danger", onclick: () => { paramRows.splice(paramRows.indexOf(entry), 1); row.remove(); } },
            icon("trash"), t("common.delete")));
        paramRows.push(entry);
        box.append(row);
      };
      (current.params.length ? current.params : [null]).forEach(addParam);
      const more = h("button", { type: "button", class: "btn sm", onclick: () => addParam() }, icon("plus"), t("composite.add_param"));
      const answer = await dialog(index === null ? t("composite.new") : t("composite.edit"), h("div", { class: "stack" },
        h("div", { class: "form-grid" }, field(t("composite.key"), key), field(t("io.name_fa"), fa), field(t("io.name_en"), en)),
        h("span", { class: "label" }, t("composite.params")), box, more, h("span", { class: "help" }, t("composite.help"))),
      [["cancel", t("common.cancel")], ["ok", t("common.save"), "primary"]]);
      if (answer !== "ok") return;
      const k = key.value.trim();
      if (!/^[a-z0-9_]{1,64}$/.test(k)) { toast(t("io.bad_key"), "err"); return; }
      const params = paramRows.map(({ tag, value }) => {
        const def = controllable.find((x) => x.key === tag.value);
        const raw = value.value.trim();
        const parsed = raw === "" ? null : def?.data_type === "bool" ? ["1", "true", "on"].includes(raw.toLowerCase())
          : Number(raw);
        return { tag: tag.value, value: parsed };
      }).filter((p) => p.tag);
      if (!params.length) { toast(t("composite.need_param"), "err"); return; }
      const out = { key: k, display_name: { fa: fa.value.trim() || k, ...(en.value.trim() ? { en: en.value.trim() } : {}) }, params };
      if (index === null) list.push(out); else list[index] = out;
      changed();
      tabBox.replaceChildren(commands());
    }
    const add = h("button", { class: "btn sm soft", disabled: !manage || !controllable.length }, `+ ${t("composite.new")}`);
    add.addEventListener("click", () => editComposite(null));
    return h("section", { class: "card pad" }, h("div", { class: "row between" }, h("span", { class: "h3" }, t("composite.title")), add),
      h("span", { class: "help" }, t("composite.intro")),
      rows.length ? rows : h("p", { class: "small muted" }, t("composite.none")));
  }

  // --- protocols ----------------------------------------------------------------------------

  function protocolsTab() {
    const bound = Object.keys(state.def.bindings);
    const free = Object.keys(schema).filter((p) => !bound.includes(p));
    const pick = select(free.map((p) => [p, t(`protocol.${p}`)]), { disabled: !manage || !free.length });
    pick.setAttribute("aria-label", t("protocols.add"));
    const add = h("button", { class: "btn soft sm", disabled: !manage || !free.length }, `+ ${t("protocols.add")}`);
    add.addEventListener("click", () => {
      state.def.bindings[pick.value] = { connection: {}, device: {}, tags: Object.fromEntries(
        state.def.tags.map((x) => [x.key, {}])) };
      changed();
      tabBox.replaceChildren(protocolsTab());
    });
    return h("div", { class: "stack loose" },
      h("div", { class: "row wrap between" }, h("p", { class: "small muted" }, t("protocols.intro")),
        h("div", { class: "row" }, pick, add)),
      bound.map((proto) => bindingCard(proto)));
  }

  function bindingCard(proto) {
    const b = state.def.bindings[proto];
    const conn = schema[proto]?.connection || [];
    const tagFields = schema[proto]?.tag || [];
    let showAll = false;
    const table = h("div", { class: "scroll" });
    const drawTable = () => {
      const fields = tagFields.filter((f) => showAll || f.required);
      if (!fields.length) {
        table.replaceChildren(h("p", { class: "small muted pad-x" }, proto === "native"
          ? t("protocols.native_tags") : t("protocols.no_tag_fields")));
        return;
      }
      table.replaceChildren(h("table", { class: "grid mid" },
        h("thead", {}, h("tr", {}, h("th", {}, t("io.col_key")), fields.map((f) => h("th", { class: "ltr" }, f.name)))),
        h("tbody", {}, sorted().filter((x) => x.kind !== "calculated").map((tag) => {
          const entry = b.tags[tag.key] = b.tags[tag.key] || {};
          return h("tr", {}, h("td", { class: "key ltr" }, tag.key),
            fields.map((f) => h("td", {}, schemaControl(f, entry, () => changed(), true))));
        }))));
    };
    drawTable();
    const toggle = h("button", { class: "btn sm", disabled: !tagFields.some((f) => !f.required) },
      t("protocols.all_fields"));
    toggle.addEventListener("click", () => { showAll = !showAll; toggle.textContent = t(showAll ? "protocols.required_fields" : "protocols.all_fields"); drawTable(); });
    const remove = h("button", { class: "btn sm danger", disabled: !manage }, icon("trash"), t("protocols.remove"));
    remove.addEventListener("click", async () => {
      if (!await confirmAction(t("protocols.remove_confirm", { p: t(`protocol.${proto}`) }), t("common.delete"), true)) return;
      delete state.def.bindings[proto];
      changed();
      tabBox.replaceChildren(protocolsTab());
    });
    const deviceOpts = h("textarea", { class: "input ltr mono", rows: 2, disabled: !manage },
      Object.keys(b.device || {}).length ? JSON.stringify(b.device) : "");
    deviceOpts.addEventListener("change", () => {
      try { b.device = deviceOpts.value.trim() ? JSON.parse(deviceOpts.value) : {}; changed(); }
      catch { toast(t("protocols.bad_json"), "err"); }
    });
    return h("section", { class: "card" },
      h("div", { class: "bar" }, h("span", { class: "h3 ltr" }, t(`protocol.${proto}`)), remove),
      h("div", { class: "card pad", "aria-label": t("protocols.connection") },
        h("span", { class: "small strong" }, t("protocols.connection")),
        h("span", { class: "help" }, t("protocols.connection_help")),
        conn.length ? h("div", { class: "form-grid" }, conn.map((f) => schemaInput(f, b.connection, () => changed())))
          : h("p", { class: "small muted" }, t("protocols.no_connection_fields")),
        field(t("protocols.device_options"), deviceOpts, t("protocols.device_options_help"))),
      h("div", { class: "bar" }, h("span", { class: "small strong" }, t("protocols.addresses")), toggle),
      table);
  }

  function schemaInput(f, target, onChange) {
    const control = schemaControl(f, target, onChange, false);
    return field(`${f.name}${f.required ? " *" : ""}`, control, f.help || null);
  }

  function schemaControl(f, target, onChange, compact) {
    const cls = compact ? "input sm" : "input";
    const current = target[f.name];
    let el;
    if (f.type === "enum" || f.type === "boolean") {
      const options = f.type === "boolean" ? [true, false] : f.enum;
      el = h("select", { class: cls, disabled: !manage },
        h("option", { value: "" }, f.default !== undefined && f.default !== null ? `(${String(f.default)})` : "—"),
        options.map((o) => h("option", { value: JSON.stringify(o), selected: current !== undefined &&
          JSON.stringify(current) === JSON.stringify(o) }, String(o))));
      el.addEventListener("change", () => {
        if (el.value === "") delete target[f.name];
        else target[f.name] = JSON.parse(el.value);
        onChange();
      });
    } else if (f.type === "json") {
      el = h("input", { class: `${cls} mono`, dir: "ltr", disabled: !manage, placeholder: "JSON",
        value: current === undefined ? "" : JSON.stringify(current) });
      el.addEventListener("change", () => {
        try {
          if (el.value.trim() === "") delete target[f.name];
          else target[f.name] = JSON.parse(el.value);
          onChange();
        } catch { toast(t("protocols.bad_json"), "err"); }
      });
    } else {
      const numeric = f.type === "integer" || f.type === "number";
      el = h("input", { class: cls, dir: "ltr", disabled: !manage, type: numeric ? "number" : "text",
        step: f.type === "integer" ? "1" : "any", min: f.minimum, max: f.maximum,
        placeholder: f.default !== undefined && f.default !== null ? String(f.default) : "",
        value: current === undefined || current === null ? "" : String(current) });
      el.addEventListener("change", () => {
        if (el.value === "") delete target[f.name];
        else target[f.name] = numeric ? Number(el.value) : el.value;
        onChange();
      });
    }
    el.setAttribute("aria-label", f.name);
    return el;
  }

  // --- versions and firmware ----------------------------------------------------------------

  async function versions() {
    const notes = h("textarea", { class: "input", rows: 2, disabled: !manage }, state.notes);
    notes.addEventListener("change", () => { state.notes = notes.value; changed(); });
    const rows = state.versions.map((v) => {
      const actions = [];
      if (v.status === "published") {
        const open = h("button", { class: "btn sm", disabled: !manage }, t("versions.open"));
        open.addEventListener("click", async () => {
          if (state.dirty && !await confirmAction(t("versions.discard"), t("common.continue"))) return;
          state.def = normalize(clone(v.definition));
          state.sel = state.def.tags[0]?.key || null;
          changed();
          state.tab = "io";
          header();
          drawTab();
        });
        actions.push(open);
        if (manage && state.versions.some((x) => x.status === "published" && x.version !== v.version && x.devices)) {
          const up = h("button", { class: "btn sm soft" }, t("versions.upgrade_to"));
          up.addEventListener("click", busy(up, () => upgrade(v.version)));
          actions.push(up);
        }
      }
      return h("tr", {},
        h("td", { class: "strong" }, t("product.version_n", { n: num(v.version) })),
        h("td", {}, h("span", { class: `chip sm ${v.status === "published" ? "teal" : "violet"}` },
          t(`versions.status_${v.status}`))),
        h("td", {}, num((v.definition.tags || []).length)),
        h("td", {}, num(v.devices)),
        h("td", { class: "sub" }, date(v.published_at || v.updated_at, true)),
        h("td", { class: "sub small" }, v.notes || "—"),
        h("td", {}, h("div", { class: "row" }, actions)));
    });
    return h("div", { class: "stack loose" },
      h("div", { class: "columns" },
        h("section", { class: "card col-main" },
          h("div", { class: "bar" }, h("span", { class: "h3" }, t("versions.title"))),
          rows.length ? h("div", { class: "scroll" }, h("table", { class: "grid mid" },
            h("thead", {}, h("tr", {}, ["version", "status", "tags", "devices", "date", "notes", "actions"]
              .map((c) => h("th", {}, t(`versions.col_${c}`))))), h("tbody", {}, rows)))
            : h("div", { class: "empty" }, h("p", {}, t("versions.none"))),
          h("div", { class: "foot-note" }, t("versions.footnote"))),
        h("aside", { class: "col-side" }, h("section", { class: "card pad" },
          h("span", { class: "h3" }, t("versions.notes_title")),
          field(t("versions.notes"), notes, t("versions.notes_help"))))),
      manage ? await firmware() : null);
  }

  async function upgrade(to) {
    const preview = await api("GET", `/api/platform/products/${productId}/versions/${to}/upgrade-preview`);
    if (!preview.devices) { toast(t("versions.nothing_to_upgrade"), "ok"); return; }
    const allow = checkbox(t("versions.allow_breaking"), false);
    const box = h("div", { class: "stack" },
      h("p", { class: "small sub" }, t("versions.preview_line", { n: num(preview.devices), v: num(to) })),
      preview.groups.map((g) => h("div", { class: "note" },
        h("span", { class: "strong" }, t("versions.group", { from: num(g.from_version), p: t(`protocol.${g.protocol}`), n: num(g.devices) })),
        h("span", {}, t("versions.diff", { a: num(g.added.length), r: num(g.removed.length), c: num(g.changed.length) })),
        g.breaking_changes.length ? h("span", { class: "orange-text ltr" }, g.breaking_changes.join(" · ")) : null)),
      preview.automations_affected.length ? h("div", { class: "note warn" },
        t("versions.automations", { n: num(preview.automations_affected.length) })) : null,
      preview.breaking ? allow.el : null);
    const answer = await dialog(t("versions.upgrade_title", { v: num(to) }), box,
      [["cancel", t("common.cancel")], ["ok", t("versions.upgrade"), "primary"]]);
    if (answer !== "ok") return;
    const r = await api("POST", `/api/platform/products/${productId}/versions/${to}/upgrade`,
      { allow_breaking: allow.box.checked });
    toast(t("versions.upgraded", { n: num(r.moved?.length ?? r.moved ?? 0) }),
      r.failed?.length ? "err" : "ok");
    await load();
    header();
    drawTab();
  }

  async function firmware() {
    const releases = await api("GET", `/api/platform/products/${productId}/firmware`);
    const file = h("input", { type: "file", class: "input" });
    const version = input({ dir: "ltr", placeholder: "1.4.2" });
    const notes = input({});
    const minVersion = input({ type: "number", min: 1, dir: "ltr" });
    const upload = h("button", { class: "btn primary sm" }, icon("upload"), t("firmware.upload"));
    upload.addEventListener("click", busy(upload, async () => {
      const f = file.files[0];
      if (!f || !version.value.trim()) { toast(t("firmware.need_file"), "err"); return; }
      const buf = await f.arrayBuffer();
      const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", buf))]
        .map((b) => b.toString(16).padStart(2, "0")).join("");
      const q = new URLSearchParams({ version: version.value.trim(), sha256: digest });
      if (notes.value.trim()) q.set("notes", notes.value.trim());
      if (minVersion.value) q.set("min_template_version", minVersion.value);
      await api("POST", `/api/platform/products/${productId}/firmware?${q}`, undefined, { raw: buf });
      toast(t("firmware.uploaded"), "ok");
      drawTab();
    }));
    const rows = releases.map((r) => {
      const roll = h("button", { class: "btn sm soft", disabled: r.status !== "released" }, t("firmware.rollout"));
      roll.addEventListener("click", busy(roll, async () => {
        if (!await confirmAction(t("firmware.rollout_confirm", { v: r.version }), t("firmware.rollout"))) return;
        const out = await api("POST", `/api/platform/firmware/${r.release_id}/rollout`, {});
        toast(t("firmware.rolled_out", { n: num(out.jobs) }), "ok");
      }));
      const jobs = h("button", { class: "btn sm" }, t("firmware.jobs"));
      jobs.addEventListener("click", busy(jobs, async () => {
        const list = await api("GET", `/api/platform/firmware/${r.release_id}/jobs`);
        const counts = {};
        for (const j of list) counts[j.status] = (counts[j.status] || 0) + 1;
        await dialog(t("firmware.jobs_title", { v: r.version }), h("div", { class: "stack" },
          list.length ? Object.entries(counts).map(([s, n]) => h("div", { class: "kv" },
            h("span", {}, t(`job.${s}`)), h("b", {}, num(n)))) : h("p", { class: "muted small" }, t("firmware.no_jobs"))));
      }));
      const withdraw = h("button", { class: "btn sm danger", disabled: r.status !== "released" }, t("firmware.withdraw"));
      withdraw.addEventListener("click", busy(withdraw, async () => {
        if (!await confirmAction(t("firmware.withdraw_confirm", { v: r.version }), t("firmware.withdraw"), true)) return;
        await api("POST", `/api/platform/firmware/${r.release_id}/withdraw`);
        drawTab();
      }));
      return h("tr", {},
        h("td", { class: "mono ltr strong" }, r.version),
        h("td", {}, h("span", { class: `chip sm ${r.status === "released" ? "teal" : "orange"}` }, t(`firmware.status_${r.status}`))),
        h("td", { class: "sub" }, t("firmware.kb", { n: num(Math.round(r.size_bytes / 1024)) })),
        h("td", { class: "mono-sub ltr" }, `${r.sha256.slice(0, 12)}…`),
        h("td", { class: "sub" }, date(r.created_at, true)),
        h("td", { class: "sub small" }, r.notes || "—"),
        h("td", {}, h("div", { class: "row" }, roll, jobs, withdraw)));
    });
    return h("div", { class: "columns" },
      h("section", { class: "card col-main" },
        h("div", { class: "bar" }, h("span", { class: "h3" }, t("firmware.title"))),
        rows.length ? h("div", { class: "scroll" }, h("table", { class: "grid wide" },
          h("thead", {}, h("tr", {}, ["version", "status", "size", "sha", "date", "notes", "actions"]
            .map((c) => h("th", {}, t(`firmware.col_${c}`))))), h("tbody", {}, rows)))
          : h("div", { class: "empty" }, h("p", {}, t("firmware.none"))),
        h("div", { class: "foot-note" }, t("firmware.footnote"))),
      h("aside", { class: "col-side" }, h("section", { class: "card pad" },
        h("span", { class: "h3" }, t("firmware.new")),
        field(t("firmware.file"), file), field(t("firmware.version"), version),
        field(t("firmware.notes"), notes), field(t("firmware.min_template"), minVersion, t("firmware.min_template_help")),
        upload)));
  }

  await load();
  header();
  drawTab();
  body.append(headBox, tabBox);
  return body;
}
