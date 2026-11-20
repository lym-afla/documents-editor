import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

import pytest

from documents_editor.events import EventBus
from documents_editor.runner import JobRunner
from documents_editor.server import make_server
from documents_editor.session import EditorSession
from examples.simple_doc import SimpleDocAdapter

DOC = {"title": "T", "paragraphs": ["one"], "table": [["x", 2]],
       "chart": {"title": "c", "labels": ["l"]}}
TOKEN = "tok"


@dataclass
class Resp:
    status: int
    body: bytes
    headers: object

    @property
    def text(self):
        return self.body.decode("utf-8")

    @property
    def json(self):
        return json.loads(self.body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(NoRedirect)


@dataclass
class Srv:
    base: str
    host: str
    session: EditorSession


def request(srv, method, path, *, token=False, headers=None, data=None, raw=None):
    hdrs = dict(headers or {})
    if token:
        hdrs["X-Editor-Token"] = TOKEN
    body = raw
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(srv.base + path, data=body, method=method, headers=hdrs)
    try:
        with _opener.open(req, timeout=10) as r:
            return Resp(r.status, r.read(), r.headers)
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read(), e.headers)


def get(srv, path, **kw):
    return request(srv, "GET", path, **kw)


def post(srv, path, **kw):
    return request(srv, "POST", path, **kw)


def post_json(srv, path, data, **kw):
    return request(srv, "POST", path, token=True, data=data, **kw)


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "doc.json").write_text(json.dumps(DOC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def server(project_dir):
    session = EditorSession(SimpleDocAdapter(), project_dir, bus=EventBus(), runner=JobRunner())
    httpd, url = make_server(session, token=TOKEN)
    port = httpd.server_address[1]
    assert url == f"http://127.0.0.1:{port}/?t={TOKEN}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Srv(f"http://127.0.0.1:{port}", f"127.0.0.1:{port}", session)
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)
    session.close()


def get_json(srv, path):
    r = get(srv, path, token=True)
    assert r.status == 200, r.text
    return r.json


def test_token_host_and_origin_checks(server):
    assert get(server, "/api/state").status == 403
    assert get(server, "/api/state", headers={"X-Editor-Token": "wrong"}).status == 403
    assert get(server, "/api/state", token=True).status == 200
    assert get(server, "/api/state", token=True, headers={"Host": "evil.test"}).status == 421
    assert get(server, "/api/state", token=True,
               headers={"Host": server.host.replace("127.0.0.1", "localhost")}).status == 200
    assert post(server, "/api/undo", token=True, headers={"Origin": "http://evil.test"}).status == 403
    denied = get(server, "/api/state")
    assert "Open the editor from the URL printed in the terminal" in denied.text


def test_index_sets_cookie_and_static_needs_no_token(server):
    assert get(server, "/").status == 403
    r = get(server, "/?t=wrong")
    assert r.status == 403
    r = get(server, f"/?t={TOKEN}")
    assert r.status == 200 and "Document editor" in r.text
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith(f"de_token={TOKEN}") and "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie and "Path=/" in cookie
    assert r.headers["Cache-Control"] == "no-store"
    # the cookie alone authorises later calls
    assert get(server, "/api/state", headers={"Cookie": f"de_token={TOKEN}"}).status == 200
    js = get(server, "/static/page.js")
    assert js.status == 200 and js.headers["Content-Type"].startswith("text/javascript")
    assert get(server, "/static/page.css").headers["Content-Type"].startswith("text/css")
    assert get(server, "/static/missing.png").status == 404
    assert get(server, "/static/..%2Fserver.py").status == 404


def test_patch_round_trip_and_error_codes(server):
    st = get_json(server, "/api/state")
    ok = post_json(server, "/api/patch", {"pointer": "/title", "old": "T", "new": "T2", "source_hash": st["hash"]})
    assert ok.status == 200
    stale = post_json(server, "/api/patch", {"pointer": "/title", "old": "T", "new": "T3", "source_hash": st["hash"]})
    assert stale.status == 409 and stale.json["error"] == "stale"
    bad = post_json(server, "/api/patch", {"pointer": "/title", "old": "T2", "new": "—", "source_hash": ok.json["hash"]})
    assert bad.status == 422 and bad.json["issues"][0]["pointer"] == "/title"
    assert post_json(server, "/api/patch", {"pointer": "/title"}).status == 400
    assert get_json(server, "/api/value?pointer=/title")["value"] == "T2"
    assert get(server, "/api/value?pointer=/nope", token=True).status == 404
    assert get_json(server, "/api/state")["can_undo"] is True
    assert post(server, "/api/undo", token=True).status == 200
    assert get_json(server, "/api/value?pointer=/title")["value"] == "T"


def test_body_validation(server):
    assert post(server, "/api/check", token=True, raw=b"{nope", headers={"Content-Type": "application/json"}).status == 400
    assert post(server, "/api/check", token=True, raw=b"{}", headers={"Content-Type": "text/plain"}).status == 400
    assert post(server, "/api/check", token=True, raw=b"[1]", headers={"Content-Type": "application/json"}).status == 400
    big = b'{"x":"' + b"a" * (5 * 1024 * 1024) + b'"}'
    assert post(server, "/api/check", token=True, raw=big, headers={"Content-Type": "application/json"}).status == 400
    r = post_json(server, "/api/check", {"pointer": "/title", "value": "a — b"})
    assert r.status == 200 and r.json["issues"][0]["message"]


def test_requests_crud_and_fields(server):
    created = post_json(server, "/api/requests", {
        "pointer": "/title", "element": "title", "quote": "T", "category": "text",
        "scope": "document", "instruction": "shorter"})
    assert created.status == 200
    rid = created.json["id"]
    assert [r["id"] for r in get_json(server, "/api/requests")] == [rid]
    bad = post_json(server, "/api/requests", {
        "pointer": None, "element": "e", "quote": "q", "category": "bogus",
        "scope": "document", "instruction": "i"})
    assert bad.status == 422 and bad.json["error"] == "invalid"
    upd = request(server, "PATCH", f"/api/requests/{rid}", token=True, data={"instruction": "longer"})
    assert upd.status == 200 and upd.json["instruction"] == "longer"
    assert request(server, "PATCH", "/api/requests/nope", token=True, data={"instruction": "x"}).status == 404
    assert request(server, "DELETE", f"/api/requests/{rid}", token=True).status == 200
    assert request(server, "DELETE", f"/api/requests/{rid}", token=True).status == 404
    fields = get_json(server, "/api/fields?pointer=/chart")
    assert fields[0]["pointer"] == "/chart/title"


def test_doc_injects_editor_assets_and_project_files_are_confined(server, project_dir):
    (project_dir / "Астро проект").mkdir()
    (project_dir / "Астро проект" / "a.png").write_bytes(b"png")
    html = get(server, "/doc", token=True).text
    assert '<base href="/project/">' in html and "/static/page.js" in html
    assert html.index("<base") < html.index("<h1") < html.index("/static/page.js")
    assert get(server, "/doc").status == 403
    r = get(server, "/project/" + quote("Астро проект/a.png"), token=True)
    assert r.body == b"png" and r.headers["Content-Type"] == "image/png"
    for bad in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "x/../doc.json", ".editor/lock",
                "missing.png", quote("Астро проект")):
        assert get(server, "/project/" + bad, token=True).status == 404, bad
    assert get(server, "/project/doc.json", token=True).status == 200
    assert get(server, "/project/doc.json").status == 403


