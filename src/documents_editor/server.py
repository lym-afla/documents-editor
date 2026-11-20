"""Token-protected JSON API and server-sent events over EditorSession (127.0.0.1 only)."""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import secrets
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .pointer import PointerError
from .runner import Busy
from .session import EditorSession, Locked, Refused, StaleSource
from .store import SourceUnreadable

MAX_BODY = 5 * 1024 * 1024
HEARTBEAT_SECONDS = 15
COOKIE = "de_token"
DENIED = "Open the editor from the URL printed in the terminal"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}
_DISCONNECTED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


class BadRequest(Exception):
    pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    session: EditorSession
    token: str
    allowed_hosts: frozenset[str]
    stopping: threading.Event

    def server_close(self) -> None:
        self.stopping.set()  # lets open SSE handlers return
        super().server_close()


def _content_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _safe_parts(rel: str) -> list[str] | None:
    """Split a decoded relative path; None when it is absolute or tries to leave its root."""
    if not rel or "\x00" in rel or "\\" in rel:
        return None
    if rel.startswith("/") or PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def _inject_assets(html: str) -> str:
    if not re.search(r"<head[\s>]", html, re.I):
        html = f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html}</body></html>'
    html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + '<base href="/project/">', html, count=1, flags=re.I)
    assets = '<link rel="stylesheet" href="/static/page.css"><script type="module" src="/static/page.js"></script>'
    if re.search(r"</body>", html, re.I):
        idx = html.lower().rindex("</body>")
        return html[:idx] + assets + html[idx:]
    return html + assets


class Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "documents-editor"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- plumbing ----------------------------------------------------------
    @property
    def session(self) -> EditorSession:
        return self.server.session

    def _send(self, status: int, body: bytes, content_type: str, extra: list[tuple[str, str]] = ()) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except _DISCONNECTED:
            self.close_connection = True

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in {f"http://{h}" for h in self.server.allowed_hosts}

    def _authorised(self, query_token: str | None = None) -> bool:
        expected = self.server.token
        candidates = [self.headers.get("X-Editor-Token"), query_token]
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        if COOKIE in cookies:
            candidates.append(cookies[COOKIE].value)
        return any(
            c is not None and secrets.compare_digest(c.encode("utf-8"), expected.encode("utf-8"))
            for c in candidates
        )

    def _discard(self, length: int) -> None:
        """Read and drop an oversized body so the client can receive the 400."""
        remaining = min(max(length, 0), 8 * MAX_BODY)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if length < 0 or length > MAX_BODY:
            self._discard(length)
            raise BadRequest("body too large")
        if length == 0:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise BadRequest("expected a JSON body")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("invalid JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    @staticmethod
    def _field(body: dict, name: str, kind: type | tuple = str) -> Any:
        if name not in body or not isinstance(body[name], kind):
            raise BadRequest(f"missing or invalid {name!r}")
        return body[name]

    # -- dispatch ----------------------------------------------------------
    def do_GET(self) -> None:
        self._handle()

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self) -> None:
        try:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            path = parts.path
            if self.headers.get("Host") not in self.server.allowed_hosts:
                return self._text(HTTPStatus.MISDIRECTED_REQUEST, "Unexpected Host header")
            if not self._origin_ok():
                return self._text(HTTPStatus.FORBIDDEN, "Cross-origin request refused")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/":
                return self._index(query)
            if not self._authorised():
                return self._text(HTTPStatus.FORBIDDEN, DENIED)
            self._route(path, query)
        except _DISCONNECTED:
            self.close_connection = True
        except Exception as exc:
            status, data = self._error(exc)
            self._json(status, data)

    @staticmethod
    def _error(exc: Exception) -> tuple[int, dict]:
        if isinstance(exc, BadRequest):
            return 400, {"error": "bad request", "message": str(exc)}
        if isinstance(exc, Locked):
            return 409, {"error": "locked"}
        if isinstance(exc, StaleSource):
            return 409, {"error": "stale"}
        if isinstance(exc, Busy):
            return 409, {"error": "busy"}
        if isinstance(exc, Refused):
            return 422, {"error": "invalid", "issues": [asdict(i) for i in exc.issues]}
        if isinstance(exc, SourceUnreadable):
            return 409, {"error": "unreadable", "message": exc.message}
        if isinstance(exc, PointerError):
            return 404, {"error": "no such field"}
        if isinstance(exc, KeyError):
            return 404, {"error": "not found"}
        if isinstance(exc, ValueError):
            return 422, {"error": "invalid", "message": str(exc)}
        return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _index(self, query: dict) -> None:
        supplied = (query.get("t") or [None])[0]
        if not self._authorised(supplied):
            return self._text(HTTPStatus.FORBIDDEN, DENIED)
        body = self._static_bytes("index.html")
        assert body is not None
        cookie = f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
        self._send(200, body, _CONTENT_TYPES[".html"], [("Set-Cookie", cookie)])

    @staticmethod
    def _static_bytes(rel: str) -> bytes | None:
        parts = _safe_parts(rel)
        if parts is None:
            return None
        node = resources.files("documents_editor").joinpath("static", *parts)
        try:
            return node.read_bytes() if node.is_file() else None
        except OSError:
            return None

    def _static(self, rel: str) -> None:
        body = self._static_bytes(unquote(rel))
        if body is None:
            return self._json(404, {"error": "not found"})
        self._send(200, body, _content_type(rel))

    # -- routes ------------------------------------------------------------
    def _route(self, path: str, query: dict) -> None:
        method, s = self.command, self.session
        if method == "HEAD":
            method = "GET"
        pointer = (query.get("pointer") or [""])[0]

        if path == "/events" and method == "GET":
            return self._events()
        if path == "/doc" and method == "GET":
            return self._send(200, _inject_assets(s.page_html()).encode("utf-8"), _CONTENT_TYPES[".html"])
        if path.startswith("/project/") and method == "GET":
            return self._project(path[len("/project/"):])
        if path.startswith("/api/pages/") and method == "GET":
            return self._page(unquote(path[len("/api/pages/"):]))
        if path.startswith("/api/requests/") and method in ("PATCH", "DELETE"):
            request_id = unquote(path[len("/api/requests/"):])
            if method == "PATCH":
                return self._json(200, s.update_request(request_id, **self._body()))
            s.delete_request(request_id)
            return self._json(200, {"ok": True})

        key = (method, path)
        if key == ("GET", "/api/state"):
            return self._json(200, s.state())
        if key == ("GET", "/api/value"):
            return self._json(200, s.value(pointer))
        if key == ("POST", "/api/check"):
            body = self._body()
            issues = s.check(self._field(body, "pointer"), self._field(body, "value"))
            return self._json(200, {"issues": [asdict(i) for i in issues]})
        if key == ("POST", "/api/patch"):
            body = self._body()
            return self._json(200, s.patch(
                self._field(body, "pointer"), body.get("old"),
                self._field(body, "new"), self._field(body, "source_hash"),
            ))
        if key == ("POST", "/api/undo"):
            s.undo()
            return self._json(200, {"ok": True})
        if key == ("POST", "/api/redo"):
            s.redo()
            return self._json(200, {"ok": True})
        if key == ("GET", "/api/requests"):
            return self._json(200, s.requests())
        if key == ("POST", "/api/requests"):
            body = self._body()
            pointer_value = body.get("pointer")
            if pointer_value is not None and not isinstance(pointer_value, str):
                raise BadRequest("invalid 'pointer'")
            return self._json(200, s.add_request(
                pointer=pointer_value, element=self._field(body, "element"),
                quote=self._field(body, "quote"), category=self._field(body, "category"),
                scope=self._field(body, "scope"), instruction=self._field(body, "instruction"),
            ))
        if key == ("GET", "/api/fields"):
            doc = s.store.read()[0]
            return self._json(200, s.adapter.panel_fields(doc, pointer))
        if key == ("GET", "/api/agent"):
            return self._json(200, s.agent_preview())
        if key == ("POST", "/api/agent"):
            confirm = self._body().get("confirm", False)
            if not isinstance(confirm, bool):
                raise BadRequest("invalid 'confirm'")
            s.start_agent(confirm)
            return self._json(200, {"ok": True})
        if key == ("POST", "/api/agent/reapply"):
            return self._json(200, s.reapply(self._pointers()))
        if key == ("POST", "/api/agent/accept"):
            s.accept(self._pointers())
            return self._json(200, {"ok": True})
        if key == ("POST", "/api/build"):
            s.start_build()
            return self._json(200, {"ok": True})
        if key == ("POST", "/api/cancel"):
            s.cancel_job()
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def _pointers(self) -> list[str]:
        pointers = self._field(self._body(), "pointers", list)
        if not all(isinstance(p, str) for p in pointers):
            raise BadRequest("invalid 'pointers'")
        return pointers

    def _project(self, raw: str) -> None:
        parts = _safe_parts(unquote(raw))
        root = self.session.project_dir.resolve()
        target = None
        if parts is not None:
            candidate = (root / Path(*parts)).resolve()
            try:
                inside = candidate.relative_to(root).parts
            except ValueError:
                inside = ()
            if inside and inside[0] != ".editor" and candidate.is_file():
                target = candidate
        if target is None:
            return self._json(404, {"error": "not found"})
        try:
            body = target.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        self._send(200, body, _content_type(target.name))

    def _page(self, name: str) -> None:
        result = self.session.adapter.build_result(self.session.project_dir)
        target = next((Path(p) for p in result.pages if Path(p).name == name), None)
        try:
            body = target.read_bytes() if target is not None else None
        except OSError:
            body = None
        if body is None:
            return self._json(404, {"error": "not found"})
        self._send(200, body, _content_type(name))

    # -- server-sent events ------------------------------------------------
    def _events(self) -> None:
        bus = self.session.bus
        q = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._write_event("state", self.session.state())
            last = time.monotonic()
            while not self.server.stopping.is_set():
                try:
                    event, data = q.get(timeout=1)
                except queue.Empty:
                    if time.monotonic() - last >= HEARTBEAT_SECONDS:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last = time.monotonic()
                    continue
                self._write_event(event, data)
                last = time.monotonic()
        except _DISCONNECTED:
            pass
        finally:
            bus.unsubscribe(q)

    def _write_event(self, event: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def make_server(
    session: EditorSession, *, port: int = 0, token: str | None = None
) -> tuple[ThreadingHTTPServer, str]:
    server = _Server(("127.0.0.1", port), Handler)
    bound = server.server_address[1]
    server.session = session
    server.token = token or secrets.token_urlsafe(24)
    server.allowed_hosts = frozenset({f"127.0.0.1:{bound}", f"localhost:{bound}"})
    server.stopping = threading.Event()
    return server, f"http://127.0.0.1:{bound}/?t={server.token}"
