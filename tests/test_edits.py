from documents_editor.edits import EditLog, survival_check


def test_kept_moved_targeted_and_lost():
    after = {"p": ["new intro", "my text", "agent rewrote"], "q": "x"}
    entries = {"/p/0": "my text", "/p/2": "mine 2", "/q": "mine q", "/p/9": "gone"}
    lost, moves = survival_check(entries, after, targeted={"/q"})
    assert moves == {"/p/0": "/p/1"}
    assert {l.pointer for l in lost} == {"/p/2", "/p/9"}
    assert next(l for l in lost if l.pointer == "/p/9").now is None
    assert next(l for l in lost if l.pointer == "/p/2").now == "agent rewrote"


def test_unchanged_value_is_kept():
    assert survival_check({"/a": "x"}, {"a": "x"}, set()) == ([], {})


def test_malformed_pointer_is_lost_not_raised():
    lost, moves = survival_check({"bad": "x"}, {"a": "y"}, set())
    assert moves == {} and [(l.pointer, l.now) for l in lost] == [("bad", None)]


def test_non_string_edit_is_never_moved():
    lost, _ = survival_check({"/a": 5}, {"a": 6, "b": 5}, set())
    assert [l.pointer for l in lost] == ["/a"]


def test_edit_log_persists(tmp_path):
    log = EditLog(tmp_path / "edits.json")
    log.record("/a", "x")
    log.move("/a", "/b")
    assert EditLog(tmp_path / "edits.json").entries() == {"/b": "x"}


def test_forget_and_move_overwrites(tmp_path):
    log = EditLog(tmp_path / ".editor" / "edits.json")
    log.record("/a", "1")
    log.record("/b", "2")
    log.move("/a", "/b")
    assert log.entries() == {"/b": "1"}
    log.forget("/b")
    log.forget("/missing")
    assert log.entries() == {}


def test_missing_or_corrupt_log_is_empty(tmp_path):
    path = tmp_path / "edits.json"
    assert EditLog(path).entries() == {}
    path.write_text("{not json", encoding="utf-8")
    assert EditLog(path).entries() == {}