def test_project_symlink_escape_is_refused(server, project_dir, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("s")
    (project_dir / "link").symlink_to(outside)
    assert get(server, "/project/link/secret.txt", token=True).status == 404


def test_pages_are_limited_to_build_results(server, project_dir, monkeypatch):
    page = project_dir / "out.html"
    page.write_text("<p>built</p>")
    (project_dir / "other.html").write_text("no")
    from documents_editor.adapter import BuildResult
    monkeypatch.setattr(server.session.adapter, "build_result", lambda d: BuildResult([page], []))
    assert get(server, "/api/pages/out.html", token=True).text == "<p>built</p>"
    assert get(server, "/api/pages/other.html", token=True).status == 404
    assert get(server, "/api/pages/..%2Fdoc.json", token=True).status == 404
    assert get(server, "/api/pages/out.html").status == 403


def test_build_cancel_and_busy_codes(server, project_dir):
    assert post(server, "/api/build", token=True).status == 200
    deadline = time.monotonic() + 10
    while not (project_dir / "build.log").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (project_dir / "build.log").exists()
    deadline = time.monotonic() + 10
    while get_json(server, "/api/state")["locked"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert post(server, "/api/cancel", token=True).status == 200
    assert get_json(server, "/api/agent")["args"]
    assert post_json(server, "/api/agent/accept", {"pointers": []}).status == 200
    assert post_json(server, "/api/agent/reapply", {"pointers": "x"}).status == 400


def test_unknown_route_and_unexpected_error(server, monkeypatch):
    assert get(server, "/nope", token=True).status == 404
    monkeypatch.setattr(server.session, "state", lambda: 1 / 0)
    r = get(server, "/api/state", token=True)
    assert r.status == 500 and r.json["error"].startswith("ZeroDivisionError")


def open_sse(srv):
    conn = http.client.HTTPConnection(srv.host, timeout=10)
    conn.request("GET", "/events", headers={"X-Editor-Token": TOKEN})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    return conn, resp


def next_event(resp, name):
    event = None
    while True:
        line = resp.readline().decode("utf-8")
        if not line:
            raise AssertionError(f"stream ended before {name!r}")
        line = line.rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == name:
            return json.loads(line[6:])


def test_events_stream_delivers_state(server):
    conn, resp = open_sse(server)
    try:
        assert next_event(resp, "state")["valid"] is True  # initial snapshot
        st = get_json(server, "/api/state")
        post_json(server, "/api/patch", {"pointer": "/title", "old": "T", "new": "T9", "source_hash": st["hash"]})
        assert next_event(resp, "state")["can_undo"] is True
        server.session.bus.publish("log", {"job": "build", "line": "hi"})
        assert next_event(resp, "log") == {"job": "build", "line": "hi"}
    finally:
        conn.close()


def test_events_require_token_and_unsubscribe_on_disconnect(server):
    assert get(server, "/events").status == 403
    bus = server.session.bus
    conn, resp = open_sse(server)
    next_event(resp, "state")
    assert len(bus._subscribers) == 1
    resp.close()
    conn.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        bus.publish("log", {"line": "x" * 1000})  # a write to the closed socket ends the handler
        if not bus._subscribers:
            break
        time.sleep(0.1)
    assert bus._subscribers == []


def test_server_close_does_not_hang_with_open_sse(project_dir):
    session = EditorSession(SimpleDocAdapter(), project_dir, bus=EventBus(), runner=JobRunner())
    httpd, _ = make_server(session, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    srv = Srv("", f"127.0.0.1:{httpd.server_address[1]}", session)
    conn, resp = open_sse(srv)
    next_event(resp, "state")
    started = time.monotonic()
    httpd.shutdown()
    httpd.server_close()
    assert time.monotonic() - started < 5
    conn.close()
    session.close()
