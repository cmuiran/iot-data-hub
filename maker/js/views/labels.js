// Labels and QR: issue activation codes for unclaimed devices, print the label sheet (the
// browser's print dialog saves it as PDF) or download the CSV for the production line.

import { api, busy, confirmAction, download, h, icon, num, parseCsv, t, toast } from "../core.js";
import { can, checkbox, ctx, field, input, pageHead, products, select } from "../ui.js";
import { qrSvg } from "../qr.js";

/** One label: QR (claim link), model, identifier, serial, code (masked when not given). */
export function labelCard(product, identifier, serial, code, qr = null) {
  const text = qr || `${location.origin}/app/#/claim/${identifier}`;
  return h("div", { class: "label-card" },
    qrSvg(text, 88, t("labels.qr_of", { id: identifier })),
    h("div", { class: "txt" },
      h("span", { class: "model" }, product.code),
      h("span", { class: "ident" }, identifier),
      h("span", { class: "meta" }, serial ? `S/N ${serial}` : "S/N —"),
      h("span", { class: "meta" }, `CODE ${code || "●●●●-●●●●-●●●●"}`)));
}

/** New codes for the devices (a batch, a list, or every unclaimed one) as CSV text. */
export async function issueLabels(product, { batch = null, deviceIds = null, credentials = false,
  csv = false, print = false } = {}) {
  if (!await confirmAction(t(credentials ? "labels.issue_confirm_credentials" : "labels.issue_confirm"),
    t("labels.issue"))) return null;
  const text = await api("POST", `/api/platform/products/${product.product_id}/labels`, {
    batch: batch || undefined, device_ids: deviceIds || undefined, credentials }, { as: "text" });
  const rows = parseCsv(text);
  const head = rows.shift() || [];
  const records = rows.map((r) => Object.fromEntries(head.map((k, i) => [k, r[i]])));
  toast(t("labels.issued", { n: num(records.length) }), "ok");
  if (csv) {
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
    download(`labels-${product.code}-${batch || "all"}-${stamp}.csv`, text);
  }
  if (print) showSheet(product, records);
  return records;
}

function showSheet(product, records) {
  const main = document.getElementById("main");
  const back = h("button", { class: "btn" }, t("common.back"));
  back.addEventListener("click", () => window.dispatchEvent(new HashChangeEvent("hashchange")));
  main.replaceChildren(
    h("div", { class: "head no-print" },
      h("div", { class: "stack tight" }, h("h1", { class: "h1" }, t("labels.sheet_title")),
        h("span", { class: "small muted" }, t("labels.sheet_help", { n: num(records.length) }))),
      h("div", { class: "row" }, back,
        h("button", { class: "btn primary", onclick: () => window.print() }, icon("print"), t("labels.print_now")))),
    h("div", { class: "note warn no-print" }, t("labels.once")),
    h("div", { class: "print-sheet" }, records.map((r) =>
      labelCard(product, r.identifier, r.serial_number, r.claim_code, r.qr))));
  setTimeout(() => window.print(), 400);
}

export default async function labelsPage(args) {
  const list = (await products(true)).filter((p) => p.org_id === ctx.org.org_id && p.latest_published);
  if (!list.length) {
    return h("div", { class: "stack loose" }, pageHead(t("menu.labels"), ""),
      h("div", { class: "card empty" }, h("p", {}, t("labels.no_products"))));
  }
  const product = list.find((p) => p.product_id === args[0]) || list[0];
  const register = can("device.register");
  const choose = select(list.map((p) => [p.product_id, `${p.name} · ${p.code}`]),
    { value: product.product_id });
  choose.addEventListener("change", () => { location.hash = `#/labels/${choose.value}`; });
  const scope = select([["all", t("labels.scope_all")], ["batch", t("labels.scope_batch")],
    ["ids", t("labels.scope_ids")]]);
  const batch = input({ dir: "ltr", placeholder: "1405-08-01" });
  const ids = h("textarea", { class: "input ltr mono", rows: 4, placeholder: "CTRL-8R-0001\nCTRL-8R-0002" });
  const batchBox = field(t("production.series"), batch);
  const idsBox = field(t("labels.identifiers"), ids, t("labels.identifiers_help"));
  const credentials = checkbox(t("labels.with_credentials"), false);
  const sync = () => {
    batchBox.classList.toggle("hidden", scope.value !== "batch");
    idsBox.classList.toggle("hidden", scope.value !== "ids");
  };
  scope.addEventListener("change", sync);
  sync();

  async function target() {
    if (scope.value === "batch") {
      if (!batch.value.trim()) throw new Error(t("labels.need_batch"));
      return { batch: batch.value.trim() };
    }
    if (scope.value === "ids") {
      const wanted = ids.value.split(/[\s,]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
      if (!wanted.length) throw new Error(t("labels.need_ids"));
      const found = [];
      for (const ident of wanted) {
        const page = await api("GET", `/api/platform/devices?product=${product.product_id}&q=${encodeURIComponent(ident)}&limit=5`);
        const d = page.devices.find((x) => x.identifier.toUpperCase() === ident);
        if (!d) throw new Error(t("labels.unknown_id", { id: ident }));
        found.push(d.device_id);
      }
      return { deviceIds: found };
    }
    return {};
  }
  const print = h("button", { class: "btn primary", disabled: !register }, icon("print"), t("labels.print"));
  print.addEventListener("click", busy(print, async () => issueLabels(product,
    { ...(await target()), credentials: credentials.box.checked, print: true })));
  const csv = h("button", { class: "btn", disabled: !register }, icon("download"), t("labels.csv_line"));
  csv.addEventListener("click", busy(csv, async () => issueLabels(product,
    { ...(await target()), credentials: credentials.box.checked, csv: true })));

  return h("div", { class: "stack loose" },
    pageHead(t("menu.labels"), t("labels.subtitle")),
    h("div", { class: "columns" },
      h("section", { class: "card pad-lg col-form" },
        h("span", { class: "h3" }, t("labels.issue_title")),
        field(t("production.product"), choose),
        field(t("labels.scope"), scope), batchBox, idsBox, credentials.el,
        h("span", { class: "help" }, t("labels.credentials_help")),
        h("div", { class: "note warn" }, t("labels.replace_note")),
        h("div", { class: "row wrap" }, print, csv)),
      h("div", { class: "col-main" },
        h("section", { class: "card pad-lg" },
          h("div", { class: "row wrap top" },
            labelCard(product, product.identifier_pattern.replace(/\{(0+)\}/, (_, z) => "1".padStart(z.length, "0")), null, null),
            h("div", { class: "stack grow" }, h("span", { class: "h3" }, t("labels.qr_title")),
              h("span", { class: "small muted" }, t("labels.qr_help")),
              h("span", { class: "small muted" }, t("labels.pdf_help"))))),
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("labels.csv_columns")),
          ["identifier", "serial_number", "claim_code", "qr", "device_id", "device_secret"].map((c) =>
            h("div", { class: "kv" }, h("span", { class: "ltr mono" }, c), h("span", {}, t(`labels.col_${c}`))))))));
}
