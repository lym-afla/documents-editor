import json
from pathlib import Path

import pytest

from documents_editor.adapter import Issue, load_adapter


def _src():
    return {"title": "T", "paragraphs": ["**b** 1[^a]"], "table": [["x", 2]],
            "chart": {"title": "c", "labels": ["l"]}}


def test_simple_adapter_satisfies_protocol_and_marks_editable_elements(tmp_path):
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    src = _src()
    html = a.render_edit_html(src, tmp_path)
    assert 'data-src="/paragraphs/0" data-edit="inline"' in html
    assert 'data-src="/table/0/1" data-edit="number-cell"' in html
    assert "<strong>b</strong>" in html and "<sup>a</sup>" in html
    assert a.validate({**src, "title": "—"}) == [Issue("em-dash is not allowed", "/title")]
    assert a.panel_fields(src, "/chart")[0]["pointer"] == "/chart/title"
    assert a.panel_fields(src, "/chart")[1]["pointer"] == "/chart/labels/0"


def test_render_escapes_text(tmp_path):
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    html = a.render_edit_html({**_src(), "paragraphs": ["<script>x</script> **<b>**"]}, tmp_path)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_validate_unknown_marker_is_pointerless_and_empty_title_flagged():
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    assert a.validate({**_src(), "paragraphs": ["x[^zz]"]}) == [Issue("unknown note marker [^zz]")]
    assert a.validate({**_src(), "title": " "})[0].pointer == "/title"
    assert a.validate(_src()) == []


def test_check_text_and_figure_change():
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    assert a.check_text("/p", "a — b") == [Issue("em-dash is not allowed", "/p")]
    assert a.figure_change("/p", "1 200,5 units", "1200.5 units") is None
    assert a.figure_change("/p", "up 12%", "up 13%") is not None


def test_build_command_writes_build_log(tmp_path):
    import subprocess
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    cmd = a.build_command(tmp_path)
    subprocess.run(cmd.args, cwd=cmd.cwd, check=True)
    assert (tmp_path / "build.log").exists()
    assert a.build_result(tmp_path).pages == []


def test_sample_document_is_valid():
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    sample = Path(__file__).parent.parent / "examples" / "sample"
    assert a.validate(json.loads((sample / "doc.json").read_text(encoding="utf-8"))) == []


def test_load_adapter_reports_bad_spec():
    with pytest.raises(ValueError, match="nope:X"):
        load_adapter("nope:X")
    with pytest.raises(ValueError, match="examples.simple_doc:Missing"):
        load_adapter("examples.simple_doc:Missing")
