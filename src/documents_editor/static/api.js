// Same-origin JSON calls. The session cookie authorises reads; state-changing calls also need
// the token in a header, which only the shell holds (set once from its start URL).
let token = null;

export function setToken(value) {
  token = value;
}

export const hasToken = () => token !== null;

export async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (token !== null && method !== "GET") options.headers["X-Editor-Token"] = token;
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (exc) {
    return { status: 0, data: { error: "network", message: `Cannot reach the editor: ${exc}` } };
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  return { status: response.status, data };
}

export const q = encodeURIComponent;

// A message for a failed call's response body.
export function errorText(data) {
  if (data?.issues?.length) return data.issues.map((i) => i.message).join("; ");
  if (data?.error === "locked" || data?.error === "busy") {
    return "The document is read-only while the agent or a build runs";
  }
  if (data?.error === "stale") return "The file changed outside the editor; reload to continue";
  return data?.message || data?.error || "Request failed";
}
