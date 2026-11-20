// Right panel: tabs, Element (chart fields and raw editor), Checks, Pages and Log.
import { api, errorText, q } from "./api.js";
import { $, h, show, toast } from "./ui.js";

const LOG_LINES = 5000;
const tabs = () => [...document.querySelectorAll('#tabs [role="tab"]')];
let liveIssues = [];
let stateIssues = [];

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
// A field: {pointer, old, hash, loaded, input, issues, conflict}. `old` and `hash` are what
// a save sends; `loaded` is the text the input was filled with (to tell unsaved text).
async function loadValue(pointer) {
  const r = await api("GET", `/api/value?pointer=${q(pointer)}`);
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return null;
  }
  return r.data; // {value, hash}
}

const asText = (value) => (value === null || value === undefined ? "" : String(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const dirty = (field) => field.input.value !== field.loaded;

function makeField(pointer, current, input) {
  input.value = asText(current.value);
  return {
    pointer, old: current.value, hash: current.hash, loaded: input.value, input,
    issues: h("ul", { class: "issues" }), conflict: h("div", { class: "conflict", hidden: true }),
  };
}

function fill(field, fresh) {
  Object.assign(field, { old: fresh.value, hash: fresh.hash });
  field.input.value = field.loaded = asText(fresh.value);
  show(field.conflict, false);
}

// The field's value changed outside the editor while the user has unsaved text: keep the
// text, show the file's value, and let a second Save replace it deliberately.
function showConflict(field, fresh) {
  Object.assign(field, { old: fresh.value, hash: fresh.hash });
  field.conflict.replaceChildren(
    h("strong", { text: "Changed outside the editor." }), " The file now has:",
    h("pre", { class: "current", text: asText(fresh.value) }),
    "Your text is kept above and was not saved. Save again to replace the file's text with it.",
  );
  show(field.conflict, true);
}

// Bring a field up to date with the file after the file changed.
async function sync(field) {
  const fresh = await loadValue(field.pointer);
  if (!fresh) return;
  if (same(fresh.value, field.old)) field.hash = fresh.hash;
  else if (!dirty(field)) fill(field, fresh);
  else showConflict(field, fresh);
}

// Patch one field; returns true on success.
async function savePatch(ctx, field, retried = false) {
  const text = field.input.value;
  const r = await api("POST", "/api/patch", {
    pointer: field.pointer, old: field.old, new: text, source_hash: field.hash,
  });
  field.issues.replaceChildren();
  if (r.status === 200) {
    show(field.conflict, false);
    field.hash = r.data.hash;
    ctx.highlight = [];
    ctx.historyMoved?.();
    if (r.data.figure_request) toast("Figure changed – the agent will reconcile facts and calculations", "warn", 10000);
    ctx.reloadDoc();
    return true;
  }
  if (r.status === 409 && r.data.error === "stale") {
    const fresh = await loadValue(field.pointer);
    if (!fresh) return false;
    if (same(fresh.value, field.old) && !retried) {
      field.hash = fresh.hash; // another field changed; this one did not
      return savePatch(ctx, field, true);
    }
    showConflict(field, fresh);
    toast("This field changed outside the editor; your text was not saved", "warn");
    return false;
  }
  if (r.status === 422) {
    field.issues.replaceChildren(...(r.data.issues || [r.data]).map((i) => h("li", { text: i.message })));
    return false;
  }
  toast(errorText(r.data), "error");
  return false;
}

const discardOk = (fields) =>
  !fields.some(dirty) || window.confirm("Discard the unsaved text in the Element tab?");

// -- Element tab: chart fields ------------------------------------------------
let fieldsPointer = null;
let fields = [];

export async function openFields(ctx, pointer) {
  if (pointer === fieldsPointer && fields.some(dirty)) return showTab("element");
  if (!discardOk(fields)) return showTab("element");
  const r = await api("GET", `/api/fields?pointer=${q(pointer)}`);
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return;
  }
  show($("#element-empty"), false);
  const next = [];
  const rows = [];
  for (const spec of r.data) {
    const current = await loadValue(spec.pointer);
    if (!current) continue;
    const input = spec.kind === "inline" ? h("textarea", { rows: 2 }) : h("input", { type: "text" });
    const field = makeField(spec.pointer, current, input);
    next.push(field);
    const save = async () => {
      if (!(await savePatch(ctx, field))) return;
      field.loaded = field.input.value;
      for (const other of fields) await sync(other); // the file changed: refresh the others
    };
    input.id = `field-${rows.length}`;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.tagName === "INPUT") save();
    });
    rows.push(h("div", { class: "field", "data-pointer": spec.pointer },
      h("label", { for: input.id, text: spec.label }), input,
      h("button", { type: "button", text: "Save", onclick: save }), field.issues, field.conflict));
  }
  fieldsPointer = pointer;
  fields = next;
  $("#element-fields").replaceChildren(
    h("h3", {}, "Fields of ", h("code", { text: pointer })),
    ...(rows.length ? rows : [h("p", { class: "muted", text: "This element has no editable fields." })]),
  );
  showTab("element");
}

