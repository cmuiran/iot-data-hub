// Products: the organization's products, and defining a new one.

import { api, busy, h, icon, num, t, toast } from "../core.js";
import { can, checkbox, crumbs, ctx, field, identifierAt, input, pageHead, picture, products,
  select } from "../ui.js";

export default async function productsPage(args) {
  if (args[0] === "new") return newProduct();
  const list = (await products(true)).filter((p) => p.org_id === ctx.org.org_id);
  const search = h("input", { class: "search", type: "search", placeholder: t("products.search"),
    "aria-label": t("products.search") });
  const grid = h("div", { class: "products" });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = list.filter((p) => !q || p.name.toLowerCase().includes(q) ||
      p.code.toLowerCase().includes(q) || (p.category || "").toLowerCase().includes(q));
    grid.replaceChildren(...(shown.length ? shown.map(card)
      : [h("div", { class: "card empty" }, h("p", {}, t(list.length ? "products.no_match" : "dashboard.no_products")))]));
  };
  search.addEventListener("input", draw);
  draw();
  return h("div", { class: "stack loose" },
    pageHead(t("menu.products"), t("products.subtitle", { n: num(list.length) }),
      can("product.manage") ? h("a", { class: "btn primary", href: "#/products/new" }, icon("plus"),
        t("dashboard.new_product")) : null),
    h("div", { class: "row wrap" }, search), grid);
}

function card(p) {
  return h("a", { class: "product-card", href: `#/product/${p.product_id}` },
    h("div", { class: "row" }, picture(p, "sm"),
      h("div", { class: "stack tight grow" }, h("span", { class: "h3" }, p.name),
        h("span", { class: "small muted" }, p.category || t("product.no_category")))),
    h("div", { class: "row wrap" },
      h("span", { class: "chip ltr" }, p.code),
      p.kind === "edge_hub" ? h("span", { class: "chip violet" }, t("product.kind_edge_hub")) : null,
      p.catalog ? h("span", { class: "chip" }, t("product.in_catalog")) : null,
      p.draft ? h("span", { class: "chip violet" }, t("product.draft_n", { n: num(p.draft) })) : null,
      p.latest_published ? h("span", { class: "chip teal" }, t("product.published_n",
        { n: num(p.latest_published), devices: num(p.devices) })) : h("span", { class: "chip" },
        t("product.unpublished"))));
}

function newProduct() {
  const code = input({ dir: "ltr", required: true, placeholder: "CTRL-8R", class: "input key",
    autocapitalize: "characters" });
  const nameIn = input({ required: true, placeholder: t("products.name_example") });
  const category = input({ placeholder: t("products.category_example") });
  const pattern = input({ dir: "ltr", required: true, class: "input key", value: "" });
  const kind = select([["device", t("product.kind_device")], ["edge_hub", t("product.kind_edge_hub")]]);
  const catalog = checkbox(t("product.catalog"), false);
  const description = h("textarea", { class: "input", rows: 3 });
  const preview = h("span", { class: "small muted ltr mono" });
  let patternTouched = false;
  const update = () => {
    code.value = code.value.toUpperCase().replace(/[^A-Z0-9-]/g, "");
    if (!patternTouched) pattern.value = code.value ? `${code.value}-{0000}` : "";
    pattern.value = pattern.value.toUpperCase();
    preview.textContent = /\{0+\}/.test(pattern.value)
      ? `${identifierAt(pattern.value, 1)} · ${identifierAt(pattern.value, 2)} · …` : "";
  };
  code.addEventListener("input", update);
  pattern.addEventListener("input", () => { patternTouched = true; update(); });
  const submit = h("button", { class: "btn primary big", type: "submit" }, t("products.create"));
  const form = h("form", { class: "card pad-lg col-form" },
    h("span", { class: "h3" }, t("products.new_title")),
    h("div", { class: "form-grid" },
      field(t("product.code"), code, t("products.code_help")),
      field(t("product.name"), nameIn),
      field(t("product.category"), category),
      field(t("product.kind"), kind),
      h("div", { class: "span" }, field(t("product.pattern"), pattern, t("products.pattern_help"))),
      h("div", { class: "span" }, preview),
      h("div", { class: "span" }, field(t("product.description"), description)),
      h("div", { class: "span" }, catalog.el)),
    submit);
  form.addEventListener("submit", busy(submit, async () => {
    const record = await api("POST", "/api/platform/products", {
      org_id: ctx.org.org_id, code: code.value.trim(), name: nameIn.value.trim(),
      category: category.value.trim() || undefined, description: description.value.trim() || undefined,
      identifier_pattern: pattern.value.trim(), kind: kind.value, catalog: catalog.box.checked });
    ctx.products = null;
    toast(t("products.created"), "ok");
    location.hash = `#/product/${record.product_id}/io`;
  }));
  return h("div", { class: "stack loose" },
    crumbs([t("menu.products"), "#/products"], t("products.new_title")),
    pageHead(t("products.new_title"), t("products.new_subtitle")),
    h("div", { class: "columns" }, form,
      h("aside", { class: "col-side" }, h("div", { class: "card pad" },
        h("span", { class: "h3" }, t("products.steps_title")),
        [1, 2, 3, 4].map((n) => h("div", { class: "row top" },
          h("span", { class: "chip accent" }, num(n)), h("span", { class: "small sub" }, t(`products.step_${n}`))))))));
}
