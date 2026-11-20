"""End to end: the editor UI in headless Chromium against a real server and a fake agent."""
import base64
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
from documents_editor.adapter import BuildResult, Issue
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
def make_editor(tmp_path):
    """Start an editor (server + external-change poll) on a fresh project; config under tmp_path."""
    started = []

    def make(adapter=None):
        project = tmp_path / "project"
        project.mkdir()
        doc_path = project / "doc.json"
        doc_path.write_text(json.dumps(DOC, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (tmp_path / "agent.py").write_text(AGENT_SCRIPT, encoding="utf-8")
        session = EditorSession(adapter or SimpleDocAdapter(), project, bus=EventBus(),
                                runner=JobRunner(), config_file=tmp_path / "cfg.toml",
                                confirm_file=tmp_path / "confirmed.json")
        httpd, url = make_server(session)
        stop = threading.Event()

        def poll():
            while not stop.wait(0.5):
                session.poll_external()

        for target in (httpd.serve_forever, poll):
            threading.Thread(target=target, daemon=True).start()
        started.append((session, httpd, stop))
        return Editor(url, doc_path, project, session)

    yield make
    for session, httpd, stop in started:
        stop.set()
        session.cancel_job()
        deadline = time.monotonic() + 15
        while session.state()["job"] is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        httpd.shutdown()
        httpd.server_close()
        session.close()


@pytest.fixture
def editor(make_editor):
    return make_editor()


@pytest.fixture
def page(browser):
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
    # nested formatting (Ctrl+I inside bold) and a stray element cannot become markup
    paragraph.evaluate("el => { el.innerHTML = 'one <b>bo<i>ld</i></b> <u>under</u>'; }")
    page.keyboard.press("Enter")
    raw = page.locator("#raw-editor textarea")
    expect(raw).to_have_value("one **bold** under")  # the user's text, flattened
    expect(paragraph).to_have_class(re.compile(r"\bde-editing\b"))  # still open, not discarded
    expect(frame.locator(".de-issue")).to_contain_text("Nested formatting")
    page.click("#rebuild")  # commit-now reports the open edit truthfully
    expect(page.locator(".toast").last).to_contain_text("Finish or cancel the edit")
    expect(raw).to_have_value("one **bold** under")
    assert not (editor.project / "build.log").exists()
    page.click("#raw-save")
    expect_json(editor.doc_path, "/paragraphs/0", "one **bold** under")
    expect(frame.locator('[data-src="/paragraphs/0"] strong')).to_have_text("bold")
    expect(frame.locator(".de-editing")).to_have_count(0)
    expect(page.locator("#raw-editor")).to_be_hidden()


def test_outside_change_is_not_overwritten_by_panel_save(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    frame.locator('[data-src="/chart"]').click()
    title = page.locator('#element-fields [data-pointer="/chart/title"]')
    label = page.locator('#element-fields [data-pointer="/chart/labels/0"]')
    expect(title.locator("input")).to_have_value("c")
    title.locator("input").fill("mine")
    label.locator("input").fill("unsaved label")
    doc = json.loads(editor.doc_path.read_text(encoding="utf-8"))
    doc["chart"]["title"] = "outside"
    editor.doc_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    expect(frame.locator('[data-src="/chart"] figcaption')).to_have_text("outside")  # poll saw it
    title.locator("button").click()
    expect(title.locator(".conflict")).to_contain_text("outside")
    expect(title.locator("input")).to_have_value("mine")
    time.sleep(0.3)
    expect_json(editor.doc_path, "/chart/title", "outside")
    title.locator("button").click()  # a deliberate second save replaces it
    expect_json(editor.doc_path, "/chart/title", "mine")
    expect(title.locator(".conflict")).to_be_hidden()
    expect(label.locator("input")).to_have_value("unsaved label")  # other fields keep their text
    # an outside change to another field does not block a save (rebase)
    doc = json.loads(editor.doc_path.read_text(encoding="utf-8"))
    doc["title"] = "Elsewhere"
    editor.doc_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    expect(frame.locator('[data-src="/title"]')).to_have_text("Elsewhere")
    label.locator("button").click()
    expect_json(editor.doc_path, "/chart/labels/0", "unsaved label")
    expect_json(editor.doc_path, "/title", "Elsewhere")


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
    page.frame(url=re.compile(r"/doc$")).wait_for_function(
        f"document.readyState === 'complete' && Math.abs(scrollY - {scrolled}) < 5", timeout=10_000)


def test_rebuild_shows_pages_tab_and_log(page, editor):
    page.goto(editor.url)
    page.click("#rebuild")
    expect(page.locator("#pane-pages")).to_be_visible()
    expect(page.locator("#pages-empty")).to_have_text("The build produced no page previews.")
    expect(page.locator("#log")).to_contain_text("build started")
    expect(page.locator("#rebuild")).to_be_enabled()
    assert (editor.project / "build.log").read_text(encoding="utf-8") == "built\n"


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class PagedAdapter(SimpleDocAdapter):
    def build_result(self, project_dir):
        pages = [project_dir / "page-1.png", project_dir / "page-2.png"]
        for page in pages:
            page.write_bytes(PNG)
        return BuildResult(pages, [Issue("Table overflows on page 2", severity="warning"),
                                   Issue("Widow line", severity="warning")])


def test_build_warning_links_to_its_page_preview(page, make_editor):
    editor = make_editor(PagedAdapter())
    page.goto(editor.url)
    page.click("#rebuild")
    expect(page.locator("#page-previews figure")).to_have_count(2)
    expect(page.locator("#build-warnings .page-link")).to_have_count(1)  # "Widow line" names no page
    page.click("#build-warnings .page-link")
    expect(page.locator('#page-previews figure[data-page="page-2.png"]')).to_have_class(re.compile(r"\btarget\b"))
    expect(page.locator('#page-previews figure[data-page="page-1.png"]')).not_to_have_class(re.compile(r"\btarget\b"))


def test_raw_hand_over_keeps_footnote_chip_inside_bold(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    paragraph = start_edit(frame, '[data-src="/paragraphs/0"]')
    # what Ctrl+B over a selection that spans a footnote chip leaves behind
    paragraph.evaluate("""el => { el.innerHTML = 'one <b>this <i>year</i>'
        + '<span class="de-fn" contenteditable="false" data-fn="a">a</span> and</b> end'; }""")
    page.keyboard.press("Enter")
    raw = page.locator("#raw-editor textarea")
    expect(raw).to_have_value("one **this year**[^a] **and** end")
    page.click("#raw-save")
    expect_json(editor.doc_path, "/paragraphs/0", "one **this year**[^a] **and** end")
    expect(frame.locator('[data-src="/paragraphs/0"] sup')).to_have_text("a")


def test_late_save_does_not_reload_under_a_new_edit(page, editor):
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    held = []

    def hold(route):  # hold the save's request until released
        held.append(route)

    page.route("**/api/patch", hold)
    start_edit(frame, '[data-src="/paragraphs/0"]')
    page.keyboard.press("End")
    page.keyboard.type(" x")
    page.keyboard.press("Enter")
    while not held:
        page.wait_for_timeout(20)
    page.keyboard.press("Escape")  # the save is still in flight
    title = start_edit(frame, '[data-src="/title"]')
    held[0].continue_()
    page.unroute("**/api/patch", hold)
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' x")
    page.wait_for_timeout(500)
    expect(title).to_have_class(re.compile(r"\bde-editing\b"))  # not reloaded away
    page.keyboard.press("Escape")  # the deferred reload now shows the saved text
    expect(frame.locator('[data-src="/paragraphs/0"]')).to_have_text("one 'q' x")


def test_shell_loading_after_the_page_still_connects(page, editor):
    editor.session.add_request(pointer="/title", element="h1", quote="T", category="text",
                               scope="document", instruction="shorter")
    held = []

    def hold(route):
        held.append(route)

    page.route("**/static/app.js", hold)
    page.goto(editor.url, wait_until="commit")
    frame = page.frame_locator("iframe#doc")
    expect(frame.locator("#de-toolbar")).to_be_attached()  # page.js ran and announced itself
    held[0].continue_()
    page.unroute("**/static/app.js", hold)
    expect(frame.locator(".de-marker")).to_have_count(1)  # the shell reached the page
