"""Adapter contract: how a document type plugs into the editor."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Issue:
    message: str
    pointer: str | None = None
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class Command:
    args: list[str]
    cwd: Path


@dataclass(frozen=True)
class BuildResult:
    pages: list[Path]
    warnings: list[Issue]


class Adapter(Protocol):
    name: str
    default_agent_command: list[str]

    def source_path(self, project_dir: Path) -> Path: ...
    def render_edit_html(self, source: Any, project_dir: Path) -> str: ...
    def validate(self, source: Any) -> list[Issue]: ...
    def check_text(self, pointer: str, value: str) -> list[Issue]: ...
    def figure_change(self, pointer: str, old: str, new: str) -> str | None: ...
    def panel_fields(self, source: Any, pointer: str) -> list[dict]: ...
    def build_command(self, project_dir: Path) -> Command: ...
    def build_result(self, project_dir: Path) -> BuildResult: ...
    def agent_cwd(self, project_dir: Path) -> Path: ...
    def agent_context(self, project_dir: Path) -> dict[str, str]: ...


def load_adapter(spec: str) -> Adapter:
    """Instantiate the adapter named by ``module:Class``."""
    module_name, sep, class_name = spec.partition(":")
    if not sep or not module_name or not class_name:
        raise ValueError(f"adapter spec must be 'module:Class', got {spec!r}")
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot load adapter {spec!r}: {exc}") from exc
    return cls()
