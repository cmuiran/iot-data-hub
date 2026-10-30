// Router and shell of the maker panel (hash routes): sign-in, sidebar menu, organization.

import { api, busy, errorText, h, icon, lang, loadLanguage, logout, saved, session, t,
  toast } from "./core.js";
import { ctx } from "./ui.js";
import { startPickers } from "./picker.js";

const root = document.getElementById("root");
const VIEWS = {
  dashboard: () => import("./views/dashboard.js"),
  products: () => import("./views/products.js"),
  product: () => import("./views/product.js"),
  production: () => import("./views/production.js"),
  labels: () => import("./views/labels.js"),
  device: () => import("./views/device.js"),
  warranty: () => import("./views/warranty.js"),
  support: () => import("./views/support.js"),
  settings: () => import("./views/settings.js"),
};
const MENU = [
  ["dashboard", "dashboard", "dashboard"], ["products", "products", "products"],
  ["production", "production", "production"], ["labels", "labels", "qr"],
  ["warranty", "warranty", "warranty"], ["support", "support", "support"],
  ["settings", "settings", "settings"]];
const SECTION = { product: "products", device: "production" };

const accent = saved.get("accent");
if (accent) document.documentElement.dataset.accent = accent;

async function loadContext() {
  ctx.me = await api("GET", "/api/auth/me");
  let orgs = [];
  try {
    orgs = (await api("GET", "/api/platform/organizations")).filter((o) => o.kind === "manufacturer");
  } catch (err) {
    if (err.status !== 403) throw err;
  }
  ctx.orgs = orgs;
  const wanted = saved.get("org");
  ctx.org = orgs.find((o) => o.org_id === wanted) || orgs[0] || null;
  ctx.brand = ctx.org ? await api("GET", `/api/platform/organizations/${ctx.org.org_id}/branding`)
    .catch(() => null) : null;
}

async function render() {
  clearInterval(ctx.timer);
  ctx.timer = null;
  const [route, ...args] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (route === "login" || !session.signedIn) {
    if (route !== "login") saved.set("after", location.hash);
    return showLogin();
  }
  if (!ctx.me) {
    try { await loadContext(); }
    catch (err) {
      if (err.status === 401) { session.clear(); location.hash = "#/login"; return undefined; }
      root.replaceChildren(h("div", { class: "auth" }, h("p", {}, errorText(err))));
      return undefined;
    }
  }
  if (!ctx.org) return showNoOrg();
  const page = VIEWS[route] ? route : "dashboard";
  const main = h("main", { class: "main", id: "main" }, h("div", { class: "loading", role: "status" }, t("common.loading")));
  root.replaceChildren(h("div", { class: "shell" }, nav(SECTION[page] || page), main));
  try {
    const module = await VIEWS[page]();
    const node = await module.default(args);
    main.replaceChildren(...[node].flat());
    main.classList.add("enter");                  // in once; later redraws inside do not replay it
    setTimeout(() => main.classList.remove("enter"), 1400);
  } catch (err) {
    main.replaceChildren(h("div", { class: "card empty" }, h("p", {}, errorText(err)),
      h("a", { class: "btn", href: "#/dashboard" }, t("menu.dashboard"))));
  }
  window.scrollTo(0, 0);
  return undefined;
}

