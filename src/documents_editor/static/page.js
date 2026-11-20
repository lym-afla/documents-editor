// Injected into the document page: in-place editing, toolbar, live checks, markers, highlights.
// Talks to the editor shell (the parent window) with postMessage, same origin only.
import { api, errorText, q } from "./api.js";
import { fromDom, toHtml } from "./markup.js";

const EDITABLE = new Set(["inline", "plain", "number-cell"]);
const CHECK_DELAY_MS = 300;
const QUOTE_MAX = 200;
const shell = window.parent;

let readOnly = false; // the server refuses edits anyway while locked
let edit = null; // {el, pointer, kind, original, raw, hash, savedHtml, abort, pending}
let starting = null; // token of an edit whose raw value is being fetched
let reloading = false; // a save succeeded; the shell is swapping the page
let reloadPending = false; // an earlier edit saved while another edit was open
let barTarget = null; // comment target of the toolbar outside edit mode
let checkTimer = 0;
let checkSeq = 0;
let lastClick = null;
let markerCounts = {};

function post(message) {
  if (shell !== window) shell.postMessage(message, location.origin);
}

const sourceOf = (node) => (node instanceof Element ? node : node?.parentElement)?.closest("[data-src]") ?? null;
const elementName = (el) => (el ? el.localName : "document");
const cut = (text) => text.replace(/\s+/g, " ").trim().slice(0, QUOTE_MAX);
const findPointer = (pointer) =>
  [...document.querySelectorAll("[data-src]")].find((el) => el.dataset.src === pointer) ??
  [...document.querySelectorAll("[data-src]")].find((el) => el.dataset.src.startsWith(`${pointer}/`)) ??
  null;

// -- toolbar ------------------------------------------------------------------
const toolbar = document.createElement("div");
toolbar.id = "de-toolbar";
toolbar.hidden = true;
toolbar.setAttribute("role", "toolbar");
toolbar.setAttribute("aria-label", "Edit");
toolbar.innerHTML =
  '<button type="button" class="de-bold" title="Bold (Ctrl+B)" aria-label="Bold"><b>B</b></button>' +
  '<button type="button" class="de-italic" title="Italic (Ctrl+I)" aria-label="Italic"><i>I</i></button>' +
  '<button type="button" class="de-comment">Comment</button>' +
  '<button type="button" class="de-raw" title="Edit the source text with its markup">Raw</button>';
toolbar.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus and selection
toolbar.addEventListener("click", onToolbar);

function showToolbar(anchorRect, mode, kind) {
  toolbar.querySelector(".de-bold").hidden = mode !== "edit" || kind !== "inline";
  toolbar.querySelector(".de-italic").hidden = mode !== "edit" || kind !== "inline";
  toolbar.querySelector(".de-raw").hidden = mode !== "edit";
  toolbar.hidden = false;
  const height = toolbar.offsetHeight;
  let top = window.scrollY + anchorRect.top - height - 6;
  if (top < window.scrollY) top = window.scrollY + anchorRect.bottom + 6;
  toolbar.style.top = `${top}px`;
  toolbar.style.left = `${Math.max(window.scrollX + 4, window.scrollX + anchorRect.left)}px`;
}

function hideToolbar() {
  toolbar.hidden = true;
  barTarget = null;
}

function showCommentBar(rect, target) {
  barTarget = target;
  showToolbar(rect, "comment");
}

async function onToolbar(event) {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.classList.contains("de-bold") || button.classList.contains("de-italic")) {
    format(button.classList.contains("de-bold") ? "bold" : "italic");
  } else if (button.classList.contains("de-comment")) {
    let target = barTarget;
    if (edit) {
      const selection = getSelection();
      const selected = !selection.isCollapsed && edit.el.contains(selection.anchorNode) ? selection.toString() : "";
      target = { pointer: edit.pointer, element: elementName(edit.el), quote: cut(selected || edit.el.textContent) };
      if (!(await commit())) return;
    }
    hideToolbar();
    if (target) post({ type: "comment", ...target });
  } else if (button.classList.contains("de-raw") && edit) {
    const pointer = edit.pointer;
    if (await commit()) post({ type: "raw", pointer, reason: "" });
  }
}

// -- issues under the element -------------------------------------------------
let issueBox = null;

