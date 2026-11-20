// Small DOM helpers for the shell.
export const $ = (selector, root = document) => root.querySelector(selector);

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function toast(text, kind = "info", ms = 6000) {
  const node = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status", text });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), ms);
}

// Show a <dialog> modally and resolve true when `okSelector` is pressed, false otherwise.
export function ask(dialog, okSelector, cancelSelector) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      resolve(value);
    };
    $(okSelector, dialog).onclick = () => done(true);
    $(cancelSelector, dialog).onclick = () => done(false);
    dialog.onclose = () => done(false);
    dialog.showModal();
  });
}

export const show = (node, visible) => {
  node.hidden = !visible;
};
