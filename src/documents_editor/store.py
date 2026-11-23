"""Atomic JSON source file store with optimistic hash check."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class StaleSource(RuntimeError):
    """The source file changed since it was read."""


class SourceUnreadable(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def dumps(doc: Any, trailing_newline: bool) -> str:
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    return text + "\n" if trailing_newline else text


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` via a temp file in the same directory.

    An existing file keeps its permission mode.
    """
    path = Path(path)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None
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
    try:
        if mode is not None:
            os.chmod(tmp_name, mode)
        attempts = 5 if sys.platform == "win32" else 1
        for attempt in range(attempts):
            try:
                os.replace(tmp_name, path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.1)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SourceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read_bytes(self) -> bytes:
        try:
            return self.path.read_bytes()
        except OSError as e:
            raise SourceUnreadable(f"cannot read {self.path}: {e.strerror or e}") from e

    def _parse(self, data: bytes) -> tuple[Any, str]:
        try:
            text = data.decode("utf-8")
            return json.loads(text), text
        except (UnicodeDecodeError, ValueError) as e:
            raise SourceUnreadable(f"invalid JSON in {self.path}: {e}") from e

    def read(self) -> tuple[Any, str]:
        data = self._read_bytes()
        doc, _ = self._parse(data)
        return doc, _sha256(data)

    def current_hash(self) -> str | None:
        try:
            return _sha256(self.path.read_bytes())
        except FileNotFoundError:
            return None

    def write(self, doc: Any, expected_hash: str) -> str:
        data = self._read_bytes()
        if _sha256(data) != expected_hash:
            raise StaleSource(f"{self.path} changed on disk")
        text = dumps(doc, data.endswith(b"\n"))
        atomic_write_text(self.path, text)
        return _sha256(text.encode("utf-8"))

    def is_canonical(self) -> bool:
        doc, text = self._parse(self._read_bytes())
        return dumps(doc, text.endswith("\n")) == text
