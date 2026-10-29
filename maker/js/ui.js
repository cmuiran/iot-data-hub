// Shared pieces of the maker panel: context (user, organization), permissions, form fields,
// status chips of devices, product pictures.

import { ago, api, date, h, icon, num, t } from "./core.js";

export const ctx = { me: null, orgs: [], org: null, products: null, protocols: null, timer: null };

/** May the user do `permission` in the selected organization? */
export function can(permission) {
  const scopes = ctx.me?.permissions?.[permission] || [];
  return scopes.some((s) => !("org" in s) || s.org === ctx.org?.org_id);
}

export async function products(force = false) {
  if (!ctx.products || force) {
    ctx.products = await api("GET", `/api/platform/products?org=${encodeURIComponent(ctx.org.org_id)}`);
  }
  return ctx.products;
}

export async function protocols() {
  ctx.protocols = ctx.protocols || await api("GET", "/api/platform/protocols");
  return ctx.protocols;
}

let uid = 0;
export function field(label, input, help = null) {
  uid += 1;
  const id = input.id || `fld-${uid}`;
  input.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, label), input,
    help ? h("span", { class: "help" }, help) : null);
}

export function input(attrs = {}) {
  return h("input", { class: "input", ...attrs });
}

export function select(options, attrs = {}) {
  const value = attrs.value;
  const el = h("select", { class: "input", ...attrs, value: undefined },
    options.map(([v, text]) => h("option", { value: v, selected: String(v) === String(value) }, text)));
  return el;
}

export function checkbox(label, checked, onchange = null) {
  const box = h("input", { type: "checkbox", checked, onchange });
  return { box, el: h("label", { class: "check" }, box, label) };
}

export function tile(label, value, kind = "", href = null) {
  return h(href ? "a" : "div", { class: `tile ${kind === "hot" ? "hot" : ""}`, href },
    h("span", { class: "label" }, label),
    h("span", { class: `value ${kind === "hot" ? "" : kind}` }, value));
}

export function crumbs(...parts) {
  return h("div", { class: "crumbs" }, parts.map((p, i) => [i ? " / " : "",
    Array.isArray(p) ? h("a", { href: p[1] }, p[0]) : h("span", { class: "here" }, p)]));
}

export function pageHead(title, subtitle, ...actions) {
  return h("div", { class: "head" },
    h("div", { class: "stack tight" }, h("h1", { class: "h1" }, title),
      subtitle ? h("span", { class: "small muted" }, subtitle) : null),
    h("div", { class: "row wrap" }, actions));
}

// --- devices -------------------------------------------------------------------------------------

const LIFECYCLE = { registered: "", ready: "accent", sold: "violet", active: "teal",
  disabled: "orange" };

export function lifecycleChip(lifecycle) {
  return h("span", { class: `chip sm ${LIFECYCLE[lifecycle] ?? ""}` }, t(`lifecycle.${lifecycle}`));
}

export function connection(d) {
  if (d.lifecycle === "disabled") return [t("conn.blocked"), "muted"];
  if (d.state === "online") return [t("conn.online"), "teal-text"];
  if (d.last_seen) return [t("conn.offline_since", { ago: ago(d.last_seen) }), "muted"];
  return [t("conn.never"), "muted"];
}

export function warrantyText(d) {
  if (d.lifecycle === "disabled") return t("warranty.void");
  if (d.warranty_months === null || d.warranty_months === undefined) return t("warranty.none");
  if (!d.warranty_until) return t("warranty.from_activation", { n: num(d.warranty_months) });
  const expired = new Date(d.warranty_until) < new Date();
  return t(expired ? "warranty.expired_on" : "warranty.until", { date: date(d.warranty_until) });
}

export function ownership(d) {
  if (d.lifecycle === "active") return t("owner.linked");
  if (d.lifecycle === "disabled") return t("owner.disabled");
  if (d.lifecycle === "sold") return t("owner.waiting");
  return "—";
}

// --- product pictures ----------------------------------------------------------------------------

const pictures = new Map();
export function picture(product, cls = "") {
  const box = h("span", { class: `pic ${cls}` });
  if (!product.has_image) {
    box.append(h("span", {}, t("product.picture")));
    return box;
  }
  const key = `${product.product_id}:${product.updated_at}`;
  const show = (url) => box.replaceChildren(h("img", { src: url, alt: product.name }));
  if (pictures.has(key)) show(pictures.get(key));
  else {
    box.append(icon("image", 22));
    api("GET", `/api/platform/products/${product.product_id}/image`, undefined, { as: "blob" })
      .then((blob) => { const url = URL.createObjectURL(blob); pictures.set(key, url); show(url); })
      .catch(() => box.replaceChildren(h("span", {}, t("product.picture"))));
  }
  return box;
}

/** Identifiers of a pattern like CTRL-8R-{0000}, as the server makes them. */
export function identifierAt(pattern, n) {
  return pattern.replace(/\{(0+)\}/, (_, zeros) => String(n).padStart(zeros.length, "0"));
}

export function patternWidth(pattern) {
  const m = pattern.match(/\{(0+)\}/);
  return m ? m[1].length : 0;
}

export function pager(total, offset, limit, go) {
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + limit, total);
  return h("div", { class: "pager" },
    h("span", {}, t("common.range", { from: num(from), to: num(to), total: num(total) })),
    h("div", { class: "row" },
      h("button", { class: "btn sm", disabled: offset <= 0, onclick: () => go(Math.max(0, offset - limit)) },
        t("common.previous")),
      h("button", { class: "btn sm", disabled: to >= total, onclick: () => go(offset + limit) },
        t("common.next"))));
}

export function bar(label, value, total, color = "") {
  const pct = total ? Math.round((value / total) * 20) * 5 : 0;
  return h("div", { class: "bar-row" }, h("span", { class: "grow" }, label),
    h("span", { class: "track" }, h("span", { class: `fill ${color} w${pct}` })),
    h("b", {}, num(value)));
}