// -- Element tab: raw editor --------------------------------------------------
let raw = null; // the field in the raw editor

// preset: {markup, old, hash} when the page hands over an edit the converter could not represent.
export async function openRaw(ctx, pointer, reason, preset = null) {
  const editor = $("#raw-editor");
  const textarea = $("textarea", editor);
  if (raw && dirty(raw)) {
    const replaces = raw.pointer !== pointer || (preset && preset.markup !== textarea.value);
    if (!replaces || !window.confirm("Replace the unsaved text in the raw editor?")) return showTab("element");
  }
  const current = preset ? { value: preset.old, hash: preset.hash } : await loadValue(pointer);
  if (!current) return;
  raw = makeField(pointer, current, textarea);
  if (preset) {
    textarea.value = preset.markup; // the user's current text, unsaved
    raw.loaded = asText(preset.old);
  }
  $(".pointer", editor).textContent = pointer;
  $(".reason", editor).textContent = reason
    ? `The edit could not be converted (${reason}). Finish it here; Save writes this text.`
    : "Edit the source text, including ** and * markup.";
  $(".issues", editor).replaceWith(raw.issues);
  $(".conflict", editor).replaceWith(raw.conflict);
  show(editor, true);
  showTab("element");
  textarea.focus();
}

async function saveRaw(ctx) {
  if (!raw) return;
  if (!(await savePatch(ctx, raw))) return;
  raw = null;
  show($("#raw-editor"), false);
  for (const field of fields) await sync(field);
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
// The preview a warning names: a page file name in its text or pointer, or "page N".
function namedPage(warning, pages) {
  const text = `${warning.message} ${warning.pointer ?? ""}`;
  const byName = pages.find((name) => text.includes(name));
  if (byName) return byName;
  const n = /\bpage\s+(\d+)\b/i.exec(text);
  return n && pages[Number(n[1]) - 1] ? pages[Number(n[1]) - 1] : null;
}

function revealPage(name) {
  const figure = [...document.querySelectorAll("#page-previews figure")].find((f) => f.dataset.page === name);
  if (!figure) return;
  for (const f of document.querySelectorAll("#page-previews figure.target")) f.classList.remove("target");
  figure.classList.add("target");
  figure.scrollIntoView({ block: "start" });
}

export function showBuild(report) {
  show($("#pages-empty"), !report.pages.length);
  $("#pages-empty").textContent = report.pages.length ? "" : "The build produced no page previews.";
  $("#build-warnings").replaceChildren(...report.warnings.map((w) => {
    const page = namedPage(w, report.pages);
    return h("li", { class: w.severity || "warning" },
      h("span", { text: w.message }), w.pointer ? h("code", { text: w.pointer }) : "",
      page ? h("button", { type: "button", class: "link page-link", text: `Show ${page}`, onclick: () => revealPage(page) }) : "");
  }));
  $("#page-previews").replaceChildren(...report.pages.map((name) =>
    h("figure", { "data-page": name },
      h("img", { src: `/api/pages/${q(name)}?v=${Date.now()}`, alt: `Page preview ${name}`, loading: "lazy" }),
      h("figcaption", { text: name }))));
  toast(report.ok ? "Build finished" : "Build failed – see the Log tab", report.ok ? "info" : "error");
  showTab("pages");
}
