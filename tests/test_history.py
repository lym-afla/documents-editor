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
