"""``python -m documents_editor --adapter module:Class PROJECT_DIR``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapter import load_adapter
from .app import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="documents_editor", description="Local browser editor for JSON documents")
    parser.add_argument("--adapter", required=True, metavar="module:Class")
    parser.add_argument("project_dir", type=Path, metavar="PROJECT_DIR")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        adapter = load_adapter(args.adapter)
    except Exception as exc:  # a broken adapter module can raise anything on import
        print(f"documents_editor: {exc}", file=sys.stderr)
        return 2
    if not args.project_dir.is_dir():
        print(f"documents_editor: not a directory: {args.project_dir}", file=sys.stderr)
        return 2
    run(adapter, args.project_dir, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
