// Settings: the organization's team (members, roles, invitations) and the user's own account
// (language, accent colour, password, sign-out).

import { api, busy, confirmAction, date, h, icon, lang, loadLanguage, logout, num, saved, t,
  toast } from "../core.js";
import { can, ctx, field, input, pageHead, select } from "../ui.js";

const ROLES = ["manufacturer_admin", "production_operator", "manufacturer_support"];
const ACCENTS = [["", "#E8B86A"], ["teal", "#7FD1C7"], ["violet", "#A99BFF"], ["coral", "#FF9F7A"]];

export default async function settings() {
  const org = ctx.org.org_id;
  const manage = can("org.members");
  const members = h("tbody");
  const invites = h("tbody");

  async function draw() {
    if (!manage) return;
    const [rows, open] = await Promise.all([
      api("GET", `/api/platform/organizations/${org}/members`),
      api("GET", `/api/platform/organizations/${org}/invitations`)]);
    members.replaceChildren(...rows.map((m) => {
      const remove = h("button", { class: "icon-btn", "aria-label": t("settings.remove_member"),
        disabled: m.user_id === ctx.me.user_id }, icon("x"));
      remove.addEventListener("click", busy(remove, async () => {
        if (!await confirmAction(t("settings.remove_confirm", { name: m.display_name }), t("common.delete"), true)) return;
        await api("DELETE", `/api/platform/organizations/${org}/members/${m.user_role_id}`);
        draw();
      }));
      return h("tr", {}, h("td", { class: "strong" }, m.display_name),
        h("td", { class: "sub ltr" }, m.email || m.username),
        h("td", {}, h("span", { class: "chip sm accent" }, t(`roles.${m.role_id}`))),
        h("td", { class: "sub" }, date(m.granted_at)), h("td", {}, remove));
    }));
    const pending = open.filter((i) => !i.accepted_at && !i.revoked_at);
    invites.replaceChildren(...(pending.length ? pending.map((i) => {
      const revoke = h("button", { class: "btn sm danger" }, t("settings.revoke"));
      revoke.addEventListener("click", busy(revoke, async () => {
        await api("DELETE", `/api/platform/organizations/${org}/invitations/${i.invitation_id}`);
        draw();
      }));
      return h("tr", {}, h("td", { class: "ltr" }, i.email),
        h("td", {}, h("span", { class: "chip sm" }, t(`roles.${i.role_id}`))),
        h("td", { class: "sub" }, date(i.expires_at)), h("td", {}, revoke));
    }) : [h("tr", {}, h("td", { colspan: 4, class: "muted" }, t("settings.no_invitations")))]));
  }

  const email = input({ type: "email", dir: "ltr", placeholder: "name@company.com" });
  const role = select(ROLES.map((r) => [r, t(`roles.${r}`)]), { value: "production_operator" });
  const invite = h("button", { class: "btn primary sm", disabled: !manage }, t("settings.invite"));
  invite.addEventListener("click", busy(invite, async () => {
    await api("POST", `/api/platform/organizations/${org}/invitations`,
      { email: email.value.trim(), role: role.value });
    email.value = "";
    toast(t("settings.invited"), "ok");
    draw();
  }));

  const current = input({ type: "password", autocomplete: "current-password" });
  const next = input({ type: "password", autocomplete: "new-password" });
  const change = h("button", { class: "btn sm" }, t("settings.change_password"));
  change.addEventListener("click", busy(change, async () => {
    await api("POST", "/api/auth/change-password",
      { current_password: current.value, new_password: next.value });
    current.value = "";
    next.value = "";
    toast(t("settings.password_changed"), "ok");
  }));

  const accents = h("div", { class: "row wrap" }, ACCENTS.map(([key, color]) => h("button", {
    class: `filter ${(document.documentElement.dataset.accent || "") === key ? "on" : ""}`,
    "aria-label": t("settings.accent_x", { x: color }), onclick: () => {
      if (key) document.documentElement.dataset.accent = key;
      else delete document.documentElement.dataset.accent;
      saved.set("accent", key || null);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } }, h("span", { class: "ltr mono" }, color))));

  await draw();
  const [brandBox, billBox] = await Promise.all([branding(org), billing(org)]);
  return h("div", { class: "stack loose" },
    pageHead(t("menu.settings"), ctx.org.name),
    h("div", { class: "columns" },
      h("div", { class: "col-main" }, brandBox, billBox,
        h("section", { class: "card" },
          h("div", { class: "bar" }, h("span", { class: "h3" }, t("settings.team"))),
          manage ? h("div", { class: "scroll" }, h("table", { class: "grid mid" },
            h("thead", {}, h("tr", {}, ["name", "email", "role", "since", ""].map((c) =>
              h("th", {}, c ? t(`settings.col_${c}`) : "")))), members))
            : h("p", { class: "small muted pad-x" }, t("settings.no_permission")),
          h("div", { class: "foot-note" }, ROLES.map((r) => h("div", {}, h("b", {}, t(`roles.${r}`)), " — ",
            t(`settings.role_${r}`))))),
        manage ? h("section", { class: "card" },
          h("div", { class: "bar" }, h("span", { class: "h3" }, t("settings.invitations"))),
          h("div", { class: "scroll" }, h("table", { class: "grid mid" },
            h("thead", {}, h("tr", {}, ["email", "role", "expires", ""].map((c) =>
              h("th", {}, c ? t(`settings.col_${c}`) : "")))), invites))) : null),
      h("aside", { class: "col-side" },
        manage ? h("section", { class: "card pad" }, h("span", { class: "h3" }, t("settings.invite_title")),
          field(t("settings.email"), email), field(t("settings.role"), role),
          h("span", { class: "help" }, t("settings.invite_help")), invite) : null,
        h("section", { class: "card pad" }, h("span", { class: "h3" }, t("settings.account")),
          h("div", { class: "kv" }, h("span", {}, t("settings.name")), h("span", {}, ctx.me.display_name)),
          h("div", { class: "kv" }, h("span", {}, t("settings.email")), h("span", { class: "ltr" }, ctx.me.email || ctx.me.username)),
          h("div", { class: "row between" }, h("span", { class: "small" }, t("settings.language")),
            h("button", { class: "btn sm", onclick: async () => {
              await loadLanguage(lang === "fa" ? "en" : "fa");
              window.dispatchEvent(new HashChangeEvent("hashchange"));
            } }, icon("globe"), lang === "fa" ? "English" : "فارسی")),
          h("div", { class: "stack tight" }, h("span", { class: "small" }, t("settings.accent")), accents)),
        ctx.me.auth_source === "local" ? h("section", { class: "card pad" },
          h("span", { class: "h3" }, t("settings.password")),
          field(t("settings.current_password"), current), field(t("settings.new_password"), next), change) : null,
        h("button", { class: "btn danger", onclick: async () => {
          await logout();
          ctx.me = null;
          location.hash = "#/login";
        } }, icon("out"), t("shell.sign_out")))));
}

