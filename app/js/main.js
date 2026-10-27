// Router and start-up of the consumer app (hash routes; account links put their token after #).

import { api, h, loadLanguage, saved, session, t } from "./core.js";
import { automationsPage } from "./auto.js";
import * as v from "./views.js";

const root = document.getElementById("app");
const state = { me: null, timer: null, cleanup: null };
const PUBLIC = new Set(["login", "signup", "forgot", "account"]);

const theme = saved.get("app.theme");
if (theme) document.documentElement.dataset.theme = theme;
else if (matchMedia("(prefers-color-scheme: light)").matches) {
  document.documentElement.dataset.theme = "light";
}

async function render() {
  clearInterval(state.timer);
  state.timer = null;
  if (state.cleanup) { state.cleanup(); state.cleanup = null; }
  const [route, ...args] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const page = route || "home";
  if (page === "claim") {               // a scanned label: #/claim/<identifier>/<code>
    if (!session.signedIn) { saved.set("app.after", location.hash); location.hash = "#/login"; return; }
    return show(v.add(state, { identifier: args[0], code: args[1] }), "add");
  }
  if (!PUBLIC.has(page) && !session.signedIn) { location.hash = "#/login"; return; }
  if (!PUBLIC.has(page) && !state.me) {
    try { state.me = await api("GET", "/api/auth/me"); }
    catch { session.clear(); location.hash = "#/login"; return; }
  }
  switch (page) {
    case "login": return show(v.login());
    case "signup": return show(v.signup());
    case "forgot": return show(v.forgot());
    case "account":
      if (args[0] === "verify") return show(v.verify(args[1]));
      if (args[0] === "reset") return show(v.reset(args[1]));
      if (args[0] === "invitation") return show(v.invitation(args[1]));
      if (args[0] === "forgot") return show(v.forgot());
      return show(v.login());
    case "device": return show(v.device(state, args[0]), "home");
    case "add": return show(v.add(state), "add");
    case "me": return show(v.me(state), "me");
    case "auto": return show(automationsPage(), "auto");
    case "members": return show(v.members(state, args[0]), "me");
    default: return show(v.home(state), "home");
  }
}

function show(node, navItem = null) {
  const parts = [h("div", { class: "glow", "aria-hidden": "true" }), node];
  if (navItem) parts.push(v.nav(navItem));
  root.replaceChildren(...parts);
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => {
  if (!location.hash.startsWith("#/login")) state.me = session.signedIn ? state.me : null;
  render();
});
window.addEventListener("offline", () => document.body.classList.add("is-offline"));
window.addEventListener("online", () => document.body.classList.remove("is-offline"));

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => { /* works without it */ });
}

await loadLanguage(saved.get("app.lang") || "fa");       // Persian first; English in "Me"
document.title = t("app.title");
render();
