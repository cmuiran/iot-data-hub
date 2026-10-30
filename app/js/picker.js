// Every <select> in the app opens the app's own dark sheet instead of the browser's list (which
// some browsers draw white whatever the page asks). The <select> stays the source of truth:
// a choice sets its value and fires "change", so the code that built it does not know the
// difference. Selects made later (dialogs, redraws) are picked up by an observer.

// (no imports: the app and the maker panel each pass their own h / icon / t and sheet class)
const DONE = new WeakSet();
let open = null;
let ui = null;            // { h, icon, t, sheetClass }

function label(sel) {
  const o = sel.options[sel.selectedIndex];
  return o ? o.textContent : "";
}

function sheet(sel, face) {
  if (open) open.close();
  const { h, icon, t } = ui;
  const box = h("dialog", { class: `${ui.sheetClass} picker` });
  const close = () => { box.close(); box.remove(); open = null; face.focus(); };
  open = { close };
  const title = sel.getAttribute("aria-label") || sel.closest(".field")?.querySelector(".label, label")?.textContent || "";
  const items = [...sel.options].map((o, i) => {
    const chosen = i === sel.selectedIndex;
    return h("button", { type: "button", class: `pick-item ${chosen ? "on" : ""}`, role: "option",
      "aria-selected": chosen, disabled: o.disabled, onclick: () => {
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        try { navigator.vibrate?.(8); } catch { /* none */ }
        sync(sel, face);
        close();
      } }, h("span", { class: "grow" }, o.textContent), chosen ? icon("check") : null);
  });
  box.append(h("div", { class: "stack" },
    h("div", { class: "row between" }, h("h2", { class: "h2" }, title || t("common.choose")),
      h("button", { type: "button", class: "icon-btn", "aria-label": t("common.close"), onclick: close }, icon("x"))),
    h("div", { class: "pick-list", role: "listbox" }, items)));
  box.addEventListener("cancel", (ev) => { ev.preventDefault(); close(); });
  box.addEventListener("click", (ev) => { if (ev.target === box) close(); });   // the backdrop
  document.body.append(box);
  box.showModal();
  box.querySelector(".pick-item.on")?.scrollIntoView({ block: "center" });
}

function sync(sel, face) {
  face.querySelector(".pick-text").textContent = label(sel);
  face.disabled = sel.disabled;
}

function enhance(sel) {
  if (DONE.has(sel) || sel.multiple || sel.size > 1) return;
  const { h, icon } = ui;
  DONE.add(sel);
  const face = h("button", { type: "button", class: `${sel.className} pick-face`, "aria-haspopup": "listbox",
    "aria-label": sel.getAttribute("aria-label") || null },
  h("span", { class: "pick-text" }), icon("chevron"));
  face.addEventListener("click", () => sheet(sel, face));
  sel.classList.add("pick-hidden");
  sel.tabIndex = -1;
  sel.setAttribute("aria-hidden", "true");
  sel.after(face);
  sync(sel, face);
  // code that sets .value / .selectedIndex directly, or rebuilds the options, is followed too
  const proto = HTMLSelectElement.prototype;
  for (const prop of ["value", "selectedIndex", "disabled"]) {
    const desc = Object.getOwnPropertyDescriptor(proto, prop);
    Object.defineProperty(sel, prop, { configurable: true, get() { return desc.get.call(this); },
      set(v) { desc.set.call(this, v); sync(sel, face); } });
  }
  sel.addEventListener("change", () => sync(sel, face));
  new MutationObserver(() => sync(sel, face)).observe(sel, { childList: true, subtree: true, attributes: true });
}

export function startPickers(deps, root = document.body) {
  ui = deps;
  for (const sel of root.querySelectorAll("select")) enhance(sel);
  new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.tagName === "SELECT") enhance(n);
        else for (const sel of n.querySelectorAll?.("select") || []) enhance(sel);
      }
    }
  }).observe(root, { childList: true, subtree: true });
}
