// Comments tab: the new-comment form, the list by status, and margin markers on the page.
import { api, errorText, q } from "./api.js";
import { $, h, show, toast } from "./ui.js";

const STATUS_ORDER = ["open", "needs_input", "done", "declined"];
const STATUS_LABEL = { open: "Open", needs_input: "Needs input", done: "Done", declined: "Declined" };
const MARKED = new Set(["open", "needs_input"]);

let target = null; // {pointer, element, quote} for the next comment, null = whole document
let items = [];
let shown = ""; // JSON of the list as last rendered
let editingId = null; // a comment whose inline edit form is open
let refreshSeq = 0;

export function init(ctx) {
  $("#comment-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const box = $("#comment-instruction");
    const instruction = box.value.trim();
    if (!instruction) {
      box.focus();
      return;
    }
    const r = await api("POST", "/api/requests", {
      pointer: target?.pointer ?? null,
      element: target?.element ?? "document",
      quote: target?.quote ?? "",
      category: $("#comment-category").value,
      scope: $("#comment-scope").value,
      instruction,
    });
    if (r.status !== 200) {
      toast(errorText(r.data), "error");
      return;
    }
    box.value = "";
    setTarget(ctx, null, false);
    refresh(ctx);
  });
  $("#comment-clear").addEventListener("click", () => setTarget(ctx, null));
}

export function setTarget(ctx, next, focus = true) {
  target = next;
  const label = $("#comment-target");
  if (next) {
    const where = next.pointer ? `${next.element} ${next.pointer}` : next.element;
    label.replaceChildren(
      h("span", { text: `On ${where}` }),
      next.quote ? h("q", { text: next.quote }) : "",
    );
  } else {
    label.textContent = "Whole document";
  }
  show($("#comment-clear"), Boolean(next));
  if (focus) {
    ctx.showTab("comments");
    $("#comment-instruction").focus();
  }
}

// Whether a pointer still names something on the page (itself or a field inside it).
function resolves(ctx, pointer) {
  return ctx.pointers.some((p) => p === pointer || p.startsWith(`${pointer}/`));
}

export async function refresh(ctx) {
  const seq = ++refreshSeq;
  const r = await api("GET", "/api/requests");
  if (seq !== refreshSeq || r.status !== 200 || !Array.isArray(r.data)) return;
  items = r.data;
  sendMarkers(ctx);
  render(ctx);
}

export function pageLoaded(ctx) {
  sendMarkers(ctx);
  render(ctx, true);
}

function sendMarkers(ctx) {
  const counts = {};
  for (const item of items) {
    if (typeof item.pointer === "string" && MARKED.has(item.status)) {
      counts[item.pointer] = (counts[item.pointer] || 0) + 1;
    }
  }
  ctx.send({ type: "comments", counts });
}

function render(ctx, force = false) {
  const key = JSON.stringify([items, ctx.pageReady ? ctx.pointers : null]);
  if (editingId || (!force && key === shown)) return;
  shown = key;
  const list = $("#comment-list");
  const statuses = [...STATUS_ORDER, ...new Set(items.map((i) => i.status).filter((s) => !STATUS_ORDER.includes(s)))];
  const groups = [];
  for (const status of statuses) {
    const group = items.filter((i) => i.status === status);
    if (!group.length) continue;
    groups.push(
      h("section", { class: `comment-group ${status}` },
        h("h3", { text: `${STATUS_LABEL[status] ?? status} (${group.length})` }),
        h("ul", {}, group.map((item) => renderItem(ctx, item)))),
    );
  }
  list.replaceChildren(...(groups.length ? groups : [h("p", { class: "muted", text: "No comments yet." })]));
}

function renderItem(ctx, item) {
  const detached = typeof item.pointer === "string" && ctx.pageReady && !resolves(ctx, item.pointer);
  const actions = [];
  if (item.status === "open" || item.status === "needs_input") {
    actions.push(h("button", { type: "button", text: "Edit", onclick: () => openEditor(ctx, li, item) }));
    actions.push(h("button", { type: "button", text: "Delete", onclick: () => remove(ctx, item) }));
  } else {
    actions.push(h("button", { type: "button", text: "Reopen", onclick: () => update(ctx, item, { status: "open" }) }));
  }
  const li = h("li", { class: "comment-item", "data-id": item.id },
    h("div", { class: "meta" },
      h("span", { class: "status", text: item.status }),
      h("span", { class: "category", text: item.category }),
      h("span", { class: "scope", text: item.scope }),
      detached ? h("span", { class: "tag detached", text: "detached", title: "This element is no longer in the document" }) : "",
      item.pointer
        ? h("button", { type: "button", class: "link pointer", text: item.pointer, title: "Show on the page",
            onclick: () => ctx.send({ type: "reveal", pointer: item.pointer }) })
        : h("span", { class: "pointer", text: "whole document" })),
    h("p", { class: "instruction", text: item.instruction }),
    item.quote ? h("blockquote", { class: "quote", text: item.quote }) : "",
    item.agent_note ? h("p", { class: "agent-note", text: `Agent: ${item.agent_note}` }) : "",
    h("div", { class: "actions" }, actions));
  return li;
}

function openEditor(ctx, li, item) {
  editingId = item.id;
  const instruction = h("textarea", { rows: 3, "aria-label": "Instruction" });
  instruction.value = item.instruction ?? "";
  const select = (name, options) => {
    const node = h("select", { "aria-label": name }, options.map((o) => h("option", { value: o, text: o })));
    node.value = item[name];
    return node;
  };
  const category = item.category === "figure" ? null : select("category", ["format", "text"]);
  const scope = select("scope", ["document", "house_style"]);
  const close = () => {
    editingId = null;
    render(ctx, true);
  };
  const save = async () => {
    const fields = { instruction: instruction.value.trim(), scope: scope.value };
    if (category) fields.category = category.value;
    if (item.status === "needs_input") fields.status = "open"; // the user answered
    editingId = null;
    if (await update(ctx, item, fields)) return;
    editingId = item.id;
  };
  li.replaceChildren(
    h("div", { class: "meta" }, h("span", { class: "status", text: item.status })),
    instruction,
    h("div", { class: "row" }, category ?? "", scope,
      h("button", { type: "button", text: "Save", onclick: save }),
      h("button", { type: "button", text: "Cancel", onclick: close })),
  );
  instruction.focus();
}

async function update(ctx, item, fields) {
  const r = await api("PATCH", `/api/requests/${q(item.id)}`, fields);
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return false;
  }
  await refresh(ctx);
  return true;
}

async function remove(ctx, item) {
  const r = await api("DELETE", `/api/requests/${q(item.id)}`);
  if (r.status !== 200) toast(errorText(r.data), "error");
  refresh(ctx);
}

// Scroll the list to the comments on a pointer (a margin marker was clicked).
export function reveal(pointer) {
  const ids = new Set(items.filter((i) => i.pointer === pointer).map((i) => i.id));
  const nodes = [...document.querySelectorAll(".comment-item")].filter((n) => ids.has(n.dataset.id));
  if (!nodes.length) return;
  document.querySelector("#tab-comments").click();
  nodes[0].scrollIntoView({ block: "nearest" });
  for (const n of nodes) {
    n.classList.add("flash");
    setTimeout(() => n.classList.remove("flash"), 1500);
  }
}
