// Support: find a device by identifier or serial and see what is wrong with it, with the
// actions the support desk may take (new activation code, factory reset, block / unblock).

import { ago, api, busy, confirmAction, date, dialog, h, icon, t, toast } from "../core.js";
import { can, connection, field, lifecycleChip, ownership, pageHead, products, warrantyText } from "../ui.js";

/** What the facts of a device suggest (the first problem first). */
function diagnose(d) {
  const out = [];
  if (d.lifecycle === "disabled") out.push(["warn", "support.d_blocked"]);
  if (d.lifecycle !== "active") out.push(["info", d.lifecycle === "sold" ? "support.d_not_claimed" : "support.d_in_stock"]);
  if (!d.enabled) out.push(["warn", "support.d_not_enabled"]);
  else if (!d.last_seen) out.push(["warn", "support.d_never_connected"]);
  else if (d.state !== "online") {
    const hours = (Date.now() - new Date(d.last_seen).getTime()) / 3600000;
    out.push(["warn", hours > 72 ? "support.d_offline_long" : "support.d_offline"]);
  }
  if (!d.firmware_version && d.last_seen) out.push(["info", "support.d_no_firmware"]);
  if (!out.length) out.push(["ok", "support.d_ok"]);
  return out;
}

export default async function support(args = []) {
  if (args[0] === "tickets") return ticketsTab(args[1] || null);
  const list = await products();
  const names = new Map(list.map((p) => [p.product_id, p]));
  const register = can("device.register");
  const query = h("input", { class: "search", type: "search", dir: "ltr",
    placeholder: t("support.placeholder"), "aria-label": t("support.find") });
  const results = h("div", { class: "stack loose" });
  const find = h("button", { class: "btn primary sm" }, icon("search"), t("support.find"));

  function cardFor(d) {
    const product = names.get(d.product_id);
    const [conn, cls] = connection(d);
    const code = h("button", { class: "btn sm soft", disabled: !register || d.lifecycle === "active" ||
      d.lifecycle === "disabled" || Boolean(d.parent_device_id) }, t("support.new_code"));
    code.addEventListener("click", busy(code, async () => {
      if (!await confirmAction(t("support.new_code_confirm"), t("support.new_code"))) return;
      const text = await api("POST", `/api/platform/products/${d.product_id}/labels`,
        { device_ids: [d.device_id] }, { as: "text" });
      const row = text.trim().split(/\r?\n/)[1]?.split(",") || [];
      await dialog(t("support.new_code"), h("div", { class: "stack" },
        h("p", { class: "small muted" }, t("support.new_code_help")), h("div", { class: "code-box" }, row[2] || "—")));
    }));
    const reset = h("button", { class: "btn sm danger", disabled: !register || Boolean(d.parent_device_id) },
      icon("reset"), t("device.reset"));
    reset.addEventListener("click", busy(reset, async () => {
      if (!await confirmAction(t("device.reset_confirm"), t("device.reset"), true)) return;
      const r = await api("POST", `/api/platform/devices/${d.device_id}/reset`);
      await dialog(t("device.reset_done"), h("div", { class: "stack" },
        h("p", { class: "small muted" }, t("device.new_code")), h("div", { class: "code-box" }, r.claim_code)));
      search();
    }));
    const block = h("button", { class: "btn sm", disabled: !register || d.lifecycle === "active" },
      t(d.lifecycle === "disabled" ? "support.unblock" : "support.block"));
    block.addEventListener("click", busy(block, async () => {
      const disabling = d.lifecycle !== "disabled";
      if (disabling && !await confirmAction(t("device.disable_confirm"), t("support.block"), true)) return;
      await api("PATCH", `/api/platform/devices/${d.device_id}`, { lifecycle: disabling ? "disabled" : "ready" });
      toast(t("common.saved"), "ok");
      search();
    }));
    return h("section", { class: "card pad-lg" },
      h("div", { class: "row between wrap" },
        h("div", { class: "stack tight" }, h("span", { class: "h2 ltr mono accent-text" }, d.identifier),
          h("span", { class: "small muted" }, product ? `${product.name} · ${product.code}` : d.product_code)),
        h("div", { class: "row wrap" }, lifecycleChip(d.lifecycle),
          h("span", { class: `chip ${d.state === "online" ? "teal" : ""}` }, h("span", { class: "dot" }), conn))),
      h("div", { class: "stack" }, diagnose(d).map(([kind, key]) =>
        h("div", { class: `note ${kind === "warn" ? "warn" : kind === "ok" ? "ok" : ""}` }, t(key)))),
      h("div", { class: "form-grid" },
        h("div", { class: "kv" }, h("span", {}, t("production.col_serial")), h("span", { class: "ltr mono" }, d.serial_number || "—")),
        h("div", { class: "kv" }, h("span", {}, t("device.last_seen")), h("span", { class: cls },
          d.last_seen ? `${date(d.last_seen, true)} (${ago(d.last_seen)})` : "—")),
        h("div", { class: "kv" }, h("span", {}, t("production.col_fw")), h("span", { class: "ltr" }, d.firmware_version || "—")),
        h("div", { class: "kv" }, h("span", {}, t("device.protocol")), h("span", { class: "ltr" }, t(`protocol.${d.protocol}`))),
        h("div", { class: "kv" }, h("span", {}, t("menu.warranty")), h("span", {}, warrantyText(d))),
        h("div", { class: "kv" }, h("span", {}, t("production.col_owner")), h("span", {}, ownership(d)))),
      h("div", { class: "row wrap" }, h("a", { class: "btn sm", href: `#/device/${d.device_id}` }, t("support.open")),
        code, block, reset));
  }

  async function search() {
    const q = query.value.trim();
    if (!q) return;
    const page = await api("GET", `/api/platform/devices?q=${encodeURIComponent(q)}&limit=10`);
    results.replaceChildren(...(page.devices.length ? page.devices.map(cardFor)
      : [h("div", { class: "card empty" }, h("p", {}, t("support.not_found")))]));
  }
  find.addEventListener("click", busy(find, search));
  query.addEventListener("keydown", (ev) => { if (ev.key === "Enter") busy(find, search)(ev); });

  return h("div", { class: "stack loose" },
    pageHead(t("menu.support"), t("support.subtitle")), tabs("lookup"),
    h("div", { class: "card pad" }, h("div", { class: "row wrap" }, query, find),
      register ? null : h("span", { class: "help" }, t("support.read_only"))),
    results,
    h("section", { class: "card pad" }, h("span", { class: "h3" }, t("support.guide")),
      [1, 2, 3, 4].map((n) => h("div", { class: "kv" }, h("span", {}, t(`support.g${n}_q`)),
        h("span", { class: "sub" }, t(`support.g${n}_a`))))));
}