function showIssues(el, issues) {
  if (!issues.length) return clearIssues();
  if (!issueBox) {
    issueBox = document.createElement("div");
    issueBox.className = "de-issue";
    issueBox.setAttribute("role", "alert");
    document.body.append(issueBox);
  }
  issueBox.replaceChildren(...issues.map((issue) => {
    const line = document.createElement("div");
    line.className = `de-issue-${issue.severity === "warning" ? "warning" : "error"}`;
    line.textContent = issue.message;
    return line;
  }));
  const rect = el.getBoundingClientRect();
  issueBox.style.top = `${window.scrollY + rect.bottom + 4}px`;
  issueBox.style.left = `${window.scrollX + rect.left}px`;
  issueBox.style.maxWidth = `${Math.max(rect.width, 240)}px`;
}

function clearIssues() {
  issueBox?.remove();
  issueBox = null;
}

// -- editing ------------------------------------------------------------------
function placeCaret(el) {
  const selection = getSelection();
  let range = null;
  if (lastClick && document.caretRangeFromPoint) {
    range = document.caretRangeFromPoint(lastClick.x, lastClick.y);
    if (range && !el.contains(range.startContainer)) range = null;
  }
  if (!range) {
    range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
  }
  selection.removeAllRanges();
  selection.addRange(range);
}

async function startEdit(el) {
  const token = {};
  starting = token;
  const pointer = el.dataset.src;
  const kind = el.dataset.edit;
  const r = await api("GET", `/api/value?pointer=${q(pointer)}`);
  if (starting !== token || edit || reloading || readOnly) return;
  starting = null;
  if (r.status !== 200) return post({ type: "toast", kind: "error", text: errorText(r.data) });
  const original = r.data.value;
  if (original !== null && typeof original === "object") {
    return post({ type: "toast", kind: "error", text: `${pointer} is not a single text field` });
  }
  const raw = original === null ? "" : String(original);
  const abort = new AbortController();
  edit = { el, pointer, kind, original, raw, hash: r.data.hash, savedHtml: el.innerHTML, abort, pending: null };
  el.innerHTML = toHtml(raw, kind);
  el.classList.add("de-editing");
  el.contentEditable = kind === "inline" ? "true" : "plaintext-only";
  const on = (type, handler) => el.addEventListener(type, handler, { signal: abort.signal });
  on("keydown", onKey);
  on("input", scheduleCheck);
  on("paste", onPaste);
  on("drop", (e) => e.preventDefault());
  on("blur", () => {
    if (edit?.el === el) commit();
  });
  el.focus();
  document.execCommand("styleWithCSS", false, false); // <b>/<i>, never styled spans
  placeCaret(el);
  showToolbar(el.getBoundingClientRect(), "edit", kind);
  post({ type: "editing", on: true, pointer });
}

function format(command) {
  if (!edit || edit.kind !== "inline") return;
  document.execCommand(command);
  scheduleCheck();
}

function onKey(event) {
  if (event.isComposing) return;
  const mod = (event.ctrlKey || event.metaKey) && !event.altKey;
  const key = event.key.toLowerCase();
  if (event.key === "Enter") {
    event.preventDefault();
    commit();
  } else if (event.key === "Escape") {
    event.preventDefault();
    finishEdit(true);
  } else if (mod && (key === "b" || key === "i")) {
    event.preventDefault();
    format(key === "b" ? "bold" : "italic");
  } else if (mod && key === "u") {
    event.preventDefault(); // underline has no markup
  }
}

function onPaste(event) {
  event.preventDefault();
  const text = (event.clipboardData?.getData("text/plain") ?? "").replace(/\r\n?|\n/g, " ");
  if (text) document.execCommand("insertText", false, text);
}

// End edit mode; restore puts the page's rendered HTML back.
function finishEdit(restore) {
  const e = edit;
  if (!e) return;
  edit = null;
  e.abort.abort();
  clearTimeout(checkTimer);
  e.el.removeAttribute("contenteditable");
  e.el.classList.remove("de-editing");
  if (restore) e.el.innerHTML = e.savedHtml;
  hideToolbar();
  clearIssues();
  post({ type: "editing", on: false });
  post({ type: "live-issues", issues: [] });
  if (reloadPending) {
    reloadPending = false;
    reloading = true;
    post({ type: "reload" });
  }
}