/** The maker's brand: name, accent colour, support contact, logo; shown in its panel, on its
 * devices in the buyer's app, and on the app's start page opened with ?brand=<org>. */
async function branding(org) {
  const b = await api("GET", `/api/platform/organizations/${org}/branding`).catch(() => null);
  if (!b) return null;
  const manage = can("product.manage");
  const nameIn = input({ value: b.brand_name || "", placeholder: ctx.org.name, disabled: !manage });
  const accent = input({ type: "color", value: b.accent || "#E8B86A", disabled: !manage });
  const email = input({ type: "email", dir: "ltr", value: b.support_email || "", disabled: !manage });
  const phone = input({ type: "tel", dir: "ltr", value: b.support_phone || "", disabled: !manage });
  const logo = h("div", { class: "logo-box" }, b.has_logo
    ? h("img", { src: `/api/public/brand/${org}/logo?v=${encodeURIComponent(b.updated_at || "")}`, alt: b.display_name })
    : icon("image", 26));
  const upload = h("button", { class: "btn sm", disabled: !manage }, icon("upload"), t("brand.upload"));
  upload.addEventListener("click", () => {
    const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp" });
    file.addEventListener("change", busy(upload, async () => {
      const f = file.files[0];
      if (!f) return;
      await api("PUT", `/api/platform/organizations/${org}/branding/logo`, undefined, { raw: f, type: f.type });
      toast(t("brand.logo_saved"), "ok");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    }));
    file.click();
  });
  const save = h("button", { class: "btn primary sm", disabled: !manage }, t("common.save"));
  save.addEventListener("click", busy(save, async () => {
    await api("PUT", `/api/platform/organizations/${org}/branding`, {
      brand_name: nameIn.value.trim() || null, accent: accent.value || null,
      support_email: email.value.trim() || null, support_phone: phone.value.trim() || null });
    toast(t("common.saved"), "ok");
  }));
  const link = `${location.origin}/app/?brand=${encodeURIComponent(org)}`;
  return h("section", { class: "card pad" }, h("span", { class: "h3" }, t("brand.title")),
    h("div", { class: "row top wrap" }, logo, h("div", { class: "stack grow" },
      h("div", { class: "form-grid" }, field(t("brand.name"), nameIn), field(t("brand.accent"), accent),
        field(t("brand.email"), email), field(t("brand.phone"), phone)),
      h("div", { class: "row wrap" }, upload, save))),
    h("div", { class: "note" }, h("span", {}, t("brand.app_link")), h("span", { class: "ltr mono" }, link)));
}