function tabs(active) {
  return h("div", { class: "tabs", role: "tablist" },
    h("a", { class: `tab ${active === "lookup" ? "on" : ""}`, href: "#/support", role: "tab" }, t("support.tab_lookup")),
    h("a", { class: `tab ${active === "tickets" ? "on" : ""}`, href: "#/support/tickets", role: "tab" },
      t("support.tab_tickets")));
}

const TICKET_CHIP = { open: "orange", answered: "accent", in_progress: "violet", approved: "teal",
  rejected: "orange", closed: "" };

/** Questions and warranty claims from buyers: a list, one ticket with its messages, the answer. */
async function ticketsTab(selected) {
  let filter = "open";
  const tbody = h("tbody");
  const detail = h("div", { class: "col-side" });
  const filters = h("div", { class: "filters" });
  async function drawList() {
    filters.replaceChildren(...[["open", t("tickets.f_open")], ["", t("tickets.f_all")],
      ["approved", t("tickets.status_approved")], ["closed", t("tickets.status_closed")]].map(([v, label]) =>
      h("button", { class: `filter ${filter === v ? "on" : ""}`, onclick: () => { filter = v; drawList(); } }, label)));
    const rows = await api("GET", `/api/platform/tickets${filter ? `?status=${filter}` : ""}`);
    tbody.replaceChildren(...(rows.length ? rows.map((r) => h("tr", {
      class: `click ${String(r.ticket_id) === String(selected) ? "sel" : ""}`,
      onclick: () => { selected = r.ticket_id; history.replaceState(null, "", `#/support/tickets/${r.ticket_id}`); drawList(); drawDetail(); } },
    h("td", {}, h("span", { class: "stack tight" }, h("span", { class: "strong" }, r.subject),
      h("span", { class: "tiny muted" }, r.opened_by_name))),
    h("td", {}, h("span", { class: `chip sm ${r.kind === "warranty" ? "violet" : ""}` }, t(`tickets.kind_${r.kind}`))),
    h("td", { class: "id ltr" }, r.identifier),
    h("td", {}, h("span", { class: `chip sm ${TICKET_CHIP[r.status] || ""}` }, t(`tickets.status_${r.status}`))),
    h("td", { class: "sub" }, ago(r.updated_at)),
    h("td", { class: "sub" }, r.last_side === "buyer" ? h("span", { class: "chip sm orange" }, t("tickets.waiting")) : "—")))
      : [h("tr", {}, h("td", { colspan: 6, class: "muted" }, t("tickets.none")))]));
  }
  async function drawDetail() {
    if (!selected) {
      detail.replaceChildren(h("section", { class: "card pad" }, h("p", { class: "small muted" }, t("tickets.pick"))));
      return;
    }
    let data;
    try { data = await api("GET", `/api/platform/tickets/${selected}`); }
    catch { detail.replaceChildren(); return; }
    const tk = data.ticket;
    const reply = h("textarea", { class: "input", rows: 4, placeholder: t("tickets.reply_hint") });
    const options = [["answered", t("tickets.status_answered")], ["in_progress", t("tickets.status_in_progress")],
      ...(tk.kind === "warranty" ? [["approved", t("tickets.status_approved")], ["rejected", t("tickets.status_rejected")]] : []),
      ["closed", t("tickets.status_closed")]];
    const status = h("select", { class: "input" }, options.map(([v, label]) => h("option", { value: v }, label)));
    const send = h("button", { class: "btn primary" }, t("tickets.send"));
    send.addEventListener("click", busy(send, async () => {
      if (!reply.value.trim()) { toast(t("tickets.need_text"), "err"); return; }
      await api("POST", `/api/platform/tickets/${selected}/messages`, { body: reply.value.trim(), status: status.value });
      toast(t("tickets.answered"), "ok");
      drawList();
      drawDetail();
    }));
    detail.replaceChildren(h("section", { class: "card pad" },
      h("div", { class: "row between wrap" }, h("span", { class: "h3" }, tk.subject),
        h("span", { class: `chip ${TICKET_CHIP[tk.status] || ""}` }, t(`tickets.status_${tk.status}`))),
      h("div", { class: "kv" }, h("span", {}, t("production.product")), h("span", {}, tk.product_name)),
      h("div", { class: "kv" }, h("span", {}, t("production.col_id")),
        h("a", { class: "ltr mono", href: `#/device/${tk.device_id}` }, tk.identifier)),
      h("div", { class: "kv" }, h("span", {}, t("tickets.from")), h("span", {}, tk.opened_by_name)),
      h("div", { class: "stack" }, data.messages.map((m) => h("div", { class: `note ${m.side === "maker" ? "ok" : ""}` },
        h("span", { class: "tiny muted" }, `${m.side === "maker" ? m.author_name : tk.opened_by_name} · ${date(m.created_at, true)}`),
        h("span", { class: "pre" }, m.body)))),
      field(t("tickets.reply"), reply), field(t("tickets.status"), status), send,
      h("span", { class: "help" }, t("tickets.privacy"))));
  }
  await drawList();
  await drawDetail();
  return h("div", { class: "stack loose" }, pageHead(t("menu.support"), t("support.subtitle")), tabs("tickets"),
    h("div", { class: "columns" },
      h("section", { class: "card col-main" }, h("div", { class: "bar" }, h("span", { class: "h3" }, t("support.tab_tickets")), filters),
        h("div", { class: "scroll" }, h("table", { class: "grid mid" },
          h("thead", {}, h("tr", {}, ["subject", "kind", "device", "status", "updated", "waiting"].map((c) =>
            h("th", {}, t(`tickets.col_${c}`))))), tbody))),
      detail));
}
