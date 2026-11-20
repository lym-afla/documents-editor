// Send to agent: command confirmation, the run's outcome, and the lost-edits dialog.
import { api, errorText } from "./api.js";
import { $, ask, h, show, toast } from "./ui.js";
import * as comments from "./comments.js";

let before = null; // {hash, requests} when this tab started the run
let revertSteps = 0; // undo steps back to before the agent run (each re-apply adds one)

// History moved past the agent run's result (a save, undo or redo): Revert no longer means one undo.
export function historyMoved() {
  revertSteps = 0;
  show($("#agent-banner .revert"), false);
  show($("#lost-edits .revert"), false);
}

export function init(ctx) {
  ctx.historyMoved = historyMoved;
  $("#send-agent").addEventListener("click", () => send(ctx));
  const dialog = $("#lost-edits");
  dialog.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const row = button.closest("tr");
    const rows = row ? [row] : [...dialog.querySelectorAll("tbody tr")];
    if (button.matches(".reapply, .reapply-all")) await reapply(ctx, rows);
    else if (button.matches(".keep, .keep-all")) await keep(rows);
    else if (button.matches(".revert")) {
      dialog.close();
      if (revertSteps) await ctx.undo(revertSteps);
    } else if (button.matches(".close")) dialog.close();
  });
  $("#agent-banner .dismiss").addEventListener("click", () => show($("#agent-banner"), false));
  $("#agent-banner .revert").addEventListener("click", async () => {
    show($("#agent-banner"), false);
    if (revertSteps) await ctx.undo(revertSteps);
  });
}

async function snapshot() {
  const [st, reqs] = await Promise.all([api("GET", "/api/state"), api("GET", "/api/requests")]);
  return { hash: st.data?.hash ?? null, requests: JSON.stringify(reqs.data) };
}

async function send(ctx) {
  if (!(await ctx.commitPage())) {
    toast("Finish or cancel the edit first, then send to the agent", "warn");
    return;
  }
  const preview = await api("GET", "/api/agent");
  if (preview.status !== 200) {
    toast(errorText(preview.data), "error");
    return;
  }
  const confirm = !preview.data.confirmed;
  if (confirm) {
    const dialog = $("#confirm-dialog");
    $(".command", dialog).textContent = preview.data.args.join("\n");
    if (!(await ask(dialog, "#confirm-command", ".cancel"))) return;
  }
  before = await snapshot();
  const r = await api("POST", "/api/agent", { confirm });
  if (r.status !== 200) {
    before = null;
    toast(errorText(r.data), "error");
    return;
  }
  show($("#agent-banner"), false);
  if ($("#lost-edits").open) $("#lost-edits").close();
}

function outcome(report) {
  if (report.cancelled) return "The agent run was cancelled.";
  if (report.timed_out) return "The agent run timed out.";
  if (report.returncode !== 0 && report.returncode !== null) return `The agent exited with code ${report.returncode}.`;
  return "The agent run finished with problems.";
}

const show_value = (value) => (value === null || value === undefined ? "(field removed)" : String(value));

export async function done(ctx, report) {
  ctx.highlight = Array.isArray(report.changed) ? report.changed : [];
  ctx.reloadDoc();
  comments.refresh(ctx);
  // An agent snapshot exists (so Revert is one undo) only if the run changed a tracked file.
  let changedFiles = ctx.highlight.length > 0;
  if (before) {
    const after = await snapshot();
    changedFiles = after.hash !== before.hash || after.requests !== before.requests;
    before = null;
  }
  revertSteps = changedFiles ? 1 : 0;

  const banner = $("#agent-banner");
  if (!report.ok) {
    const issues = (report.issues || []).filter((i) => i.severity === "error").map((i) => i.message);
    $(".text", banner).textContent = [outcome(report), ...issues].join(" ") + " See the Log tab.";
    show($(".revert", banner), changedFiles);
    show(banner, true);
  } else {
    toast("The agent run finished", "info");
  }

  const lost = report.lost || [];
  const dialog = $("#lost-edits");
  if (!lost.length) return;
  $("tbody", dialog).replaceChildren(...lost.map((item) =>
    h("tr", { "data-pointer": item.pointer },
      h("td", {}, h("code", { text: item.pointer })),
      h("td", {}, h("pre", { class: "mine", text: show_value(item.mine) })),
      h("td", {}, h("pre", { class: "now", text: show_value(item.now) })),
      h("td", { class: "actions" },
        h("button", { type: "button", class: "reapply", text: "Re-apply" }),
        h("button", { type: "button", class: "keep", text: "Keep agent's" })))));
  show($(".revert", dialog), changedFiles);
  if (!dialog.open) dialog.showModal();
}

function finishRows(rows) {
  for (const row of rows) row.remove();
  const dialog = $("#lost-edits");
  if (!dialog.querySelector("tbody tr")) dialog.close();
}

async function reapply(ctx, rows) {
  const r = await api("POST", "/api/agent/reapply", { pointers: rows.map((row) => row.dataset.pointer) });
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return;
  }
  revertSteps += 1;
  finishRows(rows);
  ctx.reloadDoc();
}

async function keep(rows) {
  const r = await api("POST", "/api/agent/accept", { pointers: rows.map((row) => row.dataset.pointer) });
  if (r.status !== 200) {
    toast(errorText(r.data), "error");
    return;
  }
  finishRows(rows);
}
