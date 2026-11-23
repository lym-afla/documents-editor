"""Wire the session, job runner and HTTP server together and serve until Ctrl+C."""
from __future__ import annotations

import logging
import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .adapter import Adapter
from .events import EventBus
from .runner import JobRunner
from .server import make_server
from .session import EditorSession, Refused

log = logging.getLogger(__name__)


def _poll_loop(session: EditorSession, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        try:
            session.poll_external()
        except Exception:
            log.exception("polling the source failed")


def startup_lines(session: EditorSession, url: str) -> list[str]:
    """What the terminal shows at startup, including the agent command that would run."""
    try:
        args = session.agent_preview()["args"]
        command = subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)
    except Refused as exc:
        command = f"(not runnable: {exc})"
    return [f"Editor: {url}", f"Agent config: {session.config_file}", f"Agent command: {command}"]


def run(adapter: Adapter, project_dir: Path, *, port: int = 0, open_browser: bool = True) -> None:
    project_dir = Path(project_dir).resolve()
    runner = JobRunner()
    session = EditorSession(adapter, project_dir, bus=EventBus(), runner=runner)
    stop = threading.Event()
    server = None
    try:
        server, url = make_server(session, port=port)
        for line in startup_lines(session, url):
            print(line, flush=True)
        threading.Thread(target=_poll_loop, args=(session, stop), daemon=True).start()
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        runner.cancel()
        if server is not None:
            server.server_close()
        session.close()
