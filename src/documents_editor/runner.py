"""One-at-a-time subprocess runner with streamed output, cancel and timeout."""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .adapter import Command

KILL_GRACE_S = 5.0
log = logging.getLogger(__name__)


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
            args = _resolve(command.args)
            try:
                proc = subprocess.Popen(
                    args,
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
        self._stop(cancelled=True)

    def _stop(self, *, cancelled: bool, only: Job | None = None) -> None:
        """Kill the current job's tree unless it already finished."""
        with self._lock:
            job, proc = self._current, self._proc
            if job is None or proc is None or (only is not None and job is not only):
                return
            if proc.poll() is not None:
                return
            if cancelled:
                job.cancelled = True
            else:
                job.timed_out = True
            _terminate(proc)
        if os.name != "nt":
            threading.Thread(target=self._escalate, args=(job, proc), daemon=True).start()

    def _escalate(self, job: Job, proc: subprocess.Popen) -> None:
        """SIGKILL the process group if it outlives the grace period."""
        pgid = proc.pid
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            if not _group_exists(pgid):
                return
            time.sleep(0.1)
        with self._lock:
            # Only while this job is still running, so the pgid cannot be recycled.
            if self._current is job and _group_exists(pgid):
                _signal_group(pgid, signal.SIGKILL)

    def _pump(self, job, proc, timeout_s, on_line, on_exit) -> None:
        timer = None
        if timeout_s is not None:
            timer = threading.Timer(timeout_s, self._stop, kwargs={"cancelled": False, "only": job})
            timer.daemon = True
            timer.start()
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                job.lines.append(line)
                try:
                    on_line(line)
                except Exception:
                    log.exception("on_line callback failed")
            proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            proc.stdout.close()
            with self._lock:
                job.returncode = proc.returncode
                self._current = None
                self._proc = None
        on_exit(job)


def _resolve(args: list[str]) -> list[str]:
    """Find a bare program name on PATH (including Windows .cmd/.bat shims)."""
    if not args:
        raise CommandFailed("empty command")
    name = args[0]
    if os.path.dirname(name):  # a path: Popen resolves it against the job's cwd
        return list(args)
    found = shutil.which(name)
    if found is None:
        raise CommandFailed(f"cannot run {name!r}: not found on PATH")
    return [found, *args[1:]]


def _terminate(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        _signal_group(proc.pid, signal.SIGTERM)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
