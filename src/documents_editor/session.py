"""EditorSession: all editor logic (saving, history, comments, agent and build runs), no HTTP."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from . import config
from .adapter import Adapter, Command, Issue
from .edits import EditLog, Lost, survival_check
from .events import EventBus
from .history import History
from .pointer import PointerError, get, iter_strings, set_value
from .requests import RequestsFile
from .runner import Busy, CommandFailed, Job, JobRunner
from .store import SourceStore, SourceUnreadable, StaleSource, dumps

__all__ = ["EditorSession", "Locked", "Refused", "StaleSource"]

log = logging.getLogger(__name__)

LOCK_NOTICE = "another editor is open on this project"
FIGURE_NOTICE = "edit_requests.json is unreadable; figure change not recorded"
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", re.ASCII)


class Locked(RuntimeError):
    """A job is running; the document is read-only."""


class Refused(RuntimeError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


def _errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def _coerce(old: Any, new: str) -> Any:
    """Keep a number in a numeric field when the new text parses as one."""
    if isinstance(old, bool) or not isinstance(old, (int, float)):
        return new
    text = new.strip()
    if not _NUMBER.fullmatch(text):
        return new
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    value = float(text)
    return value if math.isfinite(value) else new


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class EditorSession:
    def __init__(
        self,
        adapter: Adapter,
        project_dir: Path,
        *,
        bus: EventBus,
        runner: JobRunner,
        config_file: Path | None = None,
        confirm_file: Path | None = None,
    ) -> None:
        self.adapter = adapter
        self.project_dir = Path(project_dir)
        self.bus = bus
        self.runner = runner
        self._config_file = config_file
        self._confirm_file = confirm_file
        self._lock = threading.RLock()

        self.source_path = Path(adapter.source_path(self.project_dir))
        self.requests_path = self.project_dir / "edit_requests.json"
        editor_dir = self.project_dir / ".editor"
        editor_dir.mkdir(parents=True, exist_ok=True)
        self.store = SourceStore(self.source_path)
        self.requests_file = RequestsFile(self.requests_path)
        self.edit_log = EditLog(editor_dir / "edits.json")
        self.history = History(
            editor_dir / "history", [self.source_path, self.requests_path, self.edit_log.path]
        )

        self._lock_path = editor_dir / "lock"
        self._owns_lock = False
        self._notices: list[str] = []
        self._job: str | None = None
        self._agent_before: Any = None
        self._targeted: list[tuple[str, str]] = []  # (pointer, category)
        self._hash: str | None = None
        self._valid = False
        self._issues: list[Issue] = []
        self._html = ""
        self._canonical_notice: str | None = None

        self._take_lock_file()
        self._reload()
        if self._differs_from_snapshot():
            self._record("open" if self.history.current() is None else "external")

    # -- lifecycle ---------------------------------------------------------
    def _take_lock_file(self) -> None:
        try:
            pid = int(self._lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if _pid_alive(pid):
            self._notices.append(LOCK_NOTICE)
            return
        self._lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self._owns_lock = True

    def close(self) -> None:
        with self._lock:
            if self._owns_lock:
                self._lock_path.unlink(missing_ok=True)
                self._owns_lock = False

    # -- internal state ----------------------------------------------------
    def _reload(self) -> None:
        """Re-read the source; keep the last good HTML while it is unreadable or invalid."""
        self._canonical_notice = None
        try:
            data = self.source_path.read_bytes()
        except OSError as exc:
            self._hash = None
            message = f"cannot read {self.source_path}: {exc.strerror or exc}"
            self._valid, self._issues = False, [Issue(message)]
            return
        self._hash = hashlib.sha256(data).hexdigest()
        try:
            text = data.decode("utf-8")
            doc = json.loads(text)
        except (UnicodeDecodeError, ValueError) as exc:
            self._valid, self._issues = False, [Issue(f"invalid JSON in {self.source_path}: {exc}")]
            return
        self._issues = self.adapter.validate(doc)
        self._valid = not _errors(self._issues)
        if self._valid:
            self._html = self.adapter.render_edit_html(doc, self.project_dir)
        if dumps(doc, text.endswith("\n")) != text:
            self._canonical_notice = (
                f"{self.source_path.name} is not in the editor's JSON style; "
                "the first save will reformat it"
            )

    def _current_hash(self) -> str | None:
        data = _read_or_none(self.source_path)
        return None if data is None else hashlib.sha256(data).hexdigest()

    def _differs_from_snapshot(self) -> bool:
        """Whether the source or requests file differs from the current snapshot.

        The edit log is snapshotted with them but ignored here, so accept() never
        causes a spurious history entry.
        """
        current = self.history.current()
        if current is None:
            return True
        snap = self.history.directory / current
        return any(
            _read_or_none(path) != _read_or_none(snap / str(i))
            for i, path in enumerate(self.history.tracked[:2])
        )

    def _record(self, reason: str) -> None:
        """Record history for a change the session did not write; an unreadable file is skipped."""
        try:
            self.history.record(reason)
        except OSError as exc:
            log.warning("cannot record history %r: %s", reason, exc)

    def _busy(self) -> bool:
        return self._job is not None or self.runner.busy

    def _check_unlocked(self) -> None:
        if self._busy():
            raise Locked("a job is running")

    def _publish_state(self) -> None:
        self.bus.publish("state", self.state())

    def _add_notice(self, notice: str) -> None:
        if notice not in self._notices:
            self._notices.append(notice)

    # -- reading -----------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            requests, warning = self.requests_file.load()
            notices = list(self._notices)
            if self._canonical_notice:
                notices.append(self._canonical_notice)
            if warning:
                notices.append(warning)
            return {
                "hash": self._hash,
                "valid": self._valid,
                "locked": self._busy(),
                "job": self._job,
                "can_undo": self.history.can_undo,
                "can_redo": self.history.can_redo,
                "open_requests": sum(1 for r in requests if r.get("status") == "open"),
                "issues": [asdict(i) for i in self._issues],
                "notices": notices,
            }

    def page_html(self) -> str:
        with self._lock:
            return self._html

    def value(self, pointer: str) -> dict:
        with self._lock:
            doc, h = self.store.read()
            return {"value": get(doc, pointer), "hash": h}

    def fields(self, pointer: str) -> list[dict]:
        with self._lock:
            doc, _ = self.store.read()
            return self.adapter.panel_fields(doc, pointer)

    def page_path(self, name: str) -> Path | None:
        """A page of the latest build result, by file name, if it lies inside the project."""
        with self._lock:
            root = self.project_dir.resolve()
            for page in self.adapter.build_result(self.project_dir).pages:
                page = Path(page)
                if page.name != name:
                    continue
                resolved = page.resolve()
                if resolved.is_relative_to(root) and resolved.is_file():
                    return resolved
            return None

    def check(self, pointer: str, value: str) -> list[Issue]:
        return self.adapter.check_text(pointer, value)

    # -- saving ------------------------------------------------------------
    def _read_for_edit(self, source_hash: str | None) -> tuple[Any, str]:
        """Read the source for an edit; refuse when it is stale or currently invalid."""
        try:
            doc, h = self.store.read()
        except SourceUnreadable as exc:
            raise Refused([Issue(exc.message)]) from exc
        if source_hash is not None and h != source_hash:
            raise StaleSource(f"{self.source_path} changed on disk")
        if h != self._hash:
            self._poll_locked()
        if not self._valid:
            raise Refused(_errors(self._issues))
        return doc, h

    def _validate_edit(self, doc: Any, pointer: str) -> list[Issue]:
        issues = self.adapter.validate(doc)
        errors = [i if i.pointer else replace(i, pointer=pointer) for i in _errors(issues)]
        if errors:
            raise Refused(errors)
        return issues

    def _record_figure(self, pointer: str, old: Any, new: Any) -> dict | None:
        requests, warning = self.requests_file.load()
        if warning:
            self._add_notice(FIGURE_NOTICE)
            return None
        existing = next(
            (r for r in requests if r.get("category") == "figure" and r.get("origin") == "editor"
             and r.get("status") == "open" and r.get("pointer") == pointer),
            None,
        )
        base = str(existing["old"]) if existing and "old" in existing else str(old)
        note = self.adapter.figure_change(pointer, base, str(new))
        if note is None and existing is None:
            return None
        try:
            return self.requests_file.upsert_figure(
                pointer=pointer, old=str(old), new=str(new), quote=str(new),
                note=note or existing.get("instruction", ""),
            )
        except ValueError:
            self._add_notice(FIGURE_NOTICE)
            return None

    def patch(self, pointer: str, old: Any, new: str, source_hash: str) -> dict:
        with self._lock:
            self._check_unlocked()
            doc, h = self._read_for_edit(source_hash)
            try:
                current = get(doc, pointer)
            except PointerError as exc:
                raise StaleSource(f"{pointer} no longer exists") from exc
            if current != old:
                raise StaleSource(f"the value at {pointer} changed")
            value = _coerce(old, new)
            if value == old:
                return {"hash": h, "issues": [], "figure_request": None}
            set_value(doc, pointer, value)
            issues = self._validate_edit(doc, pointer)
            self.store.write(doc, h)
            figure = self._record_figure(pointer, old, value)
            self.edit_log.record(pointer, value)
            self.history.record("edit")
            self._reload()
            self._publish_state()
            return {"hash": self._hash, "issues": [asdict(i) for i in issues],
                    "figure_request": figure}

    # -- history -----------------------------------------------------------
    def _restore(self, step) -> None:
        with self._lock:
            self._check_unlocked()
            try:
                step()
            except FileNotFoundError as exc:
                raise Refused([Issue(f"cannot restore this step: {exc}")]) from exc
            self._reload()
            self._publish_state()

    def undo(self) -> None:
        self._restore(self.history.undo)

    def redo(self) -> None:
        self._restore(self.history.redo)

    # -- comments ----------------------------------------------------------
    def requests(self) -> list[dict]:
        with self._lock:
            return self.requests_file.load()[0]

    def _change_requests(self, action, *args, **kw):
        with self._lock:
            self._check_unlocked()
            result = action(*args, **kw)
            self.history.record("comment")
            self._publish_state()
            return result

    def add_request(self, **kw: Any) -> dict:
        return self._change_requests(self.requests_file.add, **kw)

    def update_request(self, id: str, **kw: Any) -> dict:
        return self._change_requests(self.requests_file.update, id, **kw)

    def delete_request(self, id: str) -> None:
        self._change_requests(self.requests_file.delete, id)

    # -- agent -------------------------------------------------------------
    def _agent_command(self) -> tuple[list[str], float]:
        settings = config.agent_settings(
            self.adapter.name, self.adapter.default_agent_command, self._config_file
        )
        values = {
            "project_dir": str(self.project_dir),
            "source": str(self.source_path),
            "requests": str(self.requests_path),
            **self.adapter.agent_context(self.project_dir),
        }
        return config.substitute(settings.command, values), settings.timeout_minutes

    def agent_preview(self) -> dict:
        try:
            args, _ = self._agent_command()
        except config.ConfigError as exc:
            raise Refused([Issue(str(exc))]) from exc
        return {"args": args, "confirmed": config.is_confirmed(args, self._confirm_file)}

    def _start_job(self, kind: str, command: Command, timeout_s: float | None, on_exit) -> None:
        try:
            self.runner.start(
                kind, command, timeout_s=timeout_s,
                on_line=lambda line: self.bus.publish("log", {"job": kind, "line": line}),
                on_exit=on_exit,
            )
        except Busy as exc:
            raise Locked(str(exc)) from exc
        except CommandFailed as exc:
            raise Refused([Issue(str(exc))]) from exc
        self._job = kind
        self._publish_state()

    def start_agent(self, confirm: bool = False) -> None:
        with self._lock:
            self._check_unlocked()
            try:
                args, timeout_minutes = self._agent_command()
            except config.ConfigError as exc:
                raise Refused([Issue(str(exc))]) from exc
            if not config.is_confirmed(args, self._confirm_file):
                if not confirm:
                    raise Refused([Issue("confirm the agent command before the first run")])
                config.confirm(args, self._confirm_file)
            self._poll_locked()
            if self._differs_from_snapshot():
                self._record("external")
            try:
                self._agent_before = self.store.read()[0]
            except SourceUnreadable:
                self._agent_before = None
            requests = self.requests_file.load()[0]
            self._targeted = [
                (r["pointer"], r["category"]) for r in requests
                if r.get("status") == "open" and r.get("category") in ("text", "format")
                and isinstance(r.get("pointer"), str)
            ]
            command = Command(args, Path(self.adapter.agent_cwd(self.project_dir)))
            self._start_job("agent", command, timeout_minutes * 60, self._finish_agent)

    def _targeted_entries(self, entries: dict[str, Any]) -> set[str]:
        """Edited pointers an open request asked to change.

        A text request covers its pointer and everything under it; a format request
        covers its exact pointer only.
        """
        return {
            p for p in entries
            if any(p == t or (category == "text" and p.startswith(t + "/"))
                   for t, category in self._targeted)
        }

    def _apply_moves(self, entries: dict[str, Any], moves: dict[str, str], after: Any) -> list[Lost]:
        taken = set(entries) - set(moves)
        lost = []
        for old in moves:
            self.edit_log.forget(old)
        for old, new in moves.items():
            if new in taken:
                try:
                    now = get(after, old)
                except PointerError:
                    now = None
                lost.append(Lost(old, entries[old], now))
                continue
            taken.add(new)
            self.edit_log.record(new, entries[old])
        # a collision loses the move; keep the user's value under its old pointer for reapply
        for item in lost:
            self.edit_log.record(item.pointer, item.mine)
        return lost

    def _finish_agent(self, job: Job) -> None:
        with self._lock:
            try:
                report = self._agent_report(job)
            except Exception as exc:  # never leave the session locked
                log.exception("finishing the agent run failed")
                report = {"ok": False, "returncode": job.returncode,
                          "issues": [asdict(Issue(f"{type(exc).__name__}: {exc}"))]}
            finally:
                self._job = None
            self.bus.publish("agent-done", report)
            self._publish_state()

    def _agent_report(self, job: Job) -> dict:
        self._reload()
        if not self._valid:
            if self._differs_from_snapshot():
                self._record("agent")
            return {"ok": False, "returncode": job.returncode, "cancelled": job.cancelled,
                    "timed_out": job.timed_out, "issues": [asdict(i) for i in self._issues]}
        after = self.store.read()[0]
        entries = self.edit_log.entries()
        targeted = self._targeted_entries(entries)
        lost, moves = survival_check(entries, after, targeted)
        lost += self._apply_moves(entries, moves, after)
        for pointer in targeted:  # the requested change supersedes the direct edit
            try:
                if get(after, pointer) == entries[pointer]:
                    continue
            except PointerError:
                pass
            self.edit_log.forget(pointer)
        if self._differs_from_snapshot():
            self._record("agent")
        before = dict(iter_strings(self._agent_before)) if self._agent_before is not None else {}
        changed = [p for p, v in iter_strings(after) if before.get(p) != v]
        return {
            "ok": job.returncode == 0 and not job.cancelled and not job.timed_out,
            "returncode": job.returncode,
            "cancelled": job.cancelled,
            "timed_out": job.timed_out,
            "changed": changed,
            "lost": [asdict(item) for item in lost],
            "issues": [asdict(i) for i in self._issues],
        }

    def reapply(self, pointers: list[str]) -> dict:
        with self._lock:
            self._check_unlocked()
            if not pointers:
                return {"hash": self._hash, "issues": []}
            doc, h = self._read_for_edit(None)
            entries = self.edit_log.entries()
            for pointer in pointers:
                if pointer not in entries:
                    raise Refused([Issue("no edit of yours is recorded here", pointer)])
                try:
                    set_value(doc, pointer, entries[pointer])
                except PointerError as exc:
                    raise Refused([Issue(f"this field no longer exists: {exc}", pointer)]) from exc
            issues = self._validate_edit(doc, pointers[0])
            self.store.write(doc, h)
            self.history.record("edit")
            self._reload()
            self._publish_state()
            return {"hash": self._hash, "issues": [asdict(i) for i in issues]}

    def accept(self, pointers: list[str]) -> None:
        with self._lock:
            self._check_unlocked()
            for pointer in pointers:
                self.edit_log.forget(pointer)
            self._publish_state()

    # -- build -------------------------------------------------------------
    def start_build(self) -> None:
        with self._lock:
            self._check_unlocked()
            command = self.adapter.build_command(self.project_dir)
            self._start_job("build", command, None, self._finish_build)

    def _finish_build(self, job: Job) -> None:
        with self._lock:
            try:
                result = self.adapter.build_result(self.project_dir)
                report = {
                    "ok": job.returncode == 0 and not job.cancelled and not job.timed_out,
                    "pages": [p.name for p in result.pages],
                    "warnings": [asdict(w) for w in result.warnings],
                }
                if self._differs_from_snapshot():
                    self._record("build")
                self._reload()
            except Exception as exc:
                log.exception("finishing the build failed")
                report = {"ok": False, "pages": [],
                          "warnings": [asdict(Issue(f"{type(exc).__name__}: {exc}"))]}
            finally:
                self._job = None
            self.bus.publish("build-done", report)
            self._publish_state()

    def cancel_job(self) -> None:
        self.runner.cancel()

    # -- outside changes ---------------------------------------------------
    def _poll_locked(self) -> bool:
        if self._current_hash() == self._hash:
            return False
        self._record("external")
        self._reload()
        self.bus.publish("source-changed", {"hash": self._hash, "valid": self._valid})
        self._publish_state()
        return True

    def poll_external(self) -> bool:
        with self._lock:
            if self._busy():
                return False
            return self._poll_locked()
