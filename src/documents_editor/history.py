"""Snapshot history with undo and redo over a set of tracked files."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import atomic_write_text


class History:
    def __init__(self, directory: Path, tracked: list[Path], cap: int = 200) -> None:
        self.directory = Path(directory)
        self.tracked = [Path(p) for p in tracked]
        self.cap = cap
        self._cursor = -1
        self._entries: list[dict[str, Any]] = []
        index = self.directory / "index.json"
        if index.exists():
            data = json.loads(index.read_text(encoding="utf-8"))
            self._cursor = data["cursor"]
            self._entries = data["entries"]

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
        now = datetime.now()
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", reason).strip("-") or "entry"
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        last = self._entries[-1]["id"] if self._entries else ""
        counter = 0
        while True:
            suffix = f"-{counter:04d}" if counter else ""
            snap_id = f"{stamp}{suffix}-{slug}"
            if not (self.directory / snap_id).exists() and snap_id > last:
                break
            counter += 1
        snap = self.directory / snap_id
        snap.mkdir()
        for i, path in enumerate(self.tracked):
            if path.exists():
                shutil.copyfile(path, snap / str(i))
        for dropped in self._entries[self._cursor + 1:]:
            shutil.rmtree(self.directory / dropped["id"], ignore_errors=True)
        self._entries = self._entries[: self._cursor + 1]
        self._entries.append(
            {"id": snap_id, "reason": reason, "time": now.isoformat(timespec="microseconds")}
        )
        while len(self._entries) > self.cap:
            old = self._entries.pop(0)
            shutil.rmtree(self.directory / old["id"], ignore_errors=True)
        self._cursor = len(self._entries) - 1
        self._save()
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
        for i, path in enumerate(self.tracked):
            saved = snap / str(i)
            if saved.exists():
                _atomic_write_bytes(path, saved.read_bytes())
            else:
                path.unlink(missing_ok=True)
        self._cursor = cursor
        self._save()
        return snap_id

    def _save(self) -> None:
        text = json.dumps({"cursor": self._cursor, "entries": self._entries}, indent=2)
        atomic_write_text(self.directory / "index.json", text + "\n")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp_name = tmp.name
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            os.unlink(tmp_name)
            raise
    os.replace(tmp_name, path)
