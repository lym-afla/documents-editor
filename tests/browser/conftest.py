"""Playwright fixtures: serve the static dir and call markup.js from a page."""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "documents_editor" / "static"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/_harness":
            body = b"<!doctype html><title>harness</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def static_server():
    handler = functools.partial(_Handler, directory=str(STATIC))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # chromium not installed or cannot start
            pytest.skip(f"chromium unavailable: {exc}")
        yield browser
        browser.close()


class Js:
    def __init__(self, page):
        self.page = page

    def round_trip(self, markup, kind):
        return self.page.evaluate(
            """async ([markup, kind]) => {
                const m = await import('/markup.js');
                const div = document.createElement('div');
                div.innerHTML = m.toHtml(markup, kind);
                return m.fromDom(div, kind, markup);
            }""",
            [markup, kind],
        )

    def from_html(self, html, kind, original):
        return self.page.evaluate(
            """async ([html, kind, original]) => {
                const m = await import('/markup.js');
                const div = document.createElement('div');
                div.innerHTML = html;
                return m.fromDom(div, kind, original);
            }""",
            [html, kind, original],
        )

    def to_html(self, markup, kind):
        return self.page.evaluate(
            "async ([m, k]) => (await import('/markup.js')).toHtml(m, k)", [markup, kind]
        )

    def literal_star_issue(self, markup):
        return self.page.evaluate(
            "async (m) => (await import('/markup.js')).literalStarIssue(m)", markup
        )


@pytest.fixture
def js(browser, static_server):
    page = browser.new_page()
    page.goto(f"{static_server}/_harness")
    yield Js(page)
    page.close()