// Markup of a formatting element's content, nesting flattened. Footnote chips inside it are
// emitted as [^id] between closed emphasis runs, and edge spaces stay outside the markers,
// so the result is valid markup: <b>this year <chip a></b> -> **this year**[^a].
function emphasis(node, mark, text) {
  const parts = []; // strings, or {fn}
  const walk = (n) => {
    for (const c of n.childNodes) {
      if (c.nodeType === Node.TEXT_NODE) parts.push(text(c.data));
      else if (c.nodeType !== Node.ELEMENT_NODE) continue;
      else if (c.matches("span.de-fn")) parts.push({ fn: c.dataset.fn ?? "" });
      else if (c.matches("br")) parts.push(" ");
      else walk(c);
    }
  };
  walk(node);
  let out = "";
  let run = "";
  const flush = () => {
    const [, lead, body, trail] = /^(\s*)([\s\S]*?)(\s*)$/.exec(run);
    out += body ? `${lead}${mark}${body}${mark}${trail}` : run;
    run = "";
  };
  for (const part of parts) {
    if (typeof part === "string") run += part;
    else {
      flush();
      out += `[^${part.fn}]`;
    }
  }
  flush();
  return out;
}

// Best-effort markup of a DOM the converter rejected: nested formatting is flattened to
// the outer tag, other elements are reduced to their text, footnote chips stay [^id].
function looseMarkup(root, kind, raw) {
  const text = (s) => (raw.includes("\u00a0") ? s : s.replaceAll("\u00a0", " "));
  if (kind !== "inline") return text(root.textContent);
  let out = "";
  for (const node of root.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) out += text(node.data);
    else if (node.nodeType !== Node.ELEMENT_NODE) continue;
    else if (node.matches("span.de-fn")) out += `[^${node.dataset.fn ?? ""}]`;
    else if (node.matches("b, strong")) out += emphasis(node, "**", text);
    else if (node.matches("i, em")) out += emphasis(node, "*", text);
    else if (node.matches("br")) out += node.nextSibling ? "\n" : "";
    else out += text(node.textContent);
  }
  return out;
}

// The converter cannot represent the edit: keep editing, show why, and offer the raw editor
// with the current text. Its save uses this edit's original value and hash.
function handOver(e, reason) {
  const issues = [{ message: `${reason} – finish this edit in the raw editor, or press Esc to cancel`, severity: "error", pointer: e.pointer }];
  showIssues(e.el, issues);
  post({ type: "live-issues", issues });
  const markup = looseMarkup(e.el, e.kind, e.raw);
  if (markup === e.handedOver) return; // already offered (e.g. blur after Enter)
  e.handedOver = markup;
  post({ type: "raw", pointer: e.pointer, reason, markup, old: e.original, hash: e.hash });
}

// Commit the current edit. Resolves true when nothing is left unsaved.
function commit() {
  const e = edit;
  if (!e) return Promise.resolve(true);
  if (e.pending) return e.pending;
  const result = fromDom(e.el, e.kind, e.raw);
  if (!result.ok) {
    handOver(e, result.reason);
    return Promise.resolve(false);
  }
  if (result.markup === e.raw) {
    finishEdit(true);
    return Promise.resolve(true);
  }
  e.pending = save(e, result.markup).finally(() => {
    e.pending = null;
  });
  return e.pending;
}

async function save(e, markup) {
  const r = await api("POST", "/api/patch", { pointer: e.pointer, old: e.original, new: markup, source_hash: e.hash });
  if (r.status === 200) {
    // saved, even if the user pressed Esc meanwhile: the page must show the file, but not
    // by reloading under another edit the user has since started
    if (edit && edit !== e) {
      reloadPending = true;
      post({ type: "committed", pointer: e.pointer, figure_request: r.data.figure_request, reload: false });
      return true;
    }
    reloading = true;
    reloadPending = false;
    if (edit === e) {
      edit = null;
      e.abort.abort();
      clearTimeout(checkTimer);
      e.el.removeAttribute("contenteditable");
      e.el.classList.replace("de-editing", "de-saving");
      hideToolbar();
      clearIssues();
    }
    post({ type: "committed", pointer: e.pointer, figure_request: r.data.figure_request });
    return true;
  }
  if (edit !== e) return false;
  if (r.status === 422) {
    const issues = r.data.issues?.length ? r.data.issues : [{ message: errorText(r.data), severity: "error" }];
    showIssues(e.el, issues);
    post({ type: "live-issues", issues });
  } else if (r.status === 409 && r.data.error === "stale") {
    post({ type: "stale" });
  } else if (r.status === 409 && (r.data.error === "locked" || r.data.error === "busy")) {
    post({ type: "locked" });
  } else {
    post({ type: "toast", kind: "error", text: errorText(r.data) });
  }
  return false;
}

