from documents_editor.history import History


def test_undo_redo_restores_source_and_requests_together(tmp_path):
    src, req = tmp_path / "doc.json", tmp_path / "edit_requests.json"
    src.write_text("v0"); h = History(tmp_path / ".editor/history", [src, req])
    h.record("open")
    src.write_text("v1"); req.write_text("r1"); h.record("agent")
    assert h.undo() and src.read_text() == "v0" and not req.exists()
    assert h.redo() and src.read_text() == "v1" and req.read_text() == "r1"
    assert h.redo() is None


def test_new_record_after_undo_drops_redo(tmp_path):
    src = tmp_path / "doc.json"; src.write_text("v0")
    h = History(tmp_path / "h", [src]); h.record("open")
    src.write_text("v1"); h.record("edit"); h.undo()
    src.write_text("v2"); h.record("edit")
    assert [e["reason"] for e in h.entries()] == ["open", "edit"] and not h.can_redo


def test_index_survives_restart_and_cap_prunes(tmp_path):
    src = tmp_path / "doc.json"; h = History(tmp_path / "h", [src], cap=3)
    for i in range(5):
        src.write_text(f"v{i}"); h.record("edit")
    h2 = History(tmp_path / "h", [src], cap=3)
    assert len(h2.entries()) == 3 and h2.current() == h.current()
    dirs = [p for p in (tmp_path / "h").iterdir() if p.is_dir()]
    assert len(dirs) == 3


def test_invalid_json_bytes_round_trip_and_ids_unique_sorted(tmp_path, monkeypatch):
    src = tmp_path / "doc.json"
    h = History(tmp_path / "h", [src])
    src.write_bytes(b'{"broken": \xff\xfe')
    ids = [h.record("agent") for _ in range(20)]
    assert ids == sorted(ids) and len(set(ids)) == 20
    src.write_bytes(b"{}")
    h.record("edit")
    assert h.undo() == ids[-1] and src.read_bytes() == b'{"broken": \xff\xfe'
    assert h.can_undo and h.can_redo and h.current() == ids[-1]


def _frozen(monkeypatch, *times):
    from datetime import datetime, timezone
    from documents_editor import history
    it = iter(times)
    last = []
    def now():
        try:
            last[:] = [next(it)]
        except StopIteration:
            pass
        return datetime(2026, 1, 1, 0, 0, 0, last[0], tzinfo=timezone.utc)
    monkeypatch.setattr(history, "_now", now)


def test_frozen_clock_with_different_slugs_terminates(tmp_path, monkeypatch):
    _frozen(monkeypatch, 5)
    src = tmp_path / "doc.json"; src.write_text("x")
    h = History(tmp_path / "h", [src])
    ids = [h.record(r) for r in ("edit", "agent", "zzz", "agent")]
    assert ids == sorted(ids) and len(set(ids)) == 4


def test_clock_going_backwards_keeps_ids_sorted(tmp_path, monkeypatch):
    _frozen(monkeypatch, 500, 100, 50)
    src = tmp_path / "doc.json"; src.write_text("x")
    h = History(tmp_path / "h", [src])
    ids = [h.record("edit") for _ in range(3)]
    assert ids == sorted(ids) and len(set(ids)) == 3
    assert History(tmp_path / "h", [src]).current() == ids[-1]


def test_missing_snapshot_dir_raises_and_leaves_files(tmp_path):
    import shutil
    import pytest
    src = tmp_path / "doc.json"; src.write_text("v0")
    h = History(tmp_path / "h", [src]); first = h.record("open")
    src.write_text("v1"); h.record("edit")
    shutil.rmtree(tmp_path / "h" / first)
    with pytest.raises(FileNotFoundError):
        h.undo()
    assert src.read_text() == "v1" and h.current() != first and h.can_undo
