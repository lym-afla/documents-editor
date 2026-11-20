"""Numeric token extraction for detecting changed figures."""
from __future__ import annotations

import re

_NUMBER = re.compile(
    r"(?<![\d.,])"
    r"(?P<num>(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)|\d+)(?:[.,]\d+)?)"
    r"(?P<suffix>[%xх](?![^\W\d_]))?"
)
_MINUS = "-−"


def numeric_tokens(text: str) -> list[str]:
    """Numeric tokens in order, normalised so formatting-only changes compare equal."""
    tokens: list[str] = []
    for m in _NUMBER.finditer(text):
        start, end = m.span()
        number = re.sub(r"[ \u00a0\u202f]", "", m["num"]).replace(",", ".")
        before = text[start - 1] if start else ""
        before2 = text[start - 2] if start > 1 else ""
        negative = before == "(" and text[end:end + 1] == ")"
        if before and before in _MINUS and not (before2 and (before2.isalnum() or before2 == "_")):
            negative = True
        tokens.append(("-" if negative else "") + number + (m["suffix"] or ""))
    return tokens
