import json

import pytest

from documents_editor.requests import RequestsFile


def _add(f, **kw):
    args = dict(pointer="/t", element="table", quote="q", category="format",
                scope="document", instruction="right-align")
    args.update(kw)
    return f.add(**args)


def test_add_update_delete_and_ids(tmp_path):
    f = RequestsFile(tmp_path / "edit_requests.json")
    a = f.add(pointer="/t", element="table", quote="q", category="format", scope="document", instruction="right-align")
    b = f.add(pointer=None, element="document", quote="", category="text", scope="house_style", instruction="x")
    assert a["id"] != b["id"] and a["status"] == "open" and f.open_count() == 2
    assert a["agent_note"] == "" and a["id"].startswith("r-")
    f.update(a["id"], status="done"); f.delete(b["id"])
    assert [r["status"] for r in f.load()[0]] == ["done"]


def test_figure_upsert_keeps_first_old_and_removes_on_restore(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    f.upsert_figure(pointer="/c", old="407.0", new="412.5", quote="412.5", note="n")
    r = f.upsert_figure(pointer="/c", old="412.5", new="413", quote="413", note="n")
    assert r["old"] == "407.0" and r["new"] == "413" and len(f.load()[0]) == 1
    assert r["origin"] == "editor" and r["category"] == "figure" and r["instruction"] == "n"
    assert set(r) == {"id", "created", "pointer", "element", "quote", "category", "origin",
                      "scope", "instruction", "old", "new", "status", "agent_note"}
    assert (r["element"], r["scope"], r["status"], r["agent_note"]) == ("text", "document", "open", "")
    assert f.upsert_figure(pointer="/c", old="413", new="407,0", quote="", note="n") is None
    assert f.load()[0] == []


def test_figure_formatting_only_change_creates_nothing(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    assert f.upsert_figure(pointer="/c", old="1 234,5", new="1234.5", quote="", note="n") is None
    assert not (tmp_path / "r.json").exists()


def test_tolerates_broken_file_and_preserves_unknown_fields(tmp_path):
    p = tmp_path / "r.json"; p.write_text("{oops", encoding="utf-8")
    reqs, warning = RequestsFile(p).load()
    assert reqs == [] and warning
    p.write_text(json.dumps({"version": 1, "requests": [{"id": "r-1", "status": "open", "x_agent": 1}]}))
    f = RequestsFile(p); f.update("r-1", status="done")
    assert json.loads(p.read_text())["requests"][0]["x_agent"] == 1


def test_missing_file_is_empty(tmp_path):
    assert RequestsFile(tmp_path / "none.json").load() == ([], None)
    assert RequestsFile(tmp_path / "none.json").open_count() == 0


@pytest.mark.parametrize("content", ["{oops", "[]", '{"requests": {}}', '{"requests": [1]}'])
def test_wrong_shape_warns_and_writes_are_refused(tmp_path, content):
    p = tmp_path / "r.json"; p.write_text(content, encoding="utf-8")
    f = RequestsFile(p)
    reqs, warning = f.load()
    assert reqs == [] and warning
    with pytest.raises(ValueError):
        _add(f)
    with pytest.raises(ValueError):
        f.upsert_figure(pointer="/c", old="1", new="2", quote="", note="n")
    with pytest.raises(ValueError):
        f.delete("r-1")
    assert p.read_text(encoding="utf-8") == content


def test_write_rereads_file_and_keeps_external_changes_and_top_level_fields(tmp_path):
    p = tmp_path / "r.json"
    f = RequestsFile(p)
    a = _add(f)
    doc = json.loads(p.read_text())
    doc["agent_meta"] = {"k": 1}
    doc["requests"].append({"id": "r-ext", "status": "open", "custom": [1]})
    p.write_text(json.dumps(doc), encoding="utf-8")
    _add(f, instruction="second")
    out = json.loads(p.read_text())
    assert out["agent_meta"] == {"k": 1} and out["version"] == 1
    assert [r["id"] for r in out["requests"]][:2] == [a["id"], "r-ext"]
    assert len(out["requests"]) == 3 and f.open_count() == 3


def test_ids_unique_when_external_ids_collide(tmp_path):
    p = tmp_path / "r.json"; f = RequestsFile(p)
    a = _add(f)
    n = a["id"].rsplit("-", 1)[1]
    p.write_text(json.dumps({"version": 1, "requests": [{"id": a["id"]}, {"id": f"{a['id'][:-4]}{int(n) + 1:04d}"}]}))
    b = _add(f)
    assert b["id"] not in (a["id"], f"{a['id'][:-4]}{int(n) + 1:04d}")


def test_update_validation(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    a = _add(f)
    with pytest.raises(ValueError):
        f.update(a["id"], quote="x")
    with pytest.raises(ValueError):
        f.update(a["id"], scope="bogus")
    with pytest.raises(KeyError):
        f.update("nope", status="done")
    with pytest.raises(KeyError):
        f.delete("nope")
    assert f.update(a["id"], instruction="new", scope="house_style")["instruction"] == "new"


def test_figure_request_stays_manageable(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    r = f.upsert_figure(pointer="/c", old="1", new="2", quote="2", note="n")
    u = f.update(r["id"], status="done", instruction="changed", scope="house_style")
    assert (u["status"], u["instruction"], u["scope"]) == ("done", "changed", "house_style")
    with pytest.raises(ValueError):
        f.update(r["id"], category="text")
    with pytest.raises(ValueError):
        f.update(r["id"], category="figure")
    a = _add(f)
    with pytest.raises(ValueError):
        f.update(a["id"], category="figure")
    f.delete(r["id"])
    assert [x["id"] for x in f.load()[0]] == [a["id"]]


def test_update_rejects_unknown_status(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    a = _add(f)
    with pytest.raises(ValueError, match="status"):
        f.update(a["id"], status="finished")
    for status in ("open", "done", "needs_input", "declined"):
        assert f.update(a["id"], status=status)["status"] == status