// -- live checks --------------------------------------------------------------
function scheduleCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, CHECK_DELAY_MS);
}

async function runCheck() {
  const e = edit;
  if (!e) return;
  const result = fromDom(e.el, e.kind, e.raw);
  const value = result.ok ? result.markup : e.el.textContent;
  const seq = ++checkSeq;
  const r = await api("POST", "/api/check", { pointer: e.pointer, value });
  if (seq !== checkSeq || edit !== e) return;
  const local = (result.ok ? result.issues : [result.reason]).map((message) => ({ message, severity: "warning", pointer: e.pointer }));
  const issues = [...local, ...(r.status === 200 ? r.data.issues : [])];
  showIssues(e.el, issues);
  post({ type: "live-issues", issues });
}

// -- clicks -------------------------------------------------------------------
document.addEventListener("click", (event) => {
  if (reloading || toolbar.contains(event.target)) return;
  if (event.target.closest(".de-marker")) return;
  const link = event.target.closest("a[href]");
  if (link && !link.getAttribute("href").startsWith("#")) event.preventDefault();
  if (edit) return; // clicks inside move the caret; outside, blur has committed
  lastClick = { x: event.clientX, y: event.clientY };
  const selection = getSelection();
  if (!selection.isCollapsed && selection.toString().trim()) {
    const host = sourceOf(selection.anchorNode);
    showCommentBar(selection.getRangeAt(0).getBoundingClientRect(),
      { pointer: host?.dataset.src ?? null, element: elementName(host), quote: cut(selection.toString()) });
    return;
  }
  hideToolbar();
  const el = sourceOf(event.target);
  if (!el) return;
  const kind = el.dataset.edit;
  const target = { pointer: el.dataset.src, element: elementName(el), quote: cut(el.textContent) };
  if (kind === "chart") post({ type: "chart", pointer: el.dataset.src });
  if (EDITABLE.has(kind) && !readOnly) {
    startEdit(el);
  } else {
    showCommentBar(el.getBoundingClientRect(), target); // none, chart, or read-only
  }
}, true);

// -- markers and highlights ---------------------------------------------------
const markerLayer = document.createElement("div");
markerLayer.id = "de-markers";

function renderMarkers() {
  const markers = [];
  for (const [pointer, count] of Object.entries(markerCounts)) {
    const el = findPointer(pointer);
    if (!el) continue;
    const rect = el.getBoundingClientRect();
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = "de-marker";
    marker.textContent = String(count);
    marker.title = `${count} open comment${count === 1 ? "" : "s"}`;
    marker.style.top = `${window.scrollY + rect.top}px`;
    marker.style.left = `${Math.max(2, window.scrollX + rect.left - 26)}px`;
    marker.addEventListener("click", () => post({ type: "marker", pointer }));
    markers.push(marker);
  }
  markerLayer.replaceChildren(...markers);
}

function highlight(pointers) {
  for (const el of document.querySelectorAll(".de-changed")) el.classList.remove("de-changed");
  const wanted = new Set(pointers);
  for (const el of document.querySelectorAll("[data-src]")) {
    if (wanted.has(el.dataset.src)) el.classList.add("de-changed");
  }
}

function reveal(pointer) {
  const el = findPointer(pointer);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("de-flash");
  setTimeout(() => el.classList.remove("de-flash"), 1600);
}

// -- messages from the shell --------------------------------------------------
const handlers = {
  lock(m) {
    readOnly = Boolean(m.readOnly);
    document.body.classList.toggle("de-locked", readOnly);
  },
  highlight(m) {
    highlight(m.pointers || []);
  },
  comments(m) {
    markerCounts = m.counts || {};
    renderMarkers();
  },
  async "commit-now"(m) {
    let ok = true;
    if (starting) starting = null; // a click that has not entered edit mode yet
    if (edit) ok = await commit();
    post({ type: "commit-result", id: m.id, ok });
  },
  reveal(m) {
    reveal(m.pointer);
  },
};

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || event.source !== shell) return;
  const m = event.data;
  if (m && typeof m.type === "string" && Object.hasOwn(handlers, m.type)) handlers[m.type](m);
});

window.addEventListener("resize", renderMarkers);
window.addEventListener("load", renderMarkers);
document.body.append(toolbar, markerLayer);
const announce = () =>
  post({ type: "ready", editing: Boolean(edit), pointers: [...document.querySelectorAll("[data-src]")].map((el) => el.dataset.src) });
handlers.hello = announce; // the shell loaded after this page and missed "ready"
announce();
