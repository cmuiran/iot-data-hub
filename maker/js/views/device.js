// One manufactured device: identity, stage, connection health, firmware, warranty, and the
// maker's actions (stage, enable, warranty, factory reset). Never the buyer or its data.

import { api, busy, confirmAction, date, dialog, errorText, h, icon, t, toast } from "../core.js";
import { can, connection, crumbs, field, input, lifecycleChip, ownership, products, select,
  warrantyText } from "../ui.js";

export default async function devicePage(args) {
  const id = args[0];
  const box = h("div", { class: "stack loose" });
  const register = can("device.register");

  async function draw() {
    const d = await api("GET", `/api/platform/devices/${id}`);
    const product = (await products()).find((p) => p.product_id === d.product_id);
    const [conn, cls] = connection(d);
    const stage = select(["registered", "ready", "sold", "disabled"].map((s) => [s, t(`lifecycle.${s}`)]),
      { value: d.lifecycle === "active" ? "" : d.lifecycle, disabled: !register || d.lifecycle === "active" });
    if (d.lifecycle === "active") stage.prepend(h("option", { value: "", selected: true }, t("lifecycle.active")));
    const saveStage = h("button", { class: "btn sm", disabled: !register || d.lifecycle === "active" }, t("common.save"));
    saveStage.addEventListener("click", busy(saveStage, async () => {
      if (!stage.value) return;
      if (stage.value === "disabled" && !await confirmAction(t("device.disable_confirm"), t("lifecycle.disabled"), true)) return;
      await api("PATCH", `/api/platform/devices/${id}`, { lifecycle: stage.value });
      toast(t("common.saved"), "ok");
      draw();
    }));
    const months = input({ type: "number", min: 0, max: 240, dir: "ltr", value: d.warranty_months ?? "",
      disabled: !register });
    const saveWarranty = h("button", { class: "btn sm", disabled: !register }, t("common.save"));
    saveWarranty.addEventListener("click", busy(saveWarranty, async () => {
      await api("PATCH", `/api/platform/devices/${id}`, { warranty_months: Number(months.value) || 0 });
      toast(t("common.saved"), "ok");
      draw();
    }));
    const toggle = h("button", { class: `btn sm ${d.enabled ? "danger" : "soft"}`, disabled: !register },
      t(d.enabled ? "device.stop" : "device.start"));
    toggle.addEventListener("click", busy(toggle, async () => {
      await api("PATCH", `/api/platform/devices/${id}`, { enabled: !d.enabled });
      draw();
    }));
    const reset = h("button", { class: "btn sm danger", disabled: !register || Boolean(d.parent_device_id) },
      icon("reset"), t("device.reset"));
    reset.addEventListener("click", busy(reset, async () => {
      if (!await confirmAction(t("device.reset_confirm"), t("device.reset"), true)) return;
      const r = await api("POST", `/api/platform/devices/${id}/reset`);
      await dialog(t("device.reset_done"), h("div", { class: "stack" },
        h("p", { class: "small muted" }, t("device.new_code")), h("div", { class: "code-box" }, r.claim_code)));
      draw();
    }));
    const connText = h("textarea", { class: "input ltr mono", rows: 4, disabled: !register },
      JSON.stringify(d.connection || {}, null, 2));
    const saveConn = h("button", { class: "btn sm", disabled: !register }, t("common.save"));
    saveConn.addEventListener("click", busy(saveConn, async () => {
      let value;
      try { value = JSON.parse(connText.value || "{}"); }
      catch { throw new Error(t("protocols.bad_json")); }
      await api("PATCH", `/api/platform/devices/${id}`, { connection: value });
      toast(t("common.saved"), "ok");
      draw();
    }));
    const kv = (label, value, extra = "") => h("div", { class: "kv" }, h("span", {}, label),
      h("span", { class: extra }, value));
    box.replaceChildren(
      crumbs([t("menu.production"), `#/production/${d.product_id}`], d.identifier),
      h("div", { class: "head" },
        h("div", { class: "stack tight" }, h("h1", { class: "h1 ltr" }, d.identifier),
          h("div", { class: "row wrap" }, lifecycleChip(d.lifecycle),
            h("span", { class: `chip ${d.state === "online" ? "teal" : ""}` }, h("span", { class: "dot" }), conn),
            product ? h("span", { class: "chip" }, product.name) : null,
            h("span", { class: "chip ltr" }, `${t(`protocol.${d.protocol}`)} · v${d.version}`))),
        h("div", { class: "row wrap" }, toggle, reset)),
      h("div", { class: "columns" },
        h("div", { class: "col-main" },
          h("section", { class: "card pad" }, h("span", { class: "h3" }, t("device.identity")),
            kv(t("production.col_id"), d.identifier, "ltr mono"),
            kv(t("production.col_serial"), d.serial_number || "—", "ltr mono"),
            kv(t("device.device_id"), d.device_id, "ltr mono small"),
            kv(t("production.series"), d.batch || "—", "ltr"),
            kv(t("device.registered_at"), date(d.registered_at, true)),
            kv(t("device.activated_at"), date(d.activated_at, true)),
            kv(t("production.col_owner"), ownership(d)),
            d.parent_device_id ? kv(t("device.behind_hub"), d.parent_device_id, "ltr mono small") : null),
          h("section", { class: "card pad" }, h("span", { class: "h3" }, t("device.health")),
            kv(t("production.col_conn"), conn, cls),
            kv(t("device.last_seen"), d.last_seen ? date(d.last_seen, true) : "—"),
            kv(t("device.enabled"), t(d.enabled ? "common.yes" : "common.no")),
            kv(t("production.col_fw"), d.firmware_version || "—", "ltr"),
            kv(t("device.fw_reported"), date(d.firmware_reported_at, true))),
          h("section", { class: "card pad" }, h("span", { class: "h3" }, t("device.connection")),
            h("span", { class: "help" }, t("device.connection_help")), connText,
            h("div", { class: "row end" }, saveConn))),
        h("aside", { class: "col-side" },
          h("section", { class: "card pad" }, h("span", { class: "h3" }, t("device.stage")),
            field(t("device.stage"), stage, t("device.stage_help")), h("div", { class: "row end" }, saveStage)),
          h("section", { class: "card pad" }, h("span", { class: "h3" }, t("menu.warranty")),
            kv(t("warranty.status"), warrantyText(d)),
            field(t("production.warranty"), months), h("div", { class: "row end" }, saveWarranty)),
          h("div", { class: "note" }, t("production.privacy")))));
  }
  try { await draw(); }
  catch (err) { box.replaceChildren(h("div", { class: "card empty" }, h("p", {}, errorText(err)))); }

  return box;
}
