// Devices and production: register a batch (range or CSV), the devices of a product, labels.

import { api, busy, confirmAction, download, h, icon, num, parseCsv, t, toast } from "../core.js";
import { can, checkbox, connection, ctx, field, identifierAt, input, lifecycleChip, ownership,
  pageHead, pager, patternWidth, products, select, tile, warrantyText } from "../ui.js";
import { issueLabels, labelCard } from "./labels.js";

const FILTERS = [["", "all"], ["stock", "ready"], ["sold", "sold"], ["active", "active"],
  ["disabled", "disabled"]];

export default async function production(args) {
  const list = (await products(true)).filter((p) => p.org_id === ctx.org.org_id);
  if (!list.length) {
    return h("div", { class: "stack loose" }, pageHead(t("menu.production"), ""),
      h("div", { class: "card empty" }, h("p", {}, t("dashboard.no_products")),
        h("a", { class: "btn primary", href: "#/products/new" }, t("dashboard.new_product"))));
  }
  const product = list.find((p) => p.product_id === args[0]) ||
    list.find((p) => p.latest_published) || list[0];
  const register = can("device.register");
  const versions = (await api("GET", `/api/platform/products/${product.product_id}/versions`))
    .filter((v) => v.status === "published");
  const stats = await api("GET", `/api/platform/stats?product=${product.product_id}`);
  const life = stats.lifecycle;

  const choose = select(list.map((p) => [p.product_id, `${p.name} · ${p.code}`]),
    { value: product.product_id, "aria-label": t("production.product") });
  choose.addEventListener("change", () => { location.hash = `#/production/${choose.value}`; });

  const csvInput = h("input", { type: "file", accept: ".csv,text/csv", class: "hidden" });
  const importBtn = h("button", { class: "btn", disabled: !register || !versions.length },
    icon("upload"), t("production.import_csv"));
  importBtn.addEventListener("click", () => csvInput.click());
  csvInput.addEventListener("change", busy(importBtn, async () => {
    const f = csvInput.files[0];
    if (!f) return;
    const text = await f.text();
    const rows = parseCsv(text.replace(/^﻿/, ""));
    const answer = await confirmAction(t("production.import_confirm", { n: num(rows.length -
      (rows[0]?.[0]?.toLowerCase() === "identifier" ? 1 : 0)), file: f.name }), t("production.import"));
    csvInput.value = "";
    if (!answer) return;
    const r = await api("POST", `/api/platform/products/${product.product_id}/devices`, {
      version: Number(versionSel.value), protocol: protocolSel.value, csv: text.replace(/^﻿/, ""),
      batch: batch.value.trim() || undefined, warranty_months: Number(warranty.value) || undefined,
      enabled: enabled.box.checked });
    toast(t("production.registered", { n: num(r.registered), first: r.first, last: r.last }), "ok");
    location.reload();
  }));
  const exportBtn = h("button", { class: "btn", disabled: !register }, icon("download"),
    t("production.export_labels"));
  exportBtn.addEventListener("click", busy(exportBtn, () => issueLabels(product, { csv: true })));

  const listBtn = h("button", { class: "btn" }, icon("download"), t("production.export_list"));
  listBtn.addEventListener("click", busy(listBtn, async () => {
    const text = await api("GET", `/api/platform/devices.csv?product=${product.product_id}`, undefined, { as: "text" });
    download(`devices-${product.code}.csv`, text.replace(/^﻿/, ""));
  }));

  // --- batch registration ---------------------------------------------------------------------
  const versionSel = select(versions.map((v) => [v.version,
    t("production.version_published", { n: num(v.version) })]), { disabled: !register });
  const protocolSel = select([], { disabled: !register });
  const fillProtocols = () => {
    const v = versions.find((x) => String(x.version) === versionSel.value);
    const bound = Object.keys(v?.definition?.bindings || {});
    protocolSel.replaceChildren(...bound.map((p) => h("option", { value: p }, t(`protocol.${p}`))));
  };
  versionSel.addEventListener("change", fillProtocols);
  fillProtocols();
  const today = new Intl.DateTimeFormat("fa-IR-u-ca-persian-nu-latn", { year: "numeric", month: "2-digit" })
    .format(new Date()).replace(/\//g, "-");
  const batch = input({ value: `${today}-01`, dir: "ltr", disabled: !register });
  const pattern = input({ value: product.identifier_pattern, dir: "ltr", class: "input key", disabled: true });
  const width = patternWidth(product.identifier_pattern);
  const start = input({ type: "number", min: 0, max: 10 ** width - 1, dir: "ltr", disabled: !register,
    value: stats.total + 1 });
  const count = input({ type: "number", min: 1, max: 10000, dir: "ltr", value: 100, disabled: !register });
  const warranty = input({ type: "number", min: 0, max: 240, dir: "ltr", value: 18, disabled: !register });
  const method = select([["claim", t("production.method_claim")]], { disabled: !register });
  const enabled = checkbox(t("production.enabled"), true);
  enabled.box.disabled = !register;
  const previewIds = h("span", { class: "ltr mono" });
  const previewCheck = h("span", {});
  const submit = h("button", { class: "btn primary big", disabled: !register || !versions.length });
  let checkTimer = null;
  const updatePreview = () => {
    const s = Number(start.value) || 0;
    const n = Math.max(1, Number(count.value) || 1);
    const at = (i) => identifierAt(product.identifier_pattern, i);
    const shown = [at(s)];
    if (n >= 2) shown.push(at(s + 1));
    if (n > 3) shown.push("…");
    if (n > 2) shown.push(at(s + n - 1));
    previewIds.textContent = shown.join(" · ");
    submit.textContent = t("production.register_n", { n: num(n) });
    previewCheck.className = "muted";
    previewCheck.textContent = t("production.checking");
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => {
      if (!register) { previewCheck.textContent = ""; return; }
      try {
        const r = await api("POST", `/api/platform/products/${product.product_id}/devices/check`,
          { start: s, count: n });
        previewCheck.className = r.taken.length ? "orange-text" : "teal-text";
        previewCheck.textContent = r.taken.length
          ? t("production.taken", { ids: r.taken.slice(0, 5).join("، ") }) : `✓ ${t("production.no_duplicates")}`;
      } catch (err) {
        previewCheck.className = "orange-text";
        previewCheck.textContent = err.message;
      }
    }, 350);
  };
  start.addEventListener("input", updatePreview);
  count.addEventListener("input", updatePreview);
  updatePreview();
  const result = h("div", { class: "stack" });
  submit.addEventListener("click", busy(submit, async () => {
    const n = Number(count.value);
    const r = await api("POST", `/api/platform/products/${product.product_id}/devices`, {
      version: Number(versionSel.value), protocol: protocolSel.value, start: Number(start.value),
      count: n, batch: batch.value.trim() || undefined,
      warranty_months: warranty.value === "" ? undefined : Number(warranty.value),
      enabled: enabled.box.checked });
    toast(t("production.registered", { n: num(r.registered), first: r.first, last: r.last }), "ok");
    const labels = h("button", { class: "btn soft" }, icon("print"), t("production.print_batch"));
    labels.addEventListener("click", busy(labels, () => issueLabels(product, { batch: batch.value.trim(), print: true })));
    const csv = h("button", { class: "btn" }, icon("download"), t("production.csv_batch"));
    csv.addEventListener("click", busy(csv, () => issueLabels(product, { batch: batch.value.trim(), csv: true,
      credentials: true })));
    result.replaceChildren(h("div", { class: "note ok" },
      t("production.registered", { n: num(r.registered), first: r.first, last: r.last }),
      h("div", { class: "row wrap" }, labels, csv)));
    start.value = Number(start.value) + n;
    updatePreview();
    drawDevices();
  }));
  const form = h("section", { class: "card pad-lg col-form", "aria-label": t("production.batch") },
    h("span", { class: "h2" }, t("production.batch")),
    versions.length ? null : h("div", { class: "note warn" }, t("production.no_version"),
      h("a", { href: `#/product/${product.product_id}/io` }, t("production.go_publish"))),
    h("div", { class: "form-grid" },
      field(t("production.version"), versionSel), field(t("production.series"), batch),
      h("div", { class: "span" }, field(t("product.pattern"), pattern)),
      field(t("production.start"), start), field(t("production.count"), count),
      field(t("production.warranty"), warranty), field(t("production.method"), method),
      field(t("production.protocol"), protocolSel), h("div", { class: "field" }, h("span", { class: "label" }, " "), enabled.el)),
    h("div", { class: "note" }, h("span", { class: "muted" }, t("production.preview")), previewIds, previewCheck),
    h("div", { class: "small muted" }, t("production.codes_note")),
    submit, result);

  function stageCard() {
    const series = input({ dir: "ltr", placeholder: batch.value, disabled: !register });
    const stage = select(["ready", "sold", "registered", "disabled"].map((x) => [x, t(`lifecycle.${x}`)]),
      { value: "sold", disabled: !register });
    const apply = h("button", { class: "btn sm", disabled: !register }, t("production.stage_apply"));
    apply.addEventListener("click", busy(apply, async () => {
      if (!series.value.trim()) throw new Error(t("labels.need_batch"));
      if (!await confirmAction(t("production.stage_confirm", { batch: series.value.trim(),
        stage: t(`lifecycle.${stage.value}`) }), t("production.stage_apply"), stage.value === "disabled")) return;
      const r = await api("POST", `/api/platform/products/${product.product_id}/devices/lifecycle`,
        { batch: series.value.trim(), lifecycle: stage.value });
      toast(t("production.stage_done", { n: num(r.moved) }), "ok");
      drawDevices();
    }));
    return h("section", { class: "card pad", "aria-label": t("production.stage_title") },
      h("span", { class: "h3" }, t("production.stage_title")),
      h("div", { class: "form-grid" }, field(t("production.series"), series), field(t("device.stage"), stage)),
      h("span", { class: "help" }, t("production.stage_help")), h("div", { class: "row end" }, apply));
  }

  // --- devices --------------------------------------------------------------------------------
  let filter = "";
  let offset = 0;
  const limit = 50;
  const search = h("input", { class: "search", type: "search", placeholder: t("production.search"),
    "aria-label": t("production.search") });
  const filters = h("div", { class: "filters" });
  const tbody = h("tbody");
  const pagerBox = h("div");
  async function drawDevices() {
    filters.replaceChildren(...FILTERS.map(([value, key]) => h("button", { class: `filter ${filter === value ? "on" : ""}`,
      onclick: () => { filter = value; offset = 0; drawDevices(); } }, t(`filter.${key}`))));
    const q = new URLSearchParams({ product: product.product_id, limit, offset });
    if (filter) q.set("lifecycle", filter);
    if (search.value.trim()) q.set("q", search.value.trim());
    const page = await api("GET", `/api/platform/devices?${q}`);
    tbody.replaceChildren(...(page.devices.length ? page.devices.map((d) => {
      const [conn, cls] = connection(d);
      return h("tr", { class: "click", onclick: () => { location.hash = `#/device/${d.device_id}`; } },
        h("td", { class: "id ltr" }, d.identifier),
        h("td", { class: "mono-sub ltr" }, d.serial_number || "—"),
        h("td", {}, lifecycleChip(d.lifecycle)),
        h("td", { class: cls }, conn),
        h("td", { class: "sub ltr" }, d.firmware_version || "—"),
        h("td", { class: "sub" }, warrantyText(d)),
        h("td", { class: "muted" }, ownership(d)));
    }) : [h("tr", {}, h("td", { colspan: 7, class: "muted" }, t("production.no_devices")))]));
    pagerBox.replaceChildren(pager(page.total, offset, limit, (o) => { offset = o; drawDevices(); }));
  }
  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { offset = 0; drawDevices(); }, 300);
  });
  await drawDevices();

  const sample = labelCard(product, identifierAt(product.identifier_pattern, Number(start.value) || 1),
    null, null);
  const printBtn = h("button", { class: "btn soft sm", disabled: !register }, icon("print"), t("labels.print"));
  printBtn.addEventListener("click", () => { location.hash = `#/labels/${product.product_id}`; });
  const csvBtn = h("button", { class: "btn sm", disabled: !register }, icon("download"), t("labels.csv_line"));
  csvBtn.addEventListener("click", busy(csvBtn, () => issueLabels(product, { csv: true, credentials: true })));

  return h("div", { class: "stack loose" },
    h("div", { class: "head" },
      h("div", { class: "stack tight" }, h("h1", { class: "h1" }, t("menu.production")),
        h("div", { class: "row wrap small muted" }, choose)),
      h("div", { class: "row wrap" }, importBtn, csvInput, exportBtn, listBtn)),
    h("div", { class: "tiles" },
      tile(t("stage.registered"), num(stats.total)),
      tile(t("stage.ready"), num(life.ready + life.registered)),
      tile(t("stage.sold"), num(life.sold)),
      tile(t("stage.active"), num(life.active), "hot"),
      tile(t("stage.online"), num(stats.online), "teal")),
    h("div", { class: "columns" }, h("div", { class: "col-form stack loose" }, form, stageCard()),
      h("div", { class: "col-main" },
        h("section", { class: "card", "aria-label": t("production.devices") },
          h("div", { class: "bar" }, h("label", { class: "sr", for: "dev-search" }, t("production.search")),
            Object.assign(search, { id: "dev-search" }), filters),
          h("div", { class: "scroll" }, h("table", { class: "grid mid" },
            h("thead", {}, h("tr", {}, ["id", "serial", "status", "conn", "fw", "warranty", "owner"]
              .map((c) => h("th", {}, t(`production.col_${c}`))))), tbody)),
          pagerBox,
          h("div", { class: "foot-note" }, t("production.privacy"))),
        h("section", { class: "card pad-lg", "aria-label": t("labels.preview") },
          h("div", { class: "row wrap top" }, sample,
            h("div", { class: "stack grow" }, h("span", { class: "h3" }, t("labels.qr_title")),
              h("span", { class: "small muted" }, t("labels.qr_help")),
              h("div", { class: "row wrap" }, printBtn, csvBtn)))))));
}
