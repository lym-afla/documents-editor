import os
import sys
import threading
import time

import pytest

from documents_editor.adapter import Command
from documents_editor.runner import Busy, CommandFailed, JobRunner


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def run(cmd, tmp_path, **kw):
    done = threading.Event()
    lines = []
    jobs = []
    r = JobRunner()
    r.start("t", Command(cmd, tmp_path), timeout_s=kw.get("timeout"), on_line=lines.append,
            on_exit=lambda j: (jobs.append(j), done.set()))
    return r, done, lines, jobs


def test_streams_lines_in_cyrillic_path_with_spaces(tmp_path):  # Review Focus 5
    d = tmp_path / "Астро проект"
    d.mkdir()
    r, done, lines, jobs = run([sys.executable, "-c", "import os;print(os.getcwd());print('ё')"], d)
    assert done.wait(10) and jobs[0].returncode == 0
    assert lines == [str(d), "ё"]
    assert jobs[0].lines == lines


def test_stderr_merged(tmp_path):
    r, done, lines, jobs = run(
        [sys.executable, "-c", "import sys;sys.stderr.write('err\\n');print('out')"], tmp_path)
    assert done.wait(10)
    assert sorted(lines) == ["err", "out"]


def test_busy_and_cancel_kills_child_tree(tmp_path):
    script = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(p.pid,flush=True);time.sleep(60)"
    )
    r, done, lines, jobs = run([sys.executable, "-c", script], tmp_path)
    with pytest.raises(Busy):
        r.start("t2", Command(["x"], tmp_path), timeout_s=None, on_line=print, on_exit=print)
    wait_until(lambda: len(lines) == 1)
    grandchild = int(lines[0])
    r.cancel()
    assert done.wait(10) and jobs[0].cancelled and not r.busy
    if os.name == "posix":
        def dead():
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return True
            return False
        wait_until(dead, 5)


def test_timeout(tmp_path):
    r, done, lines, jobs = run([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, timeout=0.5)
    assert done.wait(10) and jobs[0].timed_out and not jobs[0].cancelled


def test_not_busy_inside_on_exit_and_can_restart(tmp_path):
    r = JobRunner()
    second = threading.Event()
    seen = []

    def first_exit(job):
        seen.append(r.busy)
        r.start("b", Command([sys.executable, "-c", "print(1)"], tmp_path), timeout_s=None,
                on_line=lambda s: None, on_exit=lambda j: second.set())

    r.start("a", Command([sys.executable, "-c", "print(1)"], tmp_path), timeout_s=None,
            on_line=lambda s: None, on_exit=first_exit)
    assert second.wait(10)
    assert seen == [False]


def test_missing_executable_raises_and_stays_idle(tmp_path):
    r = JobRunner()
    with pytest.raises(CommandFailed):
        r.start("t", Command(["definitely-not-a-real-binary-xyz"], tmp_path), timeout_s=None,
                on_line=print, on_exit=print)
    assert not r.busy and r.current is None


def test_cancel_when_idle_is_noop():
    JobRunner().cancel()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_ignoring_child_is_killed_after_grace(tmp_path, monkeypatch):
    monkeypatch.setattr("documents_editor.runner.KILL_GRACE_S", 0.6)
    script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);time.sleep(60)"
    r, done, lines, jobs = run([sys.executable, "-c", script], tmp_path)
    wait_until(lambda: lines == ["up"])
    t0 = time.monotonic()
    r.cancel()
    assert done.wait(10)
    assert time.monotonic() - t0 >= 0.5
    assert jobs[0].cancelled and jobs[0].returncode != 0


def test_cancel_after_process_exit_is_ignored(tmp_path):
    release = threading.Event()
    done = threading.Event()
    jobs = []
    r = JobRunner()
    r.start("t", Command([sys.executable, "-c", "print('x')"], tmp_path), timeout_s=None,
            on_line=lambda s: release.wait(10),
            on_exit=lambda j: (jobs.append(j), done.set()))
    wait_until(lambda: r.current is not None and r.current.lines == ["x"])
    time.sleep(0.5)  # child has exited; reader is still blocked in on_line
    r.cancel()
    release.set()
    assert done.wait(10)
    assert jobs[0].returncode == 0 and not jobs[0].cancelled


def test_cancel_inside_on_exit_is_ignored(tmp_path):
    done = threading.Event()
    jobs = []
    r = JobRunner()

    def exit_(j):
        r.cancel()
        jobs.append(j)
        done.set()

    r.start("t", Command([sys.executable, "-c", "pass"], tmp_path), timeout_s=None,
            on_line=print, on_exit=exit_)
    assert done.wait(10) and not jobs[0].cancelled
