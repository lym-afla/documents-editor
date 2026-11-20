// Converts between the source's light markup and contenteditable DOM.
// The token regex mirrors the memo renderer's.
const TOKEN = /\*\*([^\n]+?)\*\*|\*([^*]+?)\*|\[\^([A-Za-z0-9_-]+)\]/g;
const FN_ID = /^[A-Za-z0-9_-]+$/;
const NBSP = " ";
const STAR_ISSUE = "A literal * will be read as emphasis";

function esc(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function toHtml(markup, kind) {
  if (kind !== "inline") return esc(markup);
  let out = "";
  let last = 0;
  for (const m of markup.matchAll(TOKEN)) {
    out += esc(markup.slice(last, m.index));
    if (m[1] !== undefined) out += `<strong>${esc(m[1])}</strong>`;
    else if (m[2] !== undefined) out += `<em>${esc(m[2])}</em>`;
    else {
      out += `<span class="de-fn" contenteditable="false" data-fn="${esc(m[3])}">${esc(m[3])}</span>`;
    }
    last = m.index + m[0].length;
  }
  return out + esc(markup.slice(last));
}

export function literalStarIssue(markup) {
  let last = 0;
  for (const m of markup.matchAll(TOKEN)) {
    if (markup.slice(last, m.index).includes("*")) return STAR_ISSUE;
    last = m.index + m[0].length;
  }
  return markup.slice(last).includes("*") ? STAR_ISSUE : null;
}

const fail = (reason) => ({ ok: false, reason });

// Serialise root's children. keepNbsp leaves NBSP untouched.
function serialise(root, kind, keepNbsp, noCheck = false) {
  const issues = new Set();
  const text = (s) => {
    return keepNbsp ? s : s.replaceAll(NBSP, " ");
  };
  const nodes = [...root.childNodes].filter((n) => n.nodeType !== 8);
  const tail = nodes[nodes.length - 1];
  if (tail && tail.nodeType === 1 && tail.tagName === "BR") nodes.pop();

  const segs = []; // {type: "text"|"strong"|"em"|"fn", value}
  for (const n of nodes) {
    if (n.nodeType === 3) {
      if (kind === "inline" && n.data.includes("*")) issues.add(STAR_ISSUE);
      const value = text(n.data);
      if (value !== "") segs.push({ type: "text", value });
      continue;
    }
    if (n.nodeType !== 1) return fail("Unsupported node");
    if (kind !== "inline") return fail(`Unsupported element <${n.localName}>`);
    const tag = n.tagName;
    if (tag === "SPAN" && n.classList.contains("de-fn")) {
      const id = n.getAttribute("data-fn") ?? "";
      if (!FN_ID.test(id)) return fail("Invalid footnote marker");
      segs.push({ type: "fn", value: id });
    } else if (["STRONG", "B", "EM", "I"].includes(tag)) {
      if (n.attributes.length) return fail(`Unsupported attributes on <${n.localName}>`);
      const type = tag === "STRONG" || tag === "B" ? "strong" : "em";
      let value = "";
      for (const c of n.childNodes) {
        if (c.nodeType === 8) continue;
        if (c.nodeType !== 3) return fail("Nested formatting");
        value += text(c.data);
      }
      if (type === "strong" && value.includes("\n")) return fail("Line break inside bold");
      if (type === "em" && value.includes("\n")) return fail("Line break inside italic");
      if (value !== "") segs.push({ type, value });
    } else {
      return fail(`Unsupported element <${n.localName}>`);
    }
  }

  const merged = [];
  for (const s of segs) {
    const p = merged[merged.length - 1];
    if (p && p.type === s.type && s.type !== "fn") p.value += s.value;
    else merged.push({ ...s });
  }
  const markup = merged
    .map((s) =>
      s.type === "strong" ? `**${s.value}**`
      : s.type === "em" ? `*${s.value}*`
      : s.type === "fn" ? `[^${s.value}]`
      : s.value)
    .join("");
  if (kind === "inline" && !noCheck) {
    // The markup must parse back to exactly these segments.
    const scratch = root.ownerDocument.createElement("div");
    scratch.innerHTML = toHtml(markup, kind);
    const back = serialise(scratch, kind, keepNbsp, true);
    if (!back.ok || JSON.stringify(back.segs) !== JSON.stringify(merged)) {
      return fail("Formatting cannot be represented");
    }
  }
  return { ok: true, markup, issues: [...issues], segs: merged };
}

export function fromDom(root, kind, original) {
  const raw = serialise(root, kind, true);
  if (raw.ok) {
    const ref = root.ownerDocument.createElement("div");
    ref.innerHTML = toHtml(original, kind);
    const expected = serialise(ref, kind, true);
    if (expected.ok && expected.markup === raw.markup) {
      return { ok: true, markup: original, issues: [] };
    }
  }
  const res = serialise(root, kind, original.includes(NBSP));
  if (!res.ok) return res;
  return { ok: true, markup: res.markup, issues: res.issues };
}
