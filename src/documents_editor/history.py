"""Snapshot history with undo and redo over a set of tracked files."""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import atomic_write_bytes, atomic_write_text

_STAMP_FORMAT = "%Y%m%d-%H%M%S-%f"
_STAMP_LEN = 22


def _now() -> datetime:
    return datetime.now(timezone.utc)


class History:
    def __init__(self, directory: Path, tracked: list[Path], cap: int = 200) -> None:
        self.directory = Path(directory)
        self.tracked = [Path(p) for p in tracked]
        self.cap = cap
        self._cursor = -1
        self._entries: list[dict[str, Any]] = []
        self.notice: str | None = None  # set when a damaged index was set aside
        index = self.directory / "index.json"
        if index.exists():
            self._load(index)

    def _load(self, index: Path) -> None:
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            cursor, entries = data["cursor"], data["entries"]
            if not (isinstance(entries, list) and isinstance(cursor, int) and not isinstance(cursor, bool)
                    and all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries)
                    and (0 if entries else -1) <= cursor < len(entries)):
                raise ValueError("unexpected structure")
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            broken = index.with_name(f"index.broken-{_now().strftime(_STAMP_FORMAT)}.json")
            try:
                os.replace(index, broken)
            except OSError:
                broken = index
            self.notice = (f"undo history was unreadable ({exc}); started a fresh history, "
                           f"old index kept as {broken.name}")
            return
        self._cursor, self._entries = cursor, entries

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._entries) - 1

    def entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._entries]

    def current(self) -> str | None:
        return self._entries[self._cursor]["id"] if self._entries else None

    def record(self, reason: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        now = _now()
        if self._entries:
            last = datetime.strptime(self._entries[-1]["id"][:_STAMP_LEN], _STAMP_FORMAT)
            last = last.replace(tzinfo=timezone.utc)
            now = max(now, last + timedelta(microseconds=1))
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", reason).strip("-") or "entry"
        while (self.directory / f"{now.strftime(_STAMP_FORMAT)}-{slug}").exists():
            now += timedelta(microseconds=1)
        snap_id = f"{now.strftime(_STAMP_FORMAT)}-{slug}"
        snap = self.directory / snap_id
        snap.mkdir()
        for i, path in enumerate(self.tracked):
            if path.exists():
                shutil.copyfile(path, snap / str(i))
        removed = self._entries[self._cursor + 1:]
        self._entries = self._entries[: self._cursor + 1]
        self._entries.append(
            {"id": snap_id, "reason": reason, "time": now.isoformat(timespec="microseconds")}
        )
        while len(self._entries) > self.cap:
            removed.append(self._entries.pop(0))
        self._cursor = len(self._entries) - 1
        self._save()
        for old in removed:
            shutil.rmtree(self.directory / old["id"], ignore_errors=True)
        return snap_id

    def undo(self) -> str | None:
        if not self.can_undo:
            return None
        return self._move(self._cursor - 1)

    def redo(self) -> str | None:
        if not self.can_redo:
            return None
        return self._move(self._cursor + 1)

    def _move(self, cursor: int) -> str:
        snap_id = self._entries[cursor]["id"]
        snap = self.directory / snap_id
        if not snap.is_dir():
            raise FileNotFoundError(f"history snapshot is missing: {snap}")
        for i, path in enumerate(self.tracked):
            saved = snap / str(i)
            if saved.exists():
                atomic_write_bytes(path, saved.read_bytes())
            else:
                path.unlink(missing_ok=True)
        self._cursor = cursor
        self._save()
        return snap_id

    def _save(self) -> None:
        text = json.dumps({"cursor": self._cursor, "entries": self._entries}, indent=2)
        atomic_write_text(self.directory / "index.json", text + "\n")

