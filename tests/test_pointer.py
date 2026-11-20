import copy

import pytest

from documents_editor.pointer import (
    PointerError, find_value, format, get, iter_strings, set_value)

DOC = {"sections": [{"heading": "A", "blocks": [{"paragraphs": ["p0", "p1"]}]}],
       "a/b": {"m~n": "x"}, "n": 4}


def test_get_and_escape():
    assert get(DOC, "/sections/0/blocks/0/paragraphs/1") == "p1"
    assert get(DOC, "/a~1b/m~0n") == "x"
    assert format(["a/b", "m~n"]) == "/a~1b/m~0n"
    assert get(DOC, "") is DOC


def test_get_missing_raises_pointer_error():
    for bad in ["/sections/5", "/sections/x", "/nope", "sections", "/sections/-1", "/sections/01"]:
        with pytest.raises(PointerError):
            get(DOC, bad)


def test_set_value_replaces_only_existing():
    doc = copy.deepcopy(DOC)
    set_value(doc, "/sections/0/heading", "B")
    assert doc["sections"][0]["heading"] == "B"
    with pytest.raises(PointerError):
        set_value(doc, "/sections/0/new", "x")


def test_iter_strings_in_document_order():
    assert [p for p, _ in iter_strings(DOC)] == [
        "/sections/0/heading", "/sections/0/blocks/0/paragraphs/0",
        "/sections/0/blocks/0/paragraphs/1", "/a~1b/m~0n"]


def test_find_value_prefers_nearest_and_refuses_ties():
    doc = {"s": [{"p": ["x", "y"]}, {"p": ["y"]}]}
    assert find_value(doc, "y", near="/s/0/p/0") == "/s/0/p/1"
    assert find_value(doc, "y", near="/s/1/p/3") == "/s/1/p/0"
    assert find_value(doc, "y", near="/t") is None   # tie at depth 0
    assert find_value(doc, "zzz", near="/s") is None
