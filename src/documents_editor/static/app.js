// Editor shell: top bar, state from server-sent events, and the bridge to the page in the iframe.
import { api, errorText } from "./api.js";
import { $, h, show, toast } from "./ui.js";
import * as agent from "./agent.js";
import * as comments from "./comments.js";
import * as panel from "./panel.js";

const FIGURE_TOAST = "Figure changed – the agent will reconcile facts and calculations";
const iframe = $("#doc");

const ctx = {
  state: null,
  pageReady: false, // the page in the iframe has sent "ready" since its last load
  editing: false,
  pointers: [], // data-src values on the current page
  highlight: [], // pointers the last agent run changed, until the next edit
  readOnly: null,

  send(message) {
    if (this.pageReady) iframe.contentWindow.postMessage(message, location.origin);
  },

  // Reload the page, keeping its scroll position.
  reloadDoc() {
    const win = iframe.contentWindow;
    try {
      savedScroll = { x: win.scrollX, y: win.scrollY };
    } catch {
      savedScroll = null;
    }
    this.pageReady = false;
    this.editing = false;
    show($("#outside-banner"), false);
    win.location.replace("/doc");
  },

  // Ask the page to commit an element being edited; resolves whether nothing is left unsaved.
  commitPage() {
    if (!this.pageReady || !this.editing) return Promise.resolve(true);
    const id = ++commitSeq;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        pendingCommits.delete(id);
        resolve(false);
      }, 15000);
      pendingCommits.set(id, (ok) => {
        clearTimeout(timer);
        resolve(ok);
      });
      this.send({ type: "commit-now", id });
    });
  },

  async undo(steps = 1) {
    if (!(await this.commitPage())) return false;
    for (let i = 0; i < steps; i++) {
      const r = await api("POST", "/api/undo");
      if (r.status !== 200) {
        toast(errorText(r.data), "error");
        break;
      }
    }
    this.reloadDoc();
    comments.refresh(this);
    return true;
  },

  sendLock() {
    const st = this.state;
    if (!st) return;
    const readOnly = st.locked || !st.valid;
    this.readOnly = readOnly;
    this.send({ type: "lock", readOnly });
  },
};

let savedScroll = null;
let commitSeq = 0;
const pendingCommits = new Map();

iframe.addEventListener("load", () => {
  if (!savedScroll) return;
  try {
    iframe.contentWindow.scrollTo(savedScroll.x, savedScroll.y);
  } catch {
    /* cross-origin error page */
  }
  savedScroll = null;
});

// -- messages from the page -------------------------------------------------
const pageHandlers = {
  ready(m) {
    ctx.pageReady = true;
    ctx.editing = false;
    ctx.pointers = Array.isArray(m.pointers) ? m.pointers : [];
    ctx.sendLock();
    ctx.send({ type: "highlight", pointers: ctx.highlight });
    comments.pageLoaded(ctx);
  },
  editing(m) {
    ctx.editing = Boolean(m.on);
  },
  committed(m) {
    ctx.highlight = [];
    if (m.figure_request) toast(FIGURE_TOAST, "warn", 10000);
    ctx.reloadDoc();
    comments.refresh(ctx);
  },
  "commit-result"(m) {
    pendingCommits.get(m.id)?.(Boolean(m.ok));
    pendingCommits.delete(m.id);
  },
  stale() {
    $("#outside-banner .text").textContent =
      "The file changed outside the editor, so this edit was not saved. Reload to see the current text.";
    show($("#outside-banner"), true);
  },
  locked() {
    toast("The document is read-only while the agent or a build runs", "warn");
  },
  toast(m) {
    toast(String(m.text), m.kind === "error" ? "error" : "info");
  },
  raw(m) {
    panel.openRaw(ctx, m.pointer, m.reason);
  },
  comment(m) {
    comments.setTarget(ctx, { pointer: m.pointer ?? null, element: m.element, quote: m.quote ?? "" });
  },
  chart(m) {
    panel.openFields(ctx, m.pointer);
  },
  "live-issues"(m) {
    panel.setLiveIssues(m.issues || []);
  },
  marker(m) {
    comments.reveal(m.pointer);
  },
};

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || event.source !== iframe.contentWindow) return;
  const m = event.data;
  if (!m || typeof m.type !== "string" || !Object.hasOwn(pageHandlers, m.type)) return;
  pageHandlers[m.type](m);
});

