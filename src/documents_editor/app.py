"""Wire the session, job runner and HTTP server together and serve until Ctrl+C."""
from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path

from . import config
from .adapter import Adapter
from .events import EventBus
from .runner import JobRunner
from .server import make_server
from .session import EditorSession

log = logging.getLogger(__name__)


def _poll_loop(session: EditorSession, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        try:
            session.poll_external()
        except Exception:
            log.exception("polling the source failed")


def run(adapter: Adapter, project_dir: Path, *, port: int = 0, open_browser: bool = True) -> None:
    project_dir = Path(project_dir).resolve()
    runner = JobRunner()
    session = EditorSession(adapter, project_dir, bus=EventBus(), runner=runner)
    server, url = make_server(session, port=port)
    print(f"Editor: {url}", flush=True)
    print(f"Agent config: {config.config_path()}", flush=True)
    stop = threading.Event()
    threading.Thread(target=_poll_loop, args=(session, stop), daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        runner.cancel()
        server.server_close()
        session.close()
