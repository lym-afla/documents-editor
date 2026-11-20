import json

import pytest

from documents_editor.store import (
    SourceStore,
    SourceUnreadable,
    StaleSource,
    atomic_write_text,
)


def test_round_trip_is_byte_identical_for_canonical_file(tmp_path):
    p = tmp_path / "doc.json"
    text = json.dumps({"b": "Ёж – 'x'", "a": [1, 2.5]}, ensure_ascii=False, indent=2) + "\n"
    p.write_text(text, encoding="utf-8")
    store = SourceStore(p)
    doc, h = store.read()
    store.write(doc, h)
    assert p.read_text(encoding="utf-8") == text
    assert store.is_canonical()


def test_detects_non_canonical_style(tmp_path):
    for text in ['{"a": 1.10}', '{\n    "a": 1\n}', '{"a": "\\u0451"}']:
        p = tmp_path / "doc.json"
        p.write_text(text, encoding="utf-8")
        assert SourceStore(p).is_canonical() is False


def test_write_refuses_stale_hash(tmp_path):
    p = tmp_path / "doc.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    store = SourceStore(p)
    doc, h = store.read()
    p.write_text('{"a": 2}', encoding="utf-8")
    with pytest.raises(StaleSource):
        store.write(doc, h)


def test_unreadable_source(tmp_path):
    p = tmp_path / "doc.json"
    p.write_text('{"a": ', encoding="utf-8")
    with pytest.raises(SourceUnreadable):
        SourceStore(p).read()
    with pytest.raises(SourceUnreadable):
        SourceStore(tmp_path / "missing.json").read()
    assert SourceStore(tmp_path / "missing.json").current_hash() is None


def test_atomic_write_leaves_no_temp_files(tmp_path):
    atomic_write_text(tmp_path / "x.json", "{}")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["x.json"]


def test_trailing_newline_kept_as_found(tmp_path):
    p = tmp_path / "doc.json"
    p.write_text('{\n  "a": 1\n}', encoding="utf-8")
    store = SourceStore(p)
    doc, h = store.read()
    store.write(doc, h)
    assert p.read_text(encoding="utf-8") == '{\n  "a": 1\n}'
    assert store.is_canonical()
