"""Log of the user's direct edits and the check that they survived an agent run."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pointer import PointerError, find_value, get
from .store import atomic_write_text


@dataclass(frozen=True)
class Lost:
    pointer: str
    mine: Any
    now: Any | None  # None when the pointer no longer resolves


class EditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def entries(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, entries: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path, json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
        )

    def record(self, pointer: str, value: Any) -> None:
        entries = self.entries()
        entries[pointer] = value
        self._save(entries)

    def forget(self, pointer: str) -> None:
        entries = self.entries()
        if pointer in entries:
            del entries[pointer]
            self._save(entries)

    def move(self, old: str, new: str) -> None:
        entries = self.entries()
        if old not in entries:
            return
        entries[new] = entries.pop(old)
        self._save(entries)


def survival_check(
    entries: dict[str, Any], after: Any, targeted: set[str]
) -> tuple[list[Lost], dict[str, str]]:
    lost: list[Lost] = []
    moves: dict[str, str] = {}
    for pointer, mine in entries.items():
        try:
            now: Any = get(after, pointer)
            resolved = True
        except PointerError:
            now, resolved = None, False
        if resolved and now == mine:
            continue
        if pointer in targeted:
            continue
        if isinstance(mine, str):
            try:
                new = find_value(after, mine, near=pointer)
            except PointerError:
                new = None
            if new is not None:
                moves[pointer] = new
                continue
        lost.append(Lost(pointer, mine, now))
    return lost, moves
