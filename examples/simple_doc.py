"""Example adapter: a small document with paragraphs, a table and a chart."""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Any

from documents_editor import pointer as ptr
from documents_editor.adapter import BuildResult, Command, Issue
from documents_editor.figures import numeric_tokens

_INLINE = re.compile(r"\*\*(.+?)\*\*|\*([^*]+?)\*|\[\^([A-Za-z0-9_-]+)\]")
_MARKER = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
_KNOWN_NOTES = {"a", "b"}
_BUILD_SCRIPT = (
    "import pathlib, sys; "
    "pathlib.Path(sys.argv[1], 'build.log').write_text('built\\n', encoding='utf-8')"
)


def _inline_html(text: str) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        if m[1] is not None:
            out.append(f"<strong>{html.escape(m[1])}</strong>")
        elif m[2] is not None:
            out.append(f"<em>{html.escape(m[2])}</em>")
        else:
            out.append(f"<sup>{html.escape(m[3])}</sup>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


class SimpleDocAdapter:
    name = "simple"
    default_agent_command = [sys.executable, "-c", "print('configure an agent command')"]

    def source_path(self, project_dir: Path) -> Path:
        return project_dir / "doc.json"

    def render_edit_html(self, source: Any, project_dir: Path) -> str:
        parts = [f'<h1 data-src="/title" data-edit="plain">{html.escape(source["title"])}</h1>']
        for i, text in enumerate(source["paragraphs"]):
            parts.append(f'<p data-src="/paragraphs/{i}" data-edit="inline">{_inline_html(text)}</p>')
        rows = []
        for r, row in enumerate(source["table"]):
            cells = []
            for c, value in enumerate(row):
                kind = "plain" if isinstance(value, str) else "number-cell"
                cls = "" if isinstance(value, str) else ' class="number-cell"'
                cells.append(
                    f'<td{cls} data-src="/table/{r}/{c}" data-edit="{kind}">{html.escape(str(value))}</td>'
                )
            rows.append(f"<tr>{''.join(cells)}</tr>")
        parts.append(f"<table>{''.join(rows)}</table>")
        chart = source["chart"]
        parts.append(
            f'<figure data-src="/chart" data-edit="chart"><figcaption>{html.escape(chart["title"])}'
            f"</figcaption><ul>{''.join(f'<li>{html.escape(l)}</li>' for l in chart['labels'])}</ul></figure>"
        )
        return "\n".join(parts)

    def validate(self, source: Any) -> list[Issue]:
        issues: list[Issue] = []
        title = source.get("title") if isinstance(source, dict) else None
        if not isinstance(title, str) or not title.strip():
            issues.append(Issue("title must not be empty", "/title"))
        unknown: set[str] = set()
        for pointer, value in ptr.iter_strings(source):
            if "—" in value:
                issues.append(Issue("em-dash is not allowed", pointer))
            unknown.update(m[1] for m in _MARKER.finditer(value) if m[1] not in _KNOWN_NOTES)
        for note in sorted(unknown):
            issues.append(Issue(f"unknown note marker [^{note}]"))
        return issues

    def check_text(self, pointer: str, value: str) -> list[Issue]:
        return [Issue("em-dash is not allowed", pointer)] if "—" in value else []

    def figure_change(self, pointer: str, old: str, new: str) -> str | None:
        before, after = numeric_tokens(old), numeric_tokens(new)
        if before == after:
            return None
        return f"figures changed: {', '.join(before) or 'none'} -> {', '.join(after) or 'none'}"

    def panel_fields(self, source: Any, pointer: str) -> list[dict]:
        if pointer != "/chart":
            return []
        fields = [{"pointer": "/chart/title", "label": "Title", "kind": "plain"}]
        for i in range(len(source["chart"]["labels"])):
            fields.append({"pointer": f"/chart/labels/{i}", "label": f"Label {i + 1}", "kind": "plain"})
        return fields

    def build_command(self, project_dir: Path) -> Command:
        return Command([sys.executable, "-c", _BUILD_SCRIPT, str(project_dir)], project_dir)

    def build_result(self, project_dir: Path) -> BuildResult:
        return BuildResult([], [])

    def agent_cwd(self, project_dir: Path) -> Path:
        return project_dir

    def agent_context(self, project_dir: Path) -> dict[str, str]:
        return {"project_dir": str(project_dir), "source": str(self.source_path(project_dir))}