// -- server state -------------------------------------------------------------
function applyState(st) {
  const previous = ctx.state;
  ctx.state = st;
  $("#undo").disabled = !st.can_undo || st.locked;
  $("#redo").disabled = !st.can_redo || st.locked;
  $("#send-agent").disabled = st.locked;
  $("#rebuild").disabled = st.locked;
  const badge = $("#open-count");
  badge.textContent = String(st.open_requests);
  show(badge, st.open_requests > 0);
  show($("#cancel-job"), st.locked);

  const working = st.job === "agent" ? "Agent working…" : st.job === "build" ? "Building…" : "A job is running…";
  $("#status").textContent = st.locked ? working : st.valid ? "Ready" : "Source has errors";
  const lock = $("#lock-banner");
  lock.textContent = `${working} The document is read-only until it finishes.`;
  show(lock, st.locked);
  if (st.job && st.job !== previous?.job) panel.startLog(st.job);

  const invalid = $("#invalid-banner");
  const errors = st.issues.filter((i) => i.severity === "error").map((i) => i.message);
  $(".text", invalid).textContent =
    `The source has errors, so the last good version is shown read-only: ${errors.join("; ")}`;
  show($(".revert", invalid), st.can_undo && !st.locked);
  show(invalid, !st.valid);

  const notices = $("#notices");
  notices.replaceChildren(...st.notices.map((n) => h("li", { text: n })));
  show(notices, st.notices.length > 0);

  panel.setStateIssues(ctx, st.issues);
  if (ctx.readOnly !== (st.locked || !st.valid)) ctx.sendLock();
  comments.refresh(ctx);
}

function connect() {
  const events = new EventSource("/events");
  const on = (name, handler) =>
    events.addEventListener(name, (e) => {
      try {
        handler(JSON.parse(e.data));
      } catch (exc) {
        console.error(`event ${name} failed`, exc);
      }
    });
  on("state", applyState);
  on("source-changed", () => {
    if (ctx.editing) {
      $("#outside-banner .text").textContent =
        "The file changed outside the editor. Finish or cancel your edit, then reload.";
      show($("#outside-banner"), true);
    } else {
      ctx.reloadDoc();
    }
    comments.refresh(ctx);
  });
  on("log", (d) => panel.appendLog(d.line));
  on("agent-done", (report) => agent.done(ctx, report));
  on("build-done", (report) => panel.showBuild(report));
  events.onerror = () => {
    $("#status").textContent = "Disconnected from the editor – retrying…";
  };
}

// -- top bar ------------------------------------------------------------------
$("#undo").addEventListener("click", () => ctx.undo());
$("#redo").addEventListener("click", async () => {
  if (!(await ctx.commitPage())) return;
  const r = await api("POST", "/api/redo");
  if (r.status !== 200) toast(errorText(r.data), "error");
  ctx.reloadDoc();
  comments.refresh(ctx);
});
$("#rebuild").addEventListener("click", async () => {
  if (!(await ctx.commitPage())) {
    toast("Finish or cancel the edit before rebuilding", "warn");
    return;
  }
  const r = await api("POST", "/api/build");
  if (r.status !== 200) toast(errorText(r.data), "error");
});
$("#cancel-job").addEventListener("click", async () => {
  const r = await api("POST", "/api/cancel");
  if (r.status !== 200) toast(errorText(r.data), "error");
});
$("#outside-banner .reload").addEventListener("click", () => ctx.reloadDoc());
$("#invalid-banner .revert").addEventListener("click", () => ctx.undo());

panel.init(ctx);
comments.init(ctx);
agent.init(ctx);
connect();
