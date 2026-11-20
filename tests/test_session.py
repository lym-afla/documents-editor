import json
import queue
import subprocess
import sys
import time

import pytest

from documents_editor.events import EventBus
from documents_editor.runner import JobRunner
from documents_editor.session import EditorSession, Locked, Refused, StaleSource
from examples.simple_doc import SimpleDocAdapter

DOC = {"title": "T", "paragraphs": ["one 'q'"], "table": [["x", 2]],
       "chart": {"title": "c", "labels": ["l"]}}

AGENT_SCRIPT = r'''
import json, pathlib, sys, time
opts = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
src, req = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
print("agent started", flush=True)
time.sleep(opts.get("sleep", 0))
if "write_raw" in opts:
    src.write_text(opts["write_raw"], encoding="utf-8")
    sys.exit(0)
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


def wait_event(q, name, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"no {name!r} event")
        event, data = q.get(timeout=remaining)
        if event == name:
            return data


def write_doc(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def doc_path(tmp_path):
    p = tmp_path / "doc.json"
    write_doc(p, DOC)
    return p


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def make_session(tmp_path, doc_path, bus):
    made = []

    def make():
        s = EditorSession(SimpleDocAdapter(), tmp_path, bus=bus, runner=JobRunner(),
                          config_file=tmp_path / "cfg.toml",
                          confirm_file=tmp_path / "confirmed.json")
        made.append(s)
        return s

    yield make
    for s in made:
        s.cancel_job()
        deadline = time.monotonic() + 15
        while s.state()["job"] is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        s.close()


@pytest.fixture
def session(make_session):
    return make_session()


@pytest.fixture
def fake_agent(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(AGENT_SCRIPT, encoding="utf-8")

    def configure(**opts):
        (tmp_path / "agent_opts.json").write_text(json.dumps(opts), encoding="utf-8")
        command = [sys.executable, str(script), "{source}", "{requests}",
                   str(tmp_path / "agent_opts.json")]
        (tmp_path / "cfg.toml").write_text(
            "[agent.simple]\ncommand = [" + ", ".join(json.dumps(a) for a in command) + "]\n",
            encoding="utf-8")

    return configure


# -- brief tests --------------------------------------------------------------

def test_patch_writes_source_records_history_and_edit_log(session, doc_path):
    h = session.state()["hash"]
    session.patch("/paragraphs/0", "one 'q'", "one 'q' **b**", h)
    assert json.loads(doc_path.read_text())["paragraphs"][0] == "one 'q' **b**"
    assert session.state()["can_undo"]
    assert session.edit_log.entries() == {"/paragraphs/0": "one 'q' **b**"}
    session.undo(); assert json.loads(doc_path.read_text())["paragraphs"][0] == "one 'q'"


def test_patch_refuses_stale_value_and_invalid_result(session):
    h = session.state()["hash"]
    with pytest.raises(StaleSource):
        session.patch("/paragraphs/0", "not the old value", "x", h)
    with pytest.raises(StaleSource):
        session.patch("/paragraphs/0", "one 'q'", "x", "0" * 64)
    with pytest.raises(Refused) as e:
        session.patch("/paragraphs/0", "one 'q'", "bad — dash", h)
    assert e.value.issues[0].pointer == "/paragraphs/0"
    with pytest.raises(Refused) as e:  # pointerless error attributed to the edit
        session.patch("/paragraphs/0", "one 'q'", "ref [^zz]", h)
    assert e.value.issues[0].pointer == "/paragraphs/0"


def test_number_cell_and_figure_request(session):
    h = session.state()["hash"]
    r = session.patch("/table/0/1", 2, "2.5", h)
    assert json.loads(session.adapter.source_path(session.project_dir).read_text())["table"][0][1] == 2.5
    assert r["figure_request"]["old"] == "2" and session.state()["open_requests"] == 1


def test_agent_run_reports_changes_and_lost_edits(session, bus, fake_agent):
    h = session.state()["hash"]; session.patch("/paragraphs/0", "one 'q'", "mine", h)
    fake_agent(set_paths={"/paragraphs/0": "regenerated", "/title": "New"}, mark_done=True)
    q = bus.subscribe(); session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    assert report["ok"] and "/title" in report["changed"]
    assert report["lost"] == [{"pointer": "/paragraphs/0", "mine": "mine", "now": "regenerated"}]
    session.reapply(["/paragraphs/0"])
    assert session.value("/paragraphs/0")["value"] == "mine"


def test_locked_while_agent_runs(session, fake_agent):
    fake_agent(sleep=5); session.start_agent(confirm=True)
    with pytest.raises(Locked):
        session.patch("/title", "T", "X", session.state()["hash"])
    session.cancel_job()


def test_broken_agent_output_keeps_last_good_page(session, bus, fake_agent):  # Review Focus 2
    good = session.page_html()
    fake_agent(write_raw='{"title": ')
    q = bus.subscribe(); session.start_agent(confirm=True)
    assert wait_event(q, "agent-done")["ok"] is False
    assert session.page_html() == good and session.state()["valid"] is False
    session.undo(); assert session.state()["valid"] is True


def test_agent_requires_confirmation(session):
    assert session.agent_preview()["confirmed"] is False
    with pytest.raises(Refused):
        session.start_agent()


def test_external_change_detected(session, doc_path):
    d = json.loads(doc_path.read_text()); d["title"] = "Ext"
    doc_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    assert session.poll_external() and session.state()["can_undo"]


def test_comment_on_vanished_pointer_does_not_crash(session):  # Review Focus 4
    session.add_request(pointer="/paragraphs/7", element="paragraph", quote="old text",
                        category="format", scope="document", instruction="x")
    assert session.requests()[0]["pointer"] == "/paragraphs/7"
    assert session.state()["open_requests"] == 1


# -- rulings and edge cases ---------------------------------------------------

def test_unchanged_value_writes_nothing(session, doc_path):
    before = doc_path.read_bytes()
    r = session.patch("/table/0/1", 2, "2", session.state()["hash"])
    assert doc_path.read_bytes() == before and r["figure_request"] is None
    assert not session.state()["can_undo"]


def test_non_numeric_text_in_number_cell_becomes_string(session):
    session.patch("/table/0/1", 2, "n/a", session.state()["hash"])
    assert session.value("/table/0/1")["value"] == "n/a"


def test_figure_edit_back_to_original_removes_request(session):
    r = session.patch("/table/0/1", 2, "3", session.state()["hash"])
    assert r["figure_request"]["new"] == "3"
    r = session.patch("/table/0/1", 3, "2", r["hash"])
    assert r["figure_request"] is None and session.state()["open_requests"] == 0


def test_broken_requests_file_skips_figure_request_with_notice(session, tmp_path):
    (tmp_path / "edit_requests.json").write_text("{broken", encoding="utf-8")
    r = session.patch("/table/0/1", 2, "3", session.state()["hash"])
    assert session.value("/table/0/1")["value"] == 3 and r["figure_request"] is None
    assert "edit_requests.json is unreadable; figure change not recorded" in session.state()["notices"]


def test_poll_ignores_own_writes_and_undo(session):
    session.patch("/title", "T", "T2", session.state()["hash"])
    assert session.poll_external() is False
    session.undo()
    assert session.poll_external() is False


def test_undo_and_requests_locked_while_job_runs(session, fake_agent):
    session.patch("/title", "T", "T2", session.state()["hash"])
    fake_agent(sleep=5); session.start_agent(confirm=True)
    assert session.state()["locked"] and session.state()["job"] == "agent"
    with pytest.raises(Locked):
        session.undo()
    with pytest.raises(Locked):
        session.add_request(pointer=None, element="document", quote="", category="text",
                            scope="document", instruction="x")
    assert session.poll_external() is False


def test_undo_with_missing_snapshot_is_refused(session):
    import shutil
    session.patch("/title", "T", "T2", session.state()["hash"])
    first = session.history.entries()[0]["id"]
    shutil.rmtree(session.history.directory / first)
    with pytest.raises(Refused) as e:
        session.undo()
    assert e.value.issues


def test_agent_changed_lists_new_strings_and_targeted_edits_are_not_lost(session, bus, fake_agent):
    h = session.state()["hash"]
    session.patch("/paragraphs/0", "one 'q'", "mine", h)
    session.add_request(pointer="/paragraphs", element="paragraph", quote="mine",
                        category="text", scope="document", instruction="rewrite")
    fake_agent(set_paths={"/paragraphs/0": "rewritten", "/chart/labels/0": "L"})
    q = bus.subscribe(); session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    assert report["ok"] and report["lost"] == []
    assert sorted(report["changed"]) == ["/chart/labels/0", "/paragraphs/0"]


def test_agent_moves_edits_when_structure_shifts(session, bus, fake_agent, doc_path):
    h = session.state()["hash"]
    h = session.patch("/paragraphs/0", "one 'q'", "first", h)["hash"]
    fake_agent(set_paths={"/paragraphs/0": "inserted", "/table/0/0": "first"})
    q = bus.subscribe(); session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    assert report["lost"] == []
    assert session.edit_log.entries() == {"/table/0/0": "first"}


def test_agent_move_collision_reports_lost(session, bus, fake_agent):
    h = session.state()["hash"]
    h = session.patch("/paragraphs/0", "one 'q'", "same", h)["hash"]
    h = session.patch("/chart/labels/0", "l", "other", h)["hash"]
    # both user values vanish; "same" moves onto /chart/labels/0 which keeps its entry
    fake_agent(set_paths={"/paragraphs/0": "x", "/chart/labels/0": "same"})
    q = bus.subscribe(); session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    pointers = sorted(l["pointer"] for l in report["lost"])
    assert pointers == ["/chart/labels/0", "/paragraphs/0"]


def test_agent_without_changes_records_nothing(session, bus, fake_agent):
    fake_agent()
    count = len(session.history.entries())
    q = bus.subscribe(); session.start_agent(confirm=True)
    assert wait_event(q, "agent-done")["changed"] == []
    assert len(session.history.entries()) == count


def test_agent_streams_log_and_confirms_command(session, bus, fake_agent):
    fake_agent()
    preview = session.agent_preview()
    assert str(session.adapter.source_path(session.project_dir)) in preview["args"]
    q = bus.subscribe(); session.start_agent(confirm=True)
    assert wait_event(q, "log")["line"] == "agent started"
    wait_event(q, "agent-done")
    assert session.agent_preview()["confirmed"] is True


def test_accept_forgets_edit(session):
    session.patch("/title", "T", "T2", session.state()["hash"])
    session.accept(["/title"])
    assert session.edit_log.entries() == {}


def test_build_publishes_result(session, bus, tmp_path):
    q = bus.subscribe(); session.start_build()
    done = wait_event(q, "build-done")
    assert done == {"ok": True, "pages": [], "warnings": []}
    assert (tmp_path / "build.log").exists()


def test_invalid_source_on_disk_blocks_edits_and_keeps_page(session, doc_path):
    good = session.page_html()
    write_doc(doc_path, {**DOC, "title": "bad — dash"})
    assert session.poll_external()
    assert session.state()["valid"] is False and session.page_html() == good
    with pytest.raises(Refused):
        session.patch("/paragraphs/0", "one 'q'", "x", session.state()["hash"])


def test_lock_file_notice_and_close(make_session, tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / ".editor").mkdir()
        (tmp_path / ".editor" / "lock").write_text(str(other.pid))
        s = make_session()
        assert "another editor is open on this project" in s.state()["notices"]
    finally:
        other.kill(); other.wait()
    (tmp_path / ".editor" / "lock").unlink()
    s2 = make_session()
    assert (tmp_path / ".editor" / "lock").read_text().strip() == str(__import__("os").getpid())
    s2.close()
    assert not (tmp_path / ".editor" / "lock").exists()


def test_non_canonical_source_notice(doc_path, make_session):
    doc_path.write_text(json.dumps(DOC), encoding="utf-8")
    s = make_session()
    assert any("doc.json" in n for n in s.state()["notices"])


def test_event_bus_unsubscribe():
    b = EventBus()
    q = b.subscribe(); b.publish("x", 1)
    assert q.get_nowait() == ("x", 1)
    b.unsubscribe(q); b.publish("x", 2)
    with pytest.raises(queue.Empty):
        q.get_nowait()


# -- review fixes -------------------------------------------------------------

def run_agent(session, bus, fake_agent, **opts):
    fake_agent(**opts)
    q = bus.subscribe()
    session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    bus.unsubscribe(q)
    return report


def test_targeted_edit_superseded_is_not_lost_on_next_run(session, bus, fake_agent):
    session.patch("/paragraphs/0", "one 'q'", "mine", session.state()["hash"])
    session.add_request(pointer="/paragraphs", element="paragraph", quote="mine",
                        category="text", scope="document", instruction="rewrite")
    first = run_agent(session, bus, fake_agent,
                      set_paths={"/paragraphs/0": "rewritten"}, mark_done=True)
    assert first["lost"] == [] and session.edit_log.entries() == {}
    assert run_agent(session, bus, fake_agent)["lost"] == []


def test_format_request_covers_exact_pointer_only(session, bus, fake_agent):
    session.patch("/table/0/0", "x", "mine", session.state()["hash"])
    session.add_request(pointer="/table", element="table", quote="", category="format",
                        scope="document", instruction="right-align")
    report = run_agent(session, bus, fake_agent, set_paths={"/table/0/0": "gone"})
    assert [l["pointer"] for l in report["lost"]] == ["/table/0/0"]


def test_undo_rolls_back_edit_log(session, bus, fake_agent):
    session.patch("/title", "T", "T2", session.state()["hash"])
    session.undo()
    assert session.edit_log.entries() == {}
    assert run_agent(session, bus, fake_agent)["lost"] == []
    session.redo()
    assert session.edit_log.entries() == {"/title": "T2"}


def test_revert_after_agent_restores_edit_log(session, bus, fake_agent):
    session.patch("/paragraphs/0", "one 'q'", "mine", session.state()["hash"])
    run_agent(session, bus, fake_agent,
              set_paths={"/paragraphs/0": "x", "/chart/labels/0": "mine"})
    assert session.edit_log.entries() == {"/chart/labels/0": "mine"}
    session.undo()
    assert session.edit_log.entries() == {"/paragraphs/0": "mine"}
    assert session.value("/paragraphs/0")["value"] == "mine"


def test_accept_does_not_create_history_entry(session, bus, fake_agent):
    session.patch("/title", "T", "T2", session.state()["hash"])
    count = len(session.history.entries())
    q = bus.subscribe()
    session.accept(["/title"])
    assert wait_event(q, "state")["can_undo"]
    run_agent(session, bus, fake_agent)
    assert len(session.history.entries()) == count


def test_accept_locked_and_empty_reapply_is_noop(session, fake_agent, doc_path):
    before = doc_path.read_bytes(); count = len(session.history.entries())
    assert session.reapply([])["hash"] == session.state()["hash"]
    assert doc_path.read_bytes() == before and len(session.history.entries()) == count
    fake_agent(sleep=5); session.start_agent(confirm=True)
    with pytest.raises(Locked):
        session.accept(["/title"])


def test_agent_preview_wraps_config_error(session, tmp_path):
    (tmp_path / "cfg.toml").write_text('[agent.simple]\ncommand = ["{nope}"]\n')
    with pytest.raises(Refused):
        session.agent_preview()


def test_broken_agent_report_has_cancel_flags(session, bus, fake_agent):
    report = run_agent(session, bus, fake_agent, write_raw="{")
    assert report["ok"] is False and report["cancelled"] is False and report["timed_out"] is False


def test_source_that_is_a_directory_does_not_crash(doc_path, make_session):
    doc_path.unlink(); doc_path.mkdir()
    s = make_session()
    assert s.state()["valid"] is False and s.state()["issues"]
    assert s.poll_external() is False


@pytest.mark.skipif(sys.platform == "win32" or getattr(__import__("os"), "geteuid", lambda: 1)() == 0,
                    reason="permission bits do not block reading here")
def test_unreadable_source_starts_invalid_and_poll_never_raises(doc_path, make_session, session):
    doc_path.chmod(0)
    try:
        assert session.poll_external() is True  # the readable file became unreadable
        assert session.state()["valid"] is False
        assert session.poll_external() is False
        s = make_session()
        assert s.state()["valid"] is False
    finally:
        doc_path.chmod(0o644)
