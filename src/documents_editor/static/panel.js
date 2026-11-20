// Right panel: tabs, Element (chart fields and raw editor), Checks, Pages and Log.
import { api, errorText, q } from "./api.js";
import { $, h, show, toast } from "./ui.js";

const LOG_LINES = 5000;
const tabs = () => [...document.querySelectorAll('#tabs [role="tab"]')];
let liveIssues = [];
let stateIssues = [];
let raw = null; // {pointer, old, hash} of the field in the raw editor

export function init(ctx) {
  ctx.showTab = showTab;
  const list = $("#tabs");
  list.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) showTab(tab.id.slice(4), true);
  });
  list.addEventListener("keydown", (event) => {
    const all = tabs();
    const index = all.indexOf(document.activeElement);
    if (index < 0) return;
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: all.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = all[(moves[event.key] + all.length) % all.length];
    showTab(next.id.slice(4), true);
  });
  $("#raw-save").addEventListener("click", () => saveRaw(ctx));
  $("#raw-cancel").addEventListener("click", closeRaw);
  $("#check-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-pointer]");
    if (item) ctx.send({ type: "reveal", pointer: item.dataset.pointer });
  });
}

export function showTab(name, focus = false) {
  for (const tab of tabs()) {
    const selected = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`#${tab.getAttribute("aria-controls")}`).hidden = !selected;
    if (selected && focus) tab.focus();
  }
}

// -- saving a single field from the panel -------------------------------------
async function loadValue(pointer) {
  const r = await api("GET", `/api/value?pointer=${q(pointer)}`);
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return null;
  }
  return r.data; // {value, hash}
}

// Patch one field; returns true on success, else shows the problem in `issuesList`.
async function savePatch(ctx, field, text, issuesList) {
  const r = await api("POST", "/api/patch", {
    pointer: field.pointer, old: field.old, new: text, source_hash: field.hash,
  });
  issuesList.replaceChildren();
  if (r.status === 200) {
    ctx.highlight = [];
    if (r.data.figure_request) toast("Figure changed – the agent will reconcile facts and calculations", "warn", 10000);
    ctx.reloadDoc();
    return true;
  }
  if (r.status === 409 && r.data.error === "stale") {
    toast("The file changed outside the editor; the field was reloaded", "warn");
    const fresh = await loadValue(field.pointer);
    if (fresh) Object.assign(field, { old: fresh.value, hash: fresh.hash });
    return false;
  }
  if (r.status === 422) {
    issuesList.replaceChildren(...(r.data.issues || [r.data]).map((i) => h("li", { text: i.message })));
    return false;
  }
  toast(errorText(r.data), "error");
  return false;
}

const asText = (value) => (value === null || value === undefined ? "" : String(value));

// -- Element tab: chart fields ------------------------------------------------
export async function openFields(ctx, pointer) {
  closeRaw();
  const r = await api("GET", `/api/fields?pointer=${q(pointer)}`);
  const box = $("#element-fields");
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return;
  }
  show($("#element-empty"), false);
  const rows = [];
  for (const spec of r.data) {
    const current = await loadValue(spec.pointer);
    if (!current) continue;
    const field = { pointer: spec.pointer, old: current.value, hash: current.hash };
    const input = spec.kind === "inline" ? h("textarea", { rows: 2 }) : h("input", { type: "text" });
    input.value = asText(current.value);
    const issues = h("ul", { class: "issues" });
    const save = async () => {
      if (await savePatch(ctx, field, input.value, issues)) openFields(ctx, pointer);
    };
    const id = `field-${rows.length}`;
    input.id = id;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.tagName === "INPUT") save();
    });
    rows.push(h("div", { class: "field", "data-pointer": spec.pointer },
      h("label", { for: id, text: spec.label }), input,
      h("button", { type: "button", text: "Save", onclick: save }), issues));
  }
  box.replaceChildren(
    h("h3", {}, "Fields of ", h("code", { text: pointer })),
    ...(rows.length ? rows : [h("p", { class: "muted", text: "This element has no editable fields." })]),
  );
  showTab("element");
}

// -- Element tab: raw editor --------------------------------------------------
export async function openRaw(ctx, pointer, reason) {
  const current = await loadValue(pointer);
  if (!current) return;
  raw = { pointer, old: current.value, hash: current.hash };
  const editor = $("#raw-editor");
  $(".pointer", editor).textContent = pointer;
  $(".reason", editor).textContent = reason
    ? `The edit could not be converted (${reason}). Edit the source text here instead.`
    : "Edit the source text, including ** and * markup.";
  $("textarea", editor).value = asText(current.value);
  $(".issues", editor).replaceChildren();
  show(editor, true);
  showTab("element");
  $("textarea", editor).focus();
}

async function saveRaw(ctx) {
  if (!raw) return;
  const editor = $("#raw-editor");
  if (await savePatch(ctx, raw, $("textarea", editor).value, $(".issues", editor))) closeRaw();
}

function closeRaw() {
  raw = null;
  show($("#raw-editor"), false);
}

// -- Checks tab ---------------------------------------------------------------
export function setStateIssues(ctx, issues) {
  stateIssues = issues;
  renderChecks();
}

export function setLiveIssues(issues) {
  liveIssues = issues;
  renderChecks();
}

function renderChecks() {
  const item = (issue) =>
    h("li", { class: issue.severity || "error", "data-pointer": issue.pointer || null },
      h("span", { text: issue.message }), issue.pointer ? h("code", { text: issue.pointer }) : "");
  const nodes = [];
  if (liveIssues.length) nodes.push(h("li", { class: "heading", text: "While typing" }), ...liveIssues.map(item));
  if (stateIssues.length) nodes.push(h("li", { class: "heading", text: "Document" }), ...stateIssues.map(item));
  $("#check-list").replaceChildren(...(nodes.length ? nodes : [h("li", { class: "muted", text: "No issues." })]));
  const badge = $("#issue-count");
  badge.textContent = String(stateIssues.length + liveIssues.length);
  show(badge, stateIssues.length + liveIssues.length > 0);
}

// -- Log tab ------------------------------------------------------------------
export function startLog(job) {
  appendLog(`── ${job} started ${new Date().toLocaleTimeString()} ──`);
}

export function appendLog(line) {
  const log = $("#log");
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.append(`${line}\n`);
  while (log.childNodes.length > LOG_LINES) log.firstChild.remove();
  if (atEnd) log.scrollTop = log.scrollHeight;
}

// -- Pages tab ----------------------------------------------------------------
export function showBuild(report) {
  show($("#pages-empty"), !report.pages.length);
  $("#pages-empty").textContent = report.pages.length ? "" : "The build produced no page previews.";
  $("#build-warnings").replaceChildren(...report.warnings.map((w) =>
    h("li", { class: w.severity || "warning" }, h("span", { text: w.message }), w.pointer ? h("code", { text: w.pointer }) : "")));
  $("#page-previews").replaceChildren(...report.pages.map((name) =>
    h("figure", {}, h("img", { src: `/api/pages/${q(name)}?v=${Date.now()}`, alt: `Page preview ${name}`, loading: "lazy" }),
      h("figcaption", { text: name }))));
  toast(report.ok ? "Build finished" : "Build failed – see the Log tab", report.ok ? "info" : "error");
  showTab("pages");
}