/** The platform plan, this month so far, past invoices, every plan to compare. */
async function billing(org) {
  if (!can("org.members")) return null;
  const b = await api("GET", `/api/platform/organizations/${org}/billing`).catch(() => null);
  if (!b) return null;
  const rial = (n) => t("billing.rial", { n: num(n) });
  return h("section", { class: "card pad" }, h("div", { class: "row between wrap" }, h("span", { class: "h3" }, t("billing.title")),
    h("span", { class: "chip accent" }, t("billing.plan_x", { x: b.plan.name }))),
  h("div", { class: "tiles" },
    h("div", { class: "tile" }, h("span", { class: "label" }, t("billing.active")), h("span", { class: "value" }, num(b.active_devices))),
    h("div", { class: "tile hot" }, h("span", { class: "label" }, t("billing.this_month", { p: num(b.current.period) })),
      h("span", { class: "value" }, rial(b.current.amount)))),
  b.over_limit ? h("div", { class: "note warn" }, t("billing.over_limit", { n: num(b.plan.max_devices) })) : null,
  h("div", { class: "products" }, b.plans.map((p) => h("div", { class: `plan ${p.plan_id === b.plan.plan_id ? "on" : ""}` },
    h("span", { class: "strong" }, p.name),
    h("span", { class: "small" }, t("billing.monthly", { x: rial(p.monthly_fee) })),
    h("span", { class: "small muted" }, t("billing.included", { n: num(p.included_devices) })),
    h("span", { class: "small muted" }, t("billing.per_device", { x: rial(p.per_device_fee) })),
    p.max_devices ? h("span", { class: "small muted" }, t("billing.max", { n: num(p.max_devices) })) : null))),
  h("span", { class: "help" }, t("billing.change_help")),
  b.invoices.length ? h("div", { class: "scroll" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["period", "plan", "devices", "amount", "status"].map((c) => h("th", {}, t(`billing.col_${c}`))))),
    h("tbody", {}, b.invoices.map((i) => h("tr", {}, h("td", { class: "ltr" }, num(i.period)), h("td", {}, i.plan_id),
      h("td", {}, num(i.active_devices)), h("td", {}, rial(i.amount)),
      h("td", {}, h("span", { class: `chip sm ${i.status === "paid" ? "teal" : i.status === "void" ? "" : "orange"}` },
        t(`billing.status_${i.status}`)))))))) : null);
}
