"""End to end: the editor UI in headless Chromium against a real server and a fake agent."""
import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from documents_editor import pointer as ptr
from documents_editor.events import EventBus
from documents_editor.runner import JobRunner
from documents_editor.server import make_server
from documents_editor.session import EditorSession
from examples.simple_doc import SimpleDocAdapter

pytestmark = pytest.mark.browser
expect = pytest.importorskip("playwright.sync_api").expect
expect.set_options(timeout=15_000)

DOC = {"title": "T", "paragraphs": ["one 'q'"], "table": [["x", 2]],
       "chart": {"title": "c", "labels": ["l"]}}

AGENT_SCRIPT = r'''
import json, pathlib, sys
opts = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
src, req = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
print("agent started", flush=True)
doc = json.loads(src.read_text(encoding="utf-8"))
for pointer, value in opts.get("set_paths", {}).items():
    *parents, last = pointer[1:].split("/")
    node = doc
    for t in parents:
        node = node[int(t)] if isinstance(node, list) else node[t]
    node[int(last) if isinstance(node, list) else last] = value
src.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if opts.get("mark_done") and req.exists():
    data = json.loads(req.read_text(encoding="utf-8"))
    for r in data["requests"]:
        r["status"] = "done"
    req.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
'''


@dataclass
class Editor:
    url: str
    doc_path: Path
    project: Path
    session: EditorSession

    def set_agent(self, **opts):
        """Point the agent command (in a config file under tmp_path) at the fake agent."""
        opts_file = self.project.parent / "agent_opts.json"
        opts_file.write_text(json.dumps(opts), encoding="utf-8")
        command = [sys.executable, str(self.project.parent / "agent.py"),
                   "{source}", "{requests}", str(opts_file)]
        (self.project.parent / "cfg.toml").write_text(
            "[agent.simple]\ncommand = [" + ", ".join(json.dumps(a) for a in command) + "]\n",
            encoding="utf-8")


