"""One-at-a-time subprocess runner with streamed output, cancel and timeout."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

from .adapter import Command

KILL_GRACE_S = 5.0


class Busy(RuntimeError):
    """A job is already running."""


class CommandFailed(RuntimeError):
    """The command could not be started (e.g. executable not found)."""


@dataclass
class Job:
    kind: str
    command: Command
    returncode: int | None = None
    cancelled: bool = False
    timed_out: bool = False
    lines: list[str] = field(default_factory=list)


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def current(self) -> Job | None:
        return self._current

    @property
    def busy(self) -> bool:
        return self._current is not None

    def start(
        self,
        kind: str,
        command: Command,
        *,
        timeout_s: float | None,
        on_line: Callable[[str], None],
        on_exit: Callable[[Job], None],
    ) -> Job:
        with self._lock:
            if self._current is not None:
                raise Busy("a job is already running")
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(
                    command.args,
                    cwd=command.cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    **kwargs,
                )
            except OSError as exc:
                raise CommandFailed(f"cannot run {command.args[0]!r}: {exc}") from exc
            job = Job(kind, command)
            self._current = job
            self._proc = proc
        threading.Thread(
            target=self._pump, args=(job, proc, timeout_s, on_line, on_exit), daemon=True
        ).start()
        return job

    def cancel(self) -> None:
        with self._lock:
            job, proc = self._current, self._proc
            if job is None or proc is None:
                return
            job.cancelled = True
        _kill_tree(proc)

    def _pump(self, job, proc, timeout_s, on_line, on_exit) -> None:
        timer = None
        if timeout_s is not None:
            def expire() -> None:
                job.timed_out = True
                _kill_tree(proc)
            timer = threading.Timer(timeout_s, expire)
            timer.daemon = True
            timer.start()
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                job.lines.append(line)
                try:
                    on_line(line)
                except Exception:
                    pass
            proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            job.returncode = proc.returncode
            with self._lock:
                self._current = None
                self._proc = None
        on_exit(job)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return
    _signal_group(proc.pid, signal.SIGTERM)

    def escalate() -> None:
        try:
            proc.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(proc.pid, signal.SIGKILL)

    threading.Thread(target=escalate, daemon=True).start()


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
