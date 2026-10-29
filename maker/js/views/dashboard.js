// Dashboard: the organization's devices by stage, online now, products, firmware versions.

import { api, date, download, h, icon, num, t } from "../core.js";
import { bar, can, ctx, pageHead, picture, products, tile } from "../ui.js";

export default async function dashboard() {
  const [stats, list, trend] = await Promise.all([api("GET", "/api/platform/stats"), products(true),
    api("GET", "/api/platform/trend?weeks=12").catch(() => null)]);
  const mine = list.filter((p) => p.org_id === ctx.org.org_id);
  const perProduct = await Promise.all(mine.map((p) =>
    api("GET", `/api/platform/stats?product=${p.product_id}`).catch(() => null)));
  const life = stats.lifecycle;
  const actions = [];
  if (can("product.manage")) {
    actions.push(h("a", { class: "btn", href: "#/products/new" }, icon("plus"), t("dashboard.new_product")));
  }
  if (can("device.register")) {
    actions.push(h("a", { class: "btn primary", href: "#/production" }, t("dashboard.register")));
  }
  actions.push(h("button", { class: "btn", onclick: async () => {
    const text = await api("GET", "/api/platform/devices.csv", undefined, { as: "text" });
    download(`devices-${ctx.org.org_id}.csv`, text.replace(/^\uFEFF/, ""));
  } }, icon("download"), t("dashboard.export_csv")));
  const firmware = Object.entries(stats.firmware).sort((a, b) => b[1] - a[1]);
  const reported = firmware.reduce((s, [, n]) => s + n, 0);
  return h("div", { class: "stack loose" },
    pageHead(t("dashboard.title"), t("dashboard.subtitle", { org: ctx.org.name }), actions),
    h("div", { class: "tiles" },
      tile(t("dashboard.products"), num(mine.length), "", "#/products"),
      tile(t("stage.registered"), num(stats.total)),
      tile(t("stage.ready"), num(life.ready + life.registered)),
      tile(t("stage.sold"), num(life.sold)),
      tile(t("stage.active"), num(life.active), "hot"),
      tile(t("stage.online"), num(stats.online), "teal"),
      tile(t("dashboard.warranty_expiring"), num(stats.warranty_expiring),
        stats.warranty_expiring ? "orange" : "", "#/warranty"),
      trend ? tile(t("dashboard.faulty"), num(trend.faulty_now), trend.faulty_now ? "orange" : "",
        "#/production") : null),
    trend ? trendCard(trend) : null,
    h("div", { class: "columns" },
      h("section", { class: "card col-main", "aria-label": t("dashboard.products") },
        h("div", { class: "bar" }, h("span", { class: "h3" }, t("dashboard.products")),
          h("a", { href: "#/products", class: "small" }, t("common.all"))),
        mine.length ? h("div", { class: "scroll" }, h("table", { class: "grid mid" },
          h("thead", {}, h("tr", {}, [t("product.name"), t("product.code"), t("product.published"),
            t("stage.registered"), t("stage.active"), t("stage.online")].map((x) => h("th", {}, x)))),
          h("tbody", {}, mine.map((p, i) => {
            const s = perProduct[i];
            return h("tr", { class: "click", onclick: () => { location.hash = `#/product/${p.product_id}`; } },
              h("td", {}, h("span", { class: "row" }, picture(p, "sm"), p.name)),
              h("td", { class: "mono-sub ltr" }, p.code),
              h("td", {}, p.latest_published ? h("span", { class: "chip sm teal" },
                t("product.version_n", { n: num(p.latest_published) })) : h("span", { class: "chip sm" },
                t("product.unpublished"))),
              h("td", {}, num(s ? s.total : p.devices)),
              h("td", {}, num(s ? s.lifecycle.active : 0)),
              h("td", { class: "teal-text" }, num(s ? s.online : 0)));
          }))))
          : h("div", { class: "empty" }, h("p", {}, t("dashboard.no_products")),
            can("product.manage") ? h("a", { class: "btn primary", href: "#/products/new" },
              t("dashboard.new_product")) : null)),
      h("aside", { class: "col-side" },
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("dashboard.funnel")),
          bar(t("stage.registered"), stats.total, stats.total),
          bar(t("stage.ready"), life.ready + life.registered, stats.total),
          bar(t("stage.sold"), life.sold, stats.total, "violet"),
          bar(t("stage.active"), life.active, stats.total, "teal"),
          bar(t("stage.disabled"), life.disabled, stats.total, "orange")),
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("dashboard.firmware")),
          firmware.length ? firmware.slice(0, 8).map(([v, n]) => bar(h("span", { class: "ltr mono" }, v),
            n, reported, "violet"))
            : h("p", { class: "small muted" }, t("dashboard.no_firmware"))))));
}

/** Weeks of registrations, activations and tickets as three lines (inline SVG). */
function trendCard(trend) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const n = Math.max(trend.weeks.length, 2);
  svg.setAttribute("viewBox", `0 0 ${(n - 1) * 40} 100`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "trend");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", t("dashboard.trend"));
  const max = Math.max(1, ...trend.registered, ...trend.activated, ...trend.tickets);
  for (const [key, cls] of [["registered", "l-accent"], ["activated", "l-teal"], ["tickets", "l-orange"]]) {
    const values = trend[key];
    if (!values.length) continue;
    const pts = (values.length === 1 ? [values[0], values[0]] : values)
      .map((v, i) => `${i * 40},${(96 - (v / max) * 90).toFixed(1)}`).join(" ");
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", pts);
    line.setAttribute("class", cls);
    svg.append(line);
  }
  return h("section", { class: "card pad" },
    h("div", { class: "row between wrap" }, h("span", { class: "h3" }, t("dashboard.trend")),
      h("div", { class: "row wrap small" },
        h("span", { class: "legend l-accent" }, t("dashboard.t_registered")),
        h("span", { class: "legend l-teal" }, t("dashboard.t_activated")),
        h("span", { class: "legend l-orange" }, t("dashboard.t_tickets")))),
    trend.weeks.length ? svg : h("p", { class: "small muted" }, t("dashboard.no_trend")),
    trend.weeks.length ? h("div", { class: "row between tiny muted" }, h("span", {}, date(trend.weeks[0])),
      h("span", {}, date(trend.weeks[trend.weeks.length - 1]))) : null);
}
