// Converts between the source's light markup and contenteditable DOM.
// The token regex mirrors the memo renderer's.
const TOKEN = /\*\*(.+?)\*\*|\*([^*]+?)\*|\[\^([A-Za-z0-9_-]+)\]/g;
const FN_ID = /^[A-Za-z0-9_-]+$/;
const NBSP = " ";
const STAR_ISSUE = "A literal * will be read as emphasis";
const FN_ISSUE = "A literal [^id] will be read as a footnote marker";

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
function serialise(root, kind, keepNbsp) {
  const issues = new Set();
  const text = (s) => {
    if (s.includes("*")) issues.add(STAR_ISSUE);
    if (/\[\^[A-Za-z0-9_-]+\]/.test(s)) issues.add(FN_ISSUE);
    return keepNbsp ? s : s.replaceAll(NBSP, " ");
  };
  const nodes = [...root.childNodes].filter((n) => n.nodeType !== 8);
  const tail = nodes[nodes.length - 1];
  if (tail && tail.nodeType === 1 && tail.tagName === "BR") nodes.pop();

  const segs = []; // {type: "text"|"strong"|"em"|"fn", value}
  for (const n of nodes) {
    if (n.nodeType === 3) {
      segs.push({ type: "text", value: text(n.data) });
      continue;
    }
    if (n.nodeType !== 1) return fail("Unsupported node");
    if (kind !== "inline") return fail(`Unsupported element <${n.localName}>`);
    const tag = n.tagName;
    if (n.classList.contains("de-fn")) {
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
      if (type === "strong" && /[\r\n]/.test(value)) return fail("Line break inside bold");
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
  return { ok: true, markup, issues: [...issues] };
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
  return serialise(root, kind, original.includes(NBSP));
}
