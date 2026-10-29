// Warranty: which activated devices are covered, which end soon, look a device up.

import { api, busy, date, h, num, t } from "../core.js";
import { lifecycleChip, pageHead, products, tile, warrantyText } from "../ui.js";

const DAY = 86400000;

async function activeDevices() {
  const out = [];
  for (let offset = 0; offset < 5000; offset += 500) {
    const page = await api("GET", `/api/platform/devices?lifecycle=active&limit=500&offset=${offset}`);
    out.push(...page.devices);
    if (out.length >= page.total) break;
  }
  return out;
}

function status(d, now) {
  if (d.lifecycle === "disabled") return "void";
  if (d.warranty_months === null || d.warranty_months === undefined) return "none";
  if (!d.warranty_until) return "not_started";
  const end = new Date(d.warranty_until).getTime();
  if (end < now) return "expired";
  return end - now <= 30 * DAY ? "soon" : "covered";
}

const STATUS_CHIP = { covered: "teal", soon: "orange", expired: "", not_started: "accent",
  none: "", void: "orange" };

export default async function warranty() {
  const [devices, list] = await Promise.all([activeDevices(), products()]);
  const names = new Map(list.map((p) => [p.product_id, p.name]));
  const now = Date.now();
  const counts = { covered: 0, soon: 0, expired: 0, none: 0 };
  for (const d of devices) {
    const s = status(d, now);
    if (s in counts) counts[s] += 1;
  }
  const soon = devices.filter((d) => ["soon", "expired"].includes(status(d, now)))
    .sort((a, b) => new Date(a.warranty_until) - new Date(b.warranty_until)).slice(0, 100);

  const query = h("input", { class: "search", type: "search", placeholder: t("warranty.lookup_placeholder"),
    "aria-label": t("warranty.lookup"), dir: "ltr" });
  const result = h("div", { class: "stack" });
  const go = h("button", { class: "btn primary sm" }, t("warranty.lookup"));
  const lookup = busy(go, async () => {
    const q = query.value.trim();
    if (!q) return;
    const page = await api("GET", `/api/platform/devices?q=${encodeURIComponent(q)}&limit=10`);
    result.replaceChildren(...(page.devices.length ? page.devices.map((d) => {
      const s = status(d, now);
      return h("a", { class: "card pad", href: `#/device/${d.device_id}` },
        h("div", { class: "row between wrap" }, h("span", { class: "strong ltr mono accent-text" }, d.identifier),
          h("span", { class: `chip ${STATUS_CHIP[s]}` }, t(`warranty.state_${s}`))),
        h("div", { class: "kv" }, h("span", {}, t("production.product")), h("span", {}, names.get(d.product_id) || d.product_code)),
        h("div", { class: "kv" }, h("span", {}, t("device.activated_at")), h("span", {}, date(d.activated_at))),
        h("div", { class: "kv" }, h("span", {}, t("production.warranty")),
          h("span", {}, d.warranty_months === null ? "—" : t("warranty.months", { n: num(d.warranty_months) }))),
        h("div", { class: "kv" }, h("span", {}, t("warranty.status")), h("span", {}, warrantyText(d))),
        h("div", { class: "row" }, lifecycleChip(d.lifecycle)));
    }) : [h("p", { class: "small muted" }, t("warranty.not_found"))]));
  });
  go.addEventListener("click", lookup);
  query.addEventListener("keydown", (ev) => { if (ev.key === "Enter") lookup(ev); });

  return h("div", { class: "stack loose" },
    pageHead(t("menu.warranty"), t("warranty.subtitle")),
    h("div", { class: "tiles" },
      tile(t("warranty.covered"), num(counts.covered + counts.soon), "teal"),
      tile(t("warranty.soon"), num(counts.soon), counts.soon ? "orange" : ""),
      tile(t("warranty.expired"), num(counts.expired)),
      tile(t("warranty.no_warranty"), num(counts.none))),
    h("div", { class: "columns" },
      h("section", { class: "card col-main" },
        h("div", { class: "bar" }, h("span", { class: "h3" }, t("warranty.ending"))),
        h("div", { class: "scroll" }, h("table", { class: "grid mid" },
          h("thead", {}, h("tr", {}, ["id", "product", "activated", "until", "status"].map((c) =>
            h("th", {}, t(`warranty.col_${c}`))))),
          h("tbody", {}, soon.length ? soon.map((d) => {
            const s = status(d, now);
            return h("tr", { class: "click", onclick: () => { location.hash = `#/device/${d.device_id}`; } },
              h("td", { class: "id ltr" }, d.identifier),
              h("td", {}, names.get(d.product_id) || d.product_code),
              h("td", { class: "sub" }, date(d.activated_at)),
              h("td", { class: "sub" }, date(d.warranty_until)),
              h("td", {}, h("span", { class: `chip sm ${STATUS_CHIP[s]}` }, t(`warranty.state_${s}`))));
          }) : [h("tr", {}, h("td", { colspan: 5, class: "muted" }, t("warranty.none_ending")))]))),
        h("div", { class: "foot-note" }, t("warranty.footnote"))),
      h("aside", { class: "col-side" }, h("section", { class: "card pad" },
        h("span", { class: "h3" }, t("warranty.lookup")),
        h("div", { class: "row" }, query, go), result))));
}
