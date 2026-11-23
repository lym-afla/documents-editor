"""User configuration: agent command, timeout and command confirmations."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_FILE = "documents-editor.toml"


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "documents-editor" / _FILE


@dataclass(frozen=True)
class AgentSettings:
    command: list[str]
    timeout_minutes: float = 30


def agent_settings(adapter_name: str, default: list[str], path: Path | None = None) -> AgentSettings:
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AgentSettings(list(default))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        raise ConfigError(f"{path}: 'agent' must be a table such as [agent.{adapter_name}]")
    section = agent.get(adapter_name)
    if section is None:
        return AgentSettings(list(default))
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [agent.{adapter_name}] must be a table")
    command = section.get("command", default)
    if not isinstance(command, list) or not all(isinstance(a, str) for a in command) or not command:
        raise ConfigError(f"[agent.{adapter_name}] command must be a non-empty list of strings")
    timeout = section.get("timeout_minutes", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"[agent.{adapter_name}] timeout_minutes must be a positive number")
    return AgentSettings(list(command), timeout)


def substitute(args: list[str], values: dict[str, str]) -> list[str]:
    out = []
    for arg in args:
        try:
            out.append(arg.format_map(values))
        except KeyError as exc:
            raise ConfigError(f"unknown placeholder {exc.args[0]!r} in {arg!r}") from exc
        except (ValueError, IndexError, AttributeError) as exc:
            raise ConfigError(f"bad placeholder syntax in {arg!r}: {exc}") from exc
    return out


def _confirmed_path() -> Path:
    return config_path().parent / "confirmed.json"


def _load_confirmed(path: Path) -> list[list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def is_confirmed(args: list[str], path: Path | None = None) -> bool:
    return list(args) in _load_confirmed(path or _confirmed_path())


def confirm(args: list[str], path: Path | None = None) -> None:
    path = path or _confirmed_path()
    known = _load_confirmed(path)
    if list(args) in known:
        return
    known.append(list(args))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(known, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
