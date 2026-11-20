"""RFC 6901 JSON pointers over plain dict/list documents."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


class PointerError(LookupError):
    """Raised when a pointer is malformed or does not resolve."""


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def parse(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PointerError(f"pointer must start with '/': {pointer!r}")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def format(tokens: Iterable[str | int]) -> str:
    return "".join("/" + _escape(str(t)) for t in tokens)


def _index(token: str, length: int, pointer: str) -> int:
    if not token.isascii() or not token.isdigit() or (token != "0" and token.startswith("0")):
        raise PointerError(f"bad array index {token!r} in {pointer!r}")
    i = int(token)
    if i >= length:
        raise PointerError(f"index out of range in {pointer!r}")
    return i


def _step(node: Any, token: str, pointer: str) -> Any:
    if isinstance(node, dict):
        if token not in node:
            raise PointerError(f"missing key {token!r} in {pointer!r}")
        return node[token]
    if isinstance(node, list):
        return node[_index(token, len(node), pointer)]
    raise PointerError(f"cannot descend into scalar in {pointer!r}")


def get(doc: Any, pointer: str) -> Any:
    node = doc
    for token in parse(pointer):
        node = _step(node, token, pointer)
    return node


def set_value(doc: Any, pointer: str, value: Any) -> None:
    tokens = parse(pointer)
    if not tokens:
        raise PointerError("cannot replace the document root")
    parent = get(doc, format(tokens[:-1]))
    last = tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PointerError(f"missing key {last!r} in {pointer!r}")
        parent[last] = value
    elif isinstance(parent, list):
        parent[_index(last, len(parent), pointer)] = value
    else:
        raise PointerError(f"cannot descend into scalar in {pointer!r}")


def iter_strings(doc: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(doc, str):
        yield prefix, doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from iter_strings(v, f"{prefix}/{_escape(str(k))}")
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from iter_strings(v, f"{prefix}/{i}")


def find_value(doc: Any, value: str, near: str) -> str | None:
    near_tokens = parse(near)
    best = -1
    winners: list[str] = []
    for ptr, s in iter_strings(doc):
        if s != value:
            continue
        shared = 0
        for a, b in zip(parse(ptr), near_tokens):
            if a != b:
                break
            shared += 1
        if shared > best:
            best, winners = shared, [ptr]
        elif shared == best:
            winners.append(ptr)
    return winners[0] if len(winners) == 1 else None