@pytest.fixture
def editor(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    doc_path = project / "doc.json"
    doc_path.write_text(json.dumps(DOC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text(AGENT_SCRIPT, encoding="utf-8")
    session = EditorSession(SimpleDocAdapter(), project, bus=EventBus(), runner=JobRunner(),
                            config_file=tmp_path / "cfg.toml",
                            confirm_file=tmp_path / "confirmed.json")
    httpd, url = make_server(session)
    stop = threading.Event()

    def poll():
        while not stop.wait(0.5):
            session.poll_external()

    threads = [threading.Thread(target=httpd.serve_forever, daemon=True),
               threading.Thread(target=poll, daemon=True)]
    for t in threads:
        t.start()
    yield Editor(url, doc_path, project, session)
    stop.set()
    session.cancel_job()
    deadline = time.monotonic() + 15
    while session.state()["job"] is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    httpd.shutdown()
    httpd.server_close()
    session.close()


@pytest.fixture
def page(browser, editor):
    context = browser.new_context()
    outside = []
    # Record, not route: a sync route handler would stall requests while the test sleeps.
    context.on("request", lambda r: urlsplit(r.url).hostname != "127.0.0.1" and outside.append(r.url))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert outside == [], f"requests left 127.0.0.1: {outside}"
    assert errors == [], f"uncaught page errors: {errors}"


def expect_json(path, pointer, value, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        current = ptr.get(json.loads(path.read_text(encoding="utf-8")), pointer)
        if current == value:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{pointer} is {current!r}, expected {value!r}")
        time.sleep(0.05)


def start_edit(frame, selector):
    element = frame.locator(selector)
    element.click()
    expect(element).to_have_class(re.compile(r"\bde-editing\b"))  # raw value loaded
    return element


def test_edit_bold_comment_agent_and_survival(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    start_edit(frame, '[data-src="/paragraphs/0"]')
    page.keyboard.press("End")
    page.keyboard.type(" plus")
    page.keyboard.press("Shift+Control+ArrowLeft")
    page.keyboard.press("Control+b")
    page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' **plus**")  # quote kept as '
    # the page swapped to the new render (the adapter renders ** as <strong>)
    expect(frame.locator('[data-src="/paragraphs/0"] strong')).to_have_text("plus")

    start_edit(frame, '[data-src="/table/0/0"]')
    frame.locator("#de-toolbar .de-comment").click()
    page.fill("#comment-instruction", "Right-align this column")
    page.click("#comment-save")
    expect(page.locator(".comment-item .status")).to_have_text("open")
    request = json.loads((editor.project / "edit_requests.json").read_text(encoding="utf-8"))
    assert [(r["pointer"], r["quote"], r["instruction"]) for r in request["requests"]] == [
        ("/table/0/0", "x", "Right-align this column")]
    expect(page.locator("#send-agent")).to_contain_text("1")  # open-request badge
    expect(frame.locator(".de-marker")).to_have_count(1)

    editor.set_agent(set_paths={"/title": "From agent"}, mark_done=True)
    page.click("#send-agent")
    expect(page.locator("#confirm-dialog")).to_contain_text("agent.py")
    page.click("#confirm-command")
    title = frame.locator('[data-src="/title"]')
    expect(title).to_have_text("From agent")
    expect(title).to_have_class(re.compile(r"\bde-changed\b"))
    expect(page.locator(".comment-item .status")).to_have_text("done")
    expect(page.locator("#log")).to_contain_text("agent started")

    editor.set_agent(set_paths={"/paragraphs/0": "regenerated"})
    page.click("#send-agent")
    lost = page.locator("#lost-edits")
    expect(lost).to_contain_text("one 'q' **plus**")
    expect(lost).to_contain_text("regenerated")
    page.click("#lost-edits .reapply")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' **plus**")
    expect(frame.locator('[data-src="/paragraphs/0"] strong')).to_have_text("plus")


def test_invalid_edit_stays_open_with_issue(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    title = start_edit(frame, '[data-src="/title"]')
    page.keyboard.type(" — x")
    expect(frame.locator(".de-issue")).to_contain_text("em-dash")
    page.keyboard.press("Enter")
    expect(frame.locator(".de-issue")).to_contain_text("em-dash")
    expect(title).to_have_class(re.compile(r"\bde-editing\b"))
    expect_json(editor.doc_path, "/title", "T")
    page.keyboard.press("Escape")
    expect(title).to_have_text("T")
    expect(frame.locator(".de-issue")).to_have_count(0)


def test_paste_is_plain_text_and_undo_redo(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    paragraph = start_edit(frame, '[data-src="/paragraphs/0"]')
    page.keyboard.press("End")
    paragraph.evaluate("""el => {
        const data = new DataTransfer();
        data.setData('text/html', '<b>bold</b>');
        data.setData('text/plain', ' pasted\\nline');
        el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
    }""")
    page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' pasted line")
    expect(paragraph).to_have_text("one 'q' pasted line")
    page.click("#undo")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q'")
    expect(paragraph).to_have_text("one 'q'")
    page.click("#redo")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' pasted line")


def test_number_cell_figure_toast_and_chart_fields(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    cell = start_edit(frame, '[data-src="/table/0/1"]')
    expect(frame.locator("#de-toolbar .de-bold")).to_be_hidden()
    page.keyboard.press("Control+a")
    page.keyboard.type("3")
    page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/table/0/1", 3)
    expect(page.locator(".toast")).to_contain_text("Figure changed")
    expect(page.locator(".comment-item .category")).to_have_text("figure")
    expect(cell).to_have_text("3")

    frame.locator('[data-src="/chart"]').click()
    field = page.locator('#element-fields [data-pointer="/chart/title"]')
    expect(field.locator("input")).to_have_value("c")
    field.locator("input").fill("Chart")
    field.locator("button").click()
    expect_json(editor.doc_path, "/chart/title", "Chart")
    expect(frame.locator('[data-src="/chart"] figcaption')).to_have_text("Chart")


def test_unrepresentable_edit_goes_to_raw_editor(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    paragraph = start_edit(frame, '[data-src="/paragraphs/0"]')
    paragraph.evaluate("el => { el.innerHTML = 'one <u>under</u>'; }")
    page.keyboard.press("Enter")
    raw = page.locator("#raw-editor textarea")
    expect(raw).to_have_value("one 'q'")
    raw.fill("one *raw*")
    page.click("#raw-save")
    expect_json(editor.doc_path, "/paragraphs/0", "one *raw*")
    expect(frame.locator('[data-src="/paragraphs/0"] em')).to_have_text("raw")


def test_state_events_do_not_disturb_typing_and_scroll_is_kept(page, editor):
    doc = dict(DOC, paragraphs=[f"paragraph {i}" for i in range(60)])
    editor.doc_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    page.set_viewport_size({"width": 1000, "height": 500})
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    expect(frame.locator('[data-src="/paragraphs/59"]')).to_have_text("paragraph 59")
    frame.locator('[data-src="/paragraphs/59"]').scroll_into_view_if_needed()
    doc_frame = page.frame(url=re.compile(r"/doc$"))
    scrolled = doc_frame.evaluate("scrollY")
    assert scrolled > 100
    paragraph = start_edit(frame, '[data-src="/paragraphs/59"]')
    page.keyboard.press("End")
    page.keyboard.type(" a")
    # a comment added elsewhere publishes a state event while the user types
    editor.session.add_request(pointer=None, element="document", quote="", category="text",
                               scope="document", instruction="from elsewhere")
    expect(page.locator(".comment-item")).to_have_count(1)
    page.keyboard.type("b")
    expect(paragraph).to_have_class(re.compile(r"\bde-editing\b"))
    page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/paragraphs/59", "paragraph 59 ab")
    expect(paragraph).to_have_text("paragraph 59 ab")
    expect(paragraph).not_to_have_class(re.compile(r"\bde-saving\b"))
    doc_frame = page.frame(url=re.compile(r"/doc$"))
    assert abs(doc_frame.evaluate("scrollY") - scrolled) < 5


def test_rebuild_shows_pages_tab_and_log(page, editor):
    page.goto(editor.url)
    page.click("#rebuild")
    expect(page.locator("#pane-pages")).to_be_visible()
    expect(page.locator("#pages-empty")).to_have_text("The build produced no page previews.")
    expect(page.locator("#log")).to_contain_text("build started")
    expect(page.locator("#rebuild")).to_be_enabled()
    assert (editor.project / "build.log").read_text(encoding="utf-8") == "built\n"