function nav(active) {
  const orgSwitch = ctx.orgs.length > 1 ? h("select", { class: "input sm",
    "aria-label": t("shell.organization"), onchange: (ev) => {
      saved.set("org", ev.target.value);
      ctx.org = ctx.orgs.find((o) => o.org_id === ev.target.value);
      ctx.products = null;
      ctx.me = null;                         // reload the brand of the other organization
      render();
    } }, ctx.orgs.map((o) => h("option", { value: o.org_id, selected: o.org_id === ctx.org.org_id },
    o.name))) : null;
  return h("nav", { class: "nav", "aria-label": t("shell.menu") },
    h("div", { class: "brand" }, ctx.brand?.has_logo
      ? h("img", { class: "logo img", src: `/api/public/brand/${ctx.org.org_id}/logo`, alt: "" })
      : h("span", { class: "logo" }, icon("logo", 20)),
    h("span", { class: "stack tight txt" }, h("span", { class: "name" }, ctx.brand?.display_name || ctx.org.name),
      h("span", { class: "role" }, t("shell.panel")))),
    MENU.map(([key, label, ic]) => h("a", { href: `#/${key}`,
      class: `item ${active === key ? "on" : ""}`, "aria-current": active === key ? "page" : null },
    icon(ic), t(`menu.${label}`))),
    h("div", { class: "foot" }, orgSwitch,
      h("div", { class: "who" }, h("span", { class: "small strong" }, ctx.me.display_name),
        h("span", { class: "tiny muted ltr" }, ctx.me.email || ctx.me.username)),
      h("a", { href: "#/login", class: "item", onclick: async (ev) => {
        ev.preventDefault();
        await logout();
        ctx.me = null;
        location.hash = "#/login";
      } }, icon("out"), t("shell.sign_out"))));
}

function showLogin() {
  const field = (label, attrs) => {
    const id = `f-${Math.random().toString(36).slice(2, 8)}`;
    const input = h("input", { class: "input", id, ...attrs });
    return { input, el: h("div", { class: "field" }, h("label", { for: id }, label), input) };
  };
  const user = field(t("auth.user"), { autocomplete: "username", dir: "ltr", required: true });
  const pass = field(t("auth.password"), { type: "password", autocomplete: "current-password",
    required: true });
  const code = field(t("auth.mfa_code"), { inputmode: "numeric", autocomplete: "one-time-code" });
  code.el.classList.add("hidden");
  let mfaToken = null;
  const submit = h("button", { class: "btn primary big block", type: "submit" }, t("auth.sign_in"));
  const form = h("form", { class: "stack" }, user.el, pass.el, code.el, submit);
  form.addEventListener("submit", busy(submit, async () => {
    let tokens;
    if (mfaToken) {
      tokens = await api("POST", "/api/auth/mfa/verify",
        { mfa_token: mfaToken, code: code.input.value.trim() }, { auth: false });
    } else {
      tokens = await api("POST", "/api/auth/login",
        { username: user.input.value.trim(), password: pass.input.value }, { auth: false });
      if (tokens.mfa_required) {
        mfaToken = tokens.mfa_token;
        code.el.classList.remove("hidden");
        code.input.focus();
        return;
      }
    }
    session.store(tokens);
    ctx.me = null;
    const after = saved.get("after");
    saved.set("after", null);
    location.hash = after && !after.startsWith("#/login") ? after : "#/dashboard";
    if (location.hash === after) render();
  }));
  root.replaceChildren(h("div", { class: "auth" }, h("div", { class: "card" },
    h("div", { class: "brand" }, h("span", { class: "logo" }, icon("logo", 20)),
      h("span", { class: "stack tight" }, h("span", { class: "name" }, t("shell.title")),
        h("span", { class: "role" }, t("shell.panel")))),
    h("h1", { class: "h1" }, t("auth.sign_in")),
    h("p", { class: "small muted" }, t("auth.lead")), form,
    h("button", { class: "btn sm", type: "button", onclick: async () => {
      await loadLanguage(lang === "fa" ? "en" : "fa");
      render();
    } }, icon("globe"), lang === "fa" ? "English" : "فارسی"))));
}

function showNoOrg() {
  root.replaceChildren(h("div", { class: "auth" }, h("div", { class: "card" },
    h("h1", { class: "h2" }, t("shell.no_org")), h("p", { class: "muted small" }, t("shell.no_org_hint")),
    h("button", { class: "btn", onclick: async () => {
      await logout();
      ctx.me = null;
      location.hash = "#/login";
    } }, icon("out"), t("shell.sign_out")))));
}

window.addEventListener("hashchange", render);
window.addEventListener("unhandledrejection", (ev) => toast(errorText(ev.reason), "err"));

await loadLanguage(saved.get("lang") || "fa");
document.title = t("shell.title");
startPickers({ h, icon: (n) => icon(n, 18), t, sheetClass: "dialog" });
render();
