"""edit_requests.json: comments for the agent, also written by external agents."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .figures import numeric_tokens
from .store import atomic_write_text

_CATEGORIES = ("format", "text")
_SCOPES = ("document", "house_style")
_UPDATABLE = ("instruction", "scope", "category", "status")
_STATUSES = ("open", "done", "needs_input", "declined")


class RequestsFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- reading ---------------------------------------------------------
    def _read(self) -> tuple[dict[str, Any], str | None, bool]:
        """Return (document, warning, trailing_newline). Document is empty on any problem."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, None, True
        except (OSError, UnicodeDecodeError) as exc:
            return {}, f"{self.path.name} is unreadable: {exc}", True
        if not raw.strip():
            return {}, None, True
        newline = raw.endswith("\n")
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            return {}, f"{self.path.name} is not valid JSON: {exc}", newline
        if not isinstance(doc, dict):
            return {}, f"{self.path.name} must contain a JSON object", newline
        requests = doc.get("requests", [])
        if not isinstance(requests, list) or not all(isinstance(r, dict) for r in requests):
            return {}, f"{self.path.name}: 'requests' must be a list of objects", newline
        return doc, None, newline

    def load(self) -> tuple[list[dict], str | None]:
        doc, warning, _ = self._read()
        return list(doc.get("requests", [])), warning

    def open_count(self) -> int:
        return sum(1 for r in self.load()[0] if r.get("status") == "open")

    # -- writing ---------------------------------------------------------
    def _fresh(self) -> tuple[dict[str, Any], bool]:
        doc, warning, newline = self._read()
        if warning:
            raise ValueError(f"refusing to overwrite broken requests file: {warning}")
        if "requests" not in doc:
            doc["requests"] = []
        doc.setdefault("version", 1)
        return doc, newline

    def _save(self, doc: dict[str, Any], newline: bool) -> None:
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        atomic_write_text(self.path, text + "\n" if newline else text)

    @staticmethod
    def _new_id(requests: list[dict], now: datetime) -> str:
        prefix = f"r-{now:%Y%m%d}-"
        used = {r.get("id") for r in requests}
        n = 1 + max(
            (int(i[len(prefix):]) for i in used
             if isinstance(i, str) and i.startswith(prefix) and i[len(prefix):].isdigit()),
            default=0,
        )
        return f"{prefix}{n:04d}"

    @staticmethod
    def _now() -> datetime:
        return datetime.now().replace(microsecond=0)

    def add(
        self, *, pointer: str | None, element: str, quote: str,
        category: Literal["format", "text"], scope: Literal["document", "house_style"],
        instruction: str,
    ) -> dict:
        if category not in _CATEGORIES:
            raise ValueError(f"invalid category: {category!r}")
        if scope not in _SCOPES:
            raise ValueError(f"invalid scope: {scope!r}")
        doc, newline = self._fresh()
        now = self._now()
        request = {
            "id": self._new_id(doc["requests"], now), "status": "open",
            "created": now.isoformat(), "pointer": pointer, "element": element,
            "quote": quote, "category": category, "scope": scope,
            "instruction": instruction, "agent_note": "",
        }
        doc["requests"].append(request)
        self._save(doc, newline)
        return request

    def update(self, request_id: str, **fields: Any) -> dict:
        bad = set(fields) - set(_UPDATABLE)
        if bad:
            raise ValueError(f"fields not updatable: {sorted(bad)}")
        if "category" in fields and fields["category"] not in _CATEGORIES:
            raise ValueError(f"invalid category: {fields['category']!r}")
        if "scope" in fields and fields["scope"] not in _SCOPES:
            raise ValueError(f"invalid scope: {fields['scope']!r}")
        if "status" in fields and fields["status"] not in _STATUSES:
            raise ValueError(f"invalid status: {fields['status']!r}")
        doc, newline = self._fresh()
        for request in doc["requests"]:
            if request.get("id") == request_id:
                if "category" in fields and request.get("category") == "figure":
                    raise ValueError("category of a figure request cannot be changed")
                request.update(fields)
                self._save(doc, newline)
                return request
        raise KeyError(request_id)

    def delete(self, request_id: str) -> None:
        doc, newline = self._fresh()
        kept = [r for r in doc["requests"] if r.get("id") != request_id]
        if len(kept) == len(doc["requests"]):
            raise KeyError(request_id)
        doc["requests"] = kept
        self._save(doc, newline)

    def upsert_figure(self, *, pointer: str, old: str, new: str, quote: str, note: str) -> dict | None:
        doc, newline = self._fresh()
        requests = doc["requests"]
        existing = next(
            (r for r in requests if r.get("category") == "figure" and r.get("origin") == "editor"
             and r.get("status") == "open" and r.get("pointer") == pointer),
            None,
        )
        first_old = existing["old"] if existing and "old" in existing else old
        if numeric_tokens(new) == numeric_tokens(first_old):
            if existing is not None:
                requests.remove(existing)
                self._save(doc, newline)
            return None
        if existing is None:
            now = self._now()
            existing = {
                "id": self._new_id(requests, now), "created": now.isoformat(),
                "pointer": pointer, "element": "text", "quote": quote,
                "category": "figure", "origin": "editor", "scope": "document",
                "instruction": note, "old": first_old, "new": new,
                "status": "open", "agent_note": "",
            }
            requests.append(existing)
        existing.update({"new": new, "quote": quote, "instruction": note})
        self._save(doc, newline)
        return existing
