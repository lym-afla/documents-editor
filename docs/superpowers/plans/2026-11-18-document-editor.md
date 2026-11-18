# Local Document Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local browser editor that edits agent-built documents in place, writes every edit to the JSON source, collects comments for an agent, runs the agent from a button and refreshes the page; plus the investment-memo adapter.

**Architecture:** Part A builds the generic `documents_editor` package (standard-library HTTP server, plain-JS front end) and a tiny example adapter used for its tests. Part B, in the memo repository, adds source pointers to the renderer, a `MemoAdapter`, `cli.py edit`, and agent instructions. The adapter runs in-process in the host project's environment.

**Tech Stack:** Python ≥ 3.11 standard library (`http.server`, `json`, `tomllib`, `subprocess`, `threading`); plain ES-module JavaScript, no build step; pytest; Playwright (dev only) for browser tests.

**Spec:** `docs/superpowers/specs/2026-11-18-document-editor-design.md` (read it before any task).

## Global Constraints

- `requires-python = ">=3.11"`; `.python-version` = `3.14`; tests pass on 3.14 and 3.11.
- Runtime dependencies of `documents-editor`: none. Dev: `pytest`, `pytest-timeout`, `playwright`.
- No external calls and no CDN assets from the editor; all JS/CSS served from the package.
- Server binds 127.0.0.1 only; every API call needs the session token.
- Subprocess commands are argument lists, never shell strings.
- The editor writes only: the source JSON, `edit_requests.json`, and files under `PROJECT_DIR/.editor/`.
- JSON writes are atomic (temp file in the same dir + `os.replace`), UTF-8, `ensure_ascii=False`, `indent=2`, key order kept, trailing newline kept as found.
- The editor never saves a source that fails `adapter.validate` (errors only; warnings do not block).
- Never read text from the rendered page to save it; always start from the raw source value at the pointer.
- Memo repo work follows its `AGENTS.md`: text rules (`US$`, en-dash, no `ё`), validation commands, preserve `CLAUDE.md` and `.claude/skills/memo/skill.md`.
- Workspace (`the workspace AGENTS.md`): do the work in worktrees `<worktrees>/documents-editor` (branch `feat/editor`) and `<worktrees>/investment-memo-generator` (branch `feat/editor-adapter`). They are siblings so the memo repo's relative path source `../documents-editor` resolves both there and in the primary checkouts under `<code root>`.

## Decisions refining the spec

- `Adapter.build(project_dir) -> BuildResult` becomes `build_command(project_dir) -> Command` plus `build_result(project_dir) -> BuildResult`, so builds use the same runner as the agent (streamed log, cancel). 
- `data-edit` kinds: `inline`, `plain`, `number-cell` (editable text); `chart` (fields in the panel); `none` (comment target only, e.g. exhibits, tables as a whole). The adapter adds `panel_fields(source, pointer)` for chart fields.
- History stores *states*: the state at open, then the state after every change (`edit`, `agent`, `undo`-free: undo moves a cursor), `external` when the file changed outside the editor. "Revert to before the agent run" = undo from the `agent` state.
- The inline markup grammar is the memo's (`**strong**`, `*em*`, `[^id]`), hard-coded in the converter for v1.
- Memo repo depends on the editor through an optional dependency group `editor` (`uv sync --group editor`) with uv path source `../documents-editor`, until the editor repo has a git remote. `cli.py edit` explains how to install it when missing.
- Agent-command confirmation is remembered per command (SHA-256 of the substituted argument list) in `confirmed.json` next to the user config file.

## Review Focus

1. **Source file not in canonical style** (other indent, `\u` escapes, `1.10` floats): expect a warning in Checks on open ("the first save will reformat this file") and byte-identical output for canonical files. Test in Task A2.
2. **Agent leaves broken files** (invalid JSON, half-written source, malformed or deleted `edit_requests.json`): no crash; page stays on the last good render with the error and Revert; requests file read as empty with a warning. Tests in Tasks A4 and A8.
3. **Browser contenteditable artefacts** (`&nbsp;`, trailing `<br>`, adjacent split `<strong>`, `<b>`/`<i>`, pasted rich HTML): normalised or routed to the raw editor, never saved as stray markup. Tests in Task A10.
4. **Pointers that no longer exist** after an agent run (shorter arrays, moved blocks) for comments and the edit log: no exception; edits re-found by value; comments shown as "detached" with their quote. Tests in Tasks A5 and A8.
5. **Project paths with spaces and Cyrillic** (`output/2611_Астро проект`) on any OS: command substitution, process-tree cancel, and `/project/` file serving work. Tests in Tasks A7 and A9.

---

# Part A – `documents-editor` package

File map (all under the editor worktree):

```
pyproject.toml, .python-version, README.md
src/documents_editor/
  __init__.py      version
  __main__.py      generic CLI: --adapter module:Class PROJECT_DIR
  pointer.py       RFC 6901 pointers over JSON-like data
  store.py         SourceStore: read/hash/atomic write/style check
  history.py       History: snapshot states with an undo cursor
  requests.py      RequestsFile: comments and figure requests
  figures.py       numeric_tokens
  edits.py         EditLog + survival_check
  adapter.py       Adapter protocol, Issue, Command, BuildResult, load_adapter
  config.py        user config, agent settings, substitution, confirmation
  runner.py        JobRunner: one subprocess at a time, streaming, cancel
  session.py       EditorSession: all editor logic, no HTTP
  events.py        EventBus for server-sent events
  server.py        HTTP routes, token/host checks, static and project files
  app.py           run(adapter, project_dir, port, open_browser)
  static/index.html, app.js, page.js, markup.js, editor.css, page.css
examples/simple_doc.py   SimpleDocAdapter (doc.json) for tests and as a reference
tests/ ...
```

### Task A1: Package scaffold and pointers

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `.gitignore`, `src/documents_editor/__init__.py`, `src/documents_editor/pointer.py`
- Test: `tests/test_pointer.py`

**Interfaces:**
- Produces (`documents_editor.pointer`):
  - `class PointerError(LookupError)`
  - `parse(pointer: str) -> list[str]` (`""` → `[]`; must start with `/`; unescape `~1`→`/`, `~0`→`~`)
  - `format(tokens: Iterable[str | int]) -> str`
  - `get(doc: Any, pointer: str) -> Any`
  - `set_value(doc: Any, pointer: str, value: Any) -> None` (replace existing only; never creates)
  - `iter_strings(doc: Any, prefix: str = "") -> Iterator[tuple[str, str]]` (pointer, string) in document order
  - `find_value(doc: Any, value: str, near: str) -> str | None`: pointer of the string equal to `value` sharing the longest token prefix with `near`; `None` if absent or if two candidates tie.

- [ ] **Step 1: Create worktrees** (from `<editor checkout>`: `git worktree add -b feat/editor <worktrees>/documents-editor`; same for the memo repo with branch `feat/editor-adapter`, used from Task B1). Write `pyproject.toml` (name `documents-editor`, `requires-python = ">=3.11"`, no dependencies, `[dependency-groups] dev = ["pytest>=9.0", "pytest-timeout>=2.3", "playwright>=1.55"]`, hatchling build backend, `src` layout, package data includes `static/*`, pytest `timeout = 60`), `.python-version` = `3.14`, `.gitignore` (`.venv/`, `__pycache__/`, `.editor/`). Run `uv sync`. Expected: environment created on 3.14.

- [ ] **Step 2: Write the failing tests**

```python
DOC = {"sections": [{"heading": "A", "blocks": [{"paragraphs": ["p0", "p1"]}]}],
       "a/b": {"m~n": "x"}, "n": 4}

def test_get_and_escape():
    assert get(DOC, "/sections/0/blocks/0/paragraphs/1") == "p1"
    assert get(DOC, "/a~1b/m~0n") == "x"
    assert format(["a/b", "m~n"]) == "/a~1b/m~0n"
    assert get(DOC, "") is DOC

def test_get_missing_raises_pointer_error():
    for bad in ["/sections/5", "/sections/x", "/nope", "sections", "/sections/-1", "/sections/01"]:
        with pytest.raises(PointerError):
            get(DOC, bad)

def test_set_value_replaces_only_existing():
    doc = copy.deepcopy(DOC)
    set_value(doc, "/sections/0/heading", "B")
    assert doc["sections"][0]["heading"] == "B"
    with pytest.raises(PointerError):
        set_value(doc, "/sections/0/new", "x")

def test_iter_strings_in_document_order():
    assert [p for p, _ in iter_strings(DOC)] == [
        "/sections/0/heading", "/sections/0/blocks/0/paragraphs/0",
        "/sections/0/blocks/0/paragraphs/1", "/a~1b/m~0n"]

def test_find_value_prefers_nearest_and_refuses_ties():
    doc = {"s": [{"p": ["x", "y"]}, {"p": ["y"]}]}
    assert find_value(doc, "y", near="/s/0/p/0") == "/s/0/p/1"
    assert find_value(doc, "y", near="/s/1/p/3") == "/s/1/p/0"
    assert find_value(doc, "y", near="/t") is None   # tie at depth 0
    assert find_value(doc, "zzz", near="/s") is None
```

- [ ] **Step 3: Run** `uv run pytest tests/test_pointer.py -v`. Expected: FAIL (module missing).
- [ ] **Step 4: Implement `pointer.py`** with the signatures above. Array tokens must be canonical non-negative integers (no leading zeros, no `-`).
- [ ] **Step 5: Run** the tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat: package scaffold and JSON pointers`.

### Task A2: Source store

**Files:**
- Create: `src/documents_editor/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`documents_editor.store`):
  - `class StaleSource(RuntimeError)`, `class SourceUnreadable(RuntimeError)` (holds `message`)
  - `dumps(doc: Any, trailing_newline: bool) -> str`
  - `atomic_write_text(path: Path, text: str) -> None`
  - `class SourceStore(path: Path)`: `read() -> tuple[Any, str]` (doc, sha256 hex of the bytes; raises `SourceUnreadable` on missing file or invalid JSON); `current_hash() -> str | None` (None if missing); `write(doc: Any, expected_hash: str) -> str` (raises `StaleSource` if the file's hash ≠ `expected_hash`; returns new hash); `is_canonical() -> bool` (`dumps(load(text)) == text`).

- [ ] **Step 1: Write the failing tests**

```python
def test_round_trip_is_byte_identical_for_canonical_file(tmp_path):
    p = tmp_path / "doc.json"
    text = json.dumps({"b": "Ёж – 'x'", "a": [1, 2.5]}, ensure_ascii=False, indent=2) + "\n"
    p.write_text(text, encoding="utf-8")
    store = SourceStore(p)
    doc, h = store.read()
    store.write(doc, h)
    assert p.read_text(encoding="utf-8") == text
    assert store.is_canonical()

def test_detects_non_canonical_style(tmp_path):  # Review Focus 1
    for text in ['{"a": 1.10}', '{\n    "a": 1\n}', '{"a": "\\u0451"}']:
        p = tmp_path / "doc.json"; p.write_text(text, encoding="utf-8")
        assert SourceStore(p).is_canonical() is False

def test_write_refuses_stale_hash(tmp_path):
    p = tmp_path / "doc.json"; p.write_text('{"a": 1}', encoding="utf-8")
    store = SourceStore(p); doc, h = store.read()
    p.write_text('{"a": 2}', encoding="utf-8")
    with pytest.raises(StaleSource):
        store.write(doc, h)

def test_unreadable_source(tmp_path):
    p = tmp_path / "doc.json"; p.write_text('{"a": ', encoding="utf-8")
    with pytest.raises(SourceUnreadable):
        SourceStore(p).read()

def test_atomic_write_leaves_no_temp_files(tmp_path):
    atomic_write_text(tmp_path / "x.json", "{}")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["x.json"]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_store.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `store.py`.** `atomic_write_text` writes a `tempfile.NamedTemporaryFile(dir=path.parent, delete=False)`, flushes, `os.fsync`, then `os.replace`; on Windows retry `os.replace` up to 5 times over 0.5 s on `PermissionError`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: atomic source store with hash check`.

### Task A3: History with undo/redo

**Files:**
- Create: `src/documents_editor/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: `store.atomic_write_text`.
- Produces: `class History(directory: Path, tracked: list[Path], cap: int = 200)` with
  - `record(reason: str) -> str`: copies the current content of every tracked file (an absent file is recorded as absent) into `directory/<YYYYmmdd-HHMMSS-ffffff>-<reason>/`, drops entries after the cursor from the index, appends, moves the cursor to it, prunes the oldest beyond `cap`; returns the id.
  - `undo() -> str | None`, `redo() -> str | None`: move the cursor and restore that state (restoring an absent file deletes it); return the id or `None` at the ends.
  - `can_undo: bool`, `can_redo: bool`, `entries() -> list[dict]` (`id`, `reason`, `time`), `current() -> str | None`.
  - Index persisted in `directory/index.json` as `{"cursor": int, "entries": [...]}` and reloaded on construction.

- [ ] **Step 1: Write the failing tests**

```python
def test_undo_redo_restores_source_and_requests_together(tmp_path):
    src, req = tmp_path / "doc.json", tmp_path / "edit_requests.json"
    src.write_text("v0"); h = History(tmp_path / ".editor/history", [src, req])
    h.record("open")
    src.write_text("v1"); req.write_text("r1"); h.record("agent")
    assert h.undo() and src.read_text() == "v0" and not req.exists()
    assert h.redo() and src.read_text() == "v1" and req.read_text() == "r1"
    assert h.redo() is None

def test_new_record_after_undo_drops_redo(tmp_path):
    src = tmp_path / "doc.json"; src.write_text("v0")
    h = History(tmp_path / "h", [src]); h.record("open")
    src.write_text("v1"); h.record("edit"); h.undo()
    src.write_text("v2"); h.record("edit")
    assert [e["reason"] for e in h.entries()] == ["open", "edit"] and not h.can_redo

def test_index_survives_restart_and_cap_prunes(tmp_path):
    src = tmp_path / "doc.json"; h = History(tmp_path / "h", [src], cap=3)
    for i in range(5):
        src.write_text(f"v{i}"); h.record("edit")
    h2 = History(tmp_path / "h", [src], cap=3)
    assert len(h2.entries()) == 3 and h2.current() == h.current()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_history.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `history.py`** per the interface. Pruned snapshot directories are deleted.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: snapshot history with undo and redo`.

### Task A4: Requests file and figure helper

**Files:**
- Create: `src/documents_editor/requests.py`, `src/documents_editor/figures.py`
- Test: `tests/test_requests.py`, `tests/test_figures.py`

**Interfaces:**
- Consumes: `store.atomic_write_text`.
- Produces:
  - `figures.numeric_tokens(text: str) -> list[str]`: numeric tokens in order, normalised so formatting-only differences compare equal: strip spaces/NBSP thousands separators, `,` decimal → `.`, keep sign as `-` for a leading `-`/`−` or enclosing brackets, keep a trailing `%`, `x` or `х`.
  - `class RequestsFile(path: Path)`:
    - `load() -> tuple[list[dict], str | None]` (requests, warning). Missing file → `([], None)`; invalid JSON or wrong shape → `([], "<message>")`. Unknown fields are preserved on rewrite.
    - `add(*, pointer: str | None, element: str, quote: str, category: Literal["format","text"], scope: Literal["document","house_style"], instruction: str) -> dict` (status `open`, `agent_note` `""`, `created` ISO seconds, id `r-YYYYMMDD-NNNN` unique within the file).
    - `update(request_id: str, **fields) -> dict` (allowed: `instruction`, `scope`, `category`, `status`); `delete(request_id: str) -> None`.
    - `upsert_figure(*, pointer: str, old: str, new: str, quote: str, note: str) -> dict | None`: one open `figure` request per pointer with `origin: "editor"`; keeps the first `old`; removes the request and returns `None` when `numeric_tokens(new) == numeric_tokens(first old)`.
    - `open_count() -> int`.
  - File shape: `{"version": 1, "requests": [...]}`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text,tokens", [
    ("Выручка 407,0 млн", ["407.0"]), ("(8.8%)", ["-8.8%"]), ("−12", ["-12"]),
    ("1 234,5", ["1234.5"]), ("2.5x и 3х", ["2.5x", "3х"]), ("2023A", ["2023"]), ("нет", [])])
def test_numeric_tokens(text, tokens):
    assert numeric_tokens(text) == tokens

def test_add_update_delete_and_ids(tmp_path):
    f = RequestsFile(tmp_path / "edit_requests.json")
    a = f.add(pointer="/t", element="table", quote="q", category="format", scope="document", instruction="right-align")
    b = f.add(pointer=None, element="document", quote="", category="text", scope="house_style", instruction="x")
    assert a["id"] != b["id"] and a["status"] == "open" and f.open_count() == 2
    f.update(a["id"], status="done"); f.delete(b["id"])
    assert [r["status"] for r in f.load()[0]] == ["done"]

def test_figure_upsert_keeps_first_old_and_removes_on_restore(tmp_path):
    f = RequestsFile(tmp_path / "r.json")
    f.upsert_figure(pointer="/c", old="407.0", new="412.5", quote="412.5", note="n")
    r = f.upsert_figure(pointer="/c", old="412.5", new="413", quote="413", note="n")
    assert r["old"] == "407.0" and r["new"] == "413" and len(f.load()[0]) == 1
    assert f.upsert_figure(pointer="/c", old="413", new="407,0", quote="", note="n") is None
    assert f.load()[0] == []

def test_tolerates_broken_file_and_preserves_unknown_fields(tmp_path):  # Review Focus 2
    p = tmp_path / "r.json"; p.write_text("{oops", encoding="utf-8")
    reqs, warning = RequestsFile(p).load()
    assert reqs == [] and warning
    p.write_text(json.dumps({"version": 1, "requests": [{"id": "r-1", "status": "open", "x_agent": 1}]}))
    f = RequestsFile(p); f.update("r-1", status="done")
    assert json.loads(p.read_text())["requests"][0]["x_agent"] == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_requests.py tests/test_figures.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `figures.py` and `requests.py` per the interfaces. Writing over a broken file is refused (raise `ValueError`) so the user's comments are never silently replaced; the session shows the warning.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: edit requests file and figure tokens`.

### Task A5: Edit log and survival check

**Files:**
- Create: `src/documents_editor/edits.py`
- Test: `tests/test_edits.py`

**Interfaces:**
- Consumes: `pointer.get`, `pointer.find_value`, `pointer.PointerError`, `store.atomic_write_text`.
- Produces:
  - `class EditLog(path: Path)`: `record(pointer: str, value: Any) -> None`, `forget(pointer: str) -> None`, `move(old: str, new: str) -> None`, `entries() -> dict[str, Any]`. Persisted in `.editor/edits.json`.
  - `@dataclass(frozen=True) class Lost: pointer: str; mine: Any; now: Any | None` (`now` is `None` when the pointer no longer resolves).
  - `survival_check(entries: dict[str, Any], after: Any, targeted: set[str]) -> tuple[list[Lost], dict[str, str]]`: returns lost edits and moves (`old pointer → new pointer`). For each entry: value at the pointer equals `mine` → kept; else if the pointer is in `targeted` (an open text/format request on it when the run started) → kept, no warning; else if `mine` is a string and `find_value(after, mine, near=pointer)` finds it → moved; else lost.

- [ ] **Step 1: Write the failing tests**

```python
def test_kept_moved_targeted_and_lost():
    after = {"p": ["new intro", "my text", "agent rewrote"], "q": "x"}
    entries = {"/p/0": "my text", "/p/2": "mine 2", "/q": "mine q", "/p/9": "gone"}
    lost, moves = survival_check(entries, after, targeted={"/q"})
    assert moves == {"/p/0": "/p/1"}
    assert {l.pointer for l in lost} == {"/p/2", "/p/9"}
    assert next(l for l in lost if l.pointer == "/p/9").now is None   # Review Focus 4

def test_edit_log_persists(tmp_path):
    log = EditLog(tmp_path / "edits.json"); log.record("/a", "x"); log.move("/a", "/b")
    assert EditLog(tmp_path / "edits.json").entries() == {"/b": "x"}
```

- [ ] **Step 2: Run** `uv run pytest tests/test_edits.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `edits.py`.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: edit log and survival check`.

### Task A6: Adapter contract, config and example adapter

**Files:**
- Create: `src/documents_editor/adapter.py`, `src/documents_editor/config.py`, `examples/simple_doc.py`
- Test: `tests/test_adapter_contract.py`, `tests/test_config.py`

**Interfaces:**
- Produces (`documents_editor.adapter`):
  - `@dataclass(frozen=True) class Issue: message: str; pointer: str | None = None; severity: Literal["error", "warning"] = "error"`
  - `@dataclass(frozen=True) class Command: args: list[str]; cwd: Path`
  - `@dataclass(frozen=True) class BuildResult: pages: list[Path]; warnings: list[Issue]`
  - `class Adapter(Protocol)`: `name: str`; `default_agent_command: list[str]`; `source_path(project_dir: Path) -> Path`; `render_edit_html(source: Any, project_dir: Path) -> str`; `validate(source: Any) -> list[Issue]`; `check_text(pointer: str, value: str) -> list[Issue]`; `figure_change(pointer: str, old: str, new: str) -> str | None`; `panel_fields(source: Any, pointer: str) -> list[dict]` (each `{"pointer", "label", "kind"}`); `build_command(project_dir: Path) -> Command`; `build_result(project_dir: Path) -> BuildResult`; `agent_cwd(project_dir: Path) -> Path`; `agent_context(project_dir: Path) -> dict[str, str]`.
  - `load_adapter(spec: str) -> Adapter` for `"module:Class"`; raises `ValueError` with the spec in the message.
- Produces (`documents_editor.config`):
  - `config_path() -> Path`: Windows `%APPDATA%\documents-editor\documents-editor.toml`; macOS `~/Library/Application Support/documents-editor/documents-editor.toml`; else `$XDG_CONFIG_HOME` or `~/.config` + `/documents-editor/documents-editor.toml`.
  - `@dataclass(frozen=True) class AgentSettings: command: list[str]; timeout_minutes: float = 30`
  - `agent_settings(adapter_name: str, default: list[str], path: Path | None = None) -> AgentSettings` (reads `[agent.<name>]` with `command`, `timeout_minutes`).
  - `substitute(args: list[str], values: dict[str, str]) -> list[str]` (`str.format_map` per argument; unknown placeholder → `ConfigError` naming it).
  - `is_confirmed(args: list[str]) -> bool`, `confirm(args: list[str]) -> None` (`confirmed.json` beside `config_path()`).
- Produces (`examples/simple_doc.py`): `SimpleDocAdapter`, source `PROJECT_DIR/doc.json` shaped `{"title": str, "paragraphs": [str], "table": [[str | number]], "chart": {"title": str, "labels": [str]}}`; renders `<h1 data-src="/title" data-edit="plain">`, `<p data-src="/paragraphs/i" data-edit="inline">` (markup → `<strong>`, `<em>`, `<sup>`), table cells (`number-cell` for numbers), `<figure data-src="/chart" data-edit="chart">`; `validate`: title non-empty, no `—` anywhere (located), `[^id]` markers must be in `{"a","b"}`; `check_text`: `—` → error; `figure_change` via `numeric_tokens`; `build_command`: `[sys.executable, "-c", <script writing PROJECT_DIR/build.log>]`; `build_result`: no pages, no warnings; `default_agent_command`: `[sys.executable, "-c", "print('configure an agent command')"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_simple_adapter_satisfies_protocol_and_marks_editable_elements(tmp_path):
    a = load_adapter("examples.simple_doc:SimpleDocAdapter")
    src = {"title": "T", "paragraphs": ["**b** 1[^a]"], "table": [["x", 2]], "chart": {"title": "c", "labels": ["l"]}}
    html = a.render_edit_html(src, tmp_path)
    assert 'data-src="/paragraphs/0" data-edit="inline"' in html
    assert 'data-src="/table/0/1" data-edit="number-cell"' in html
    assert a.validate({**src, "title": "—"}) == [Issue("em-dash is not allowed", "/title")]
    assert a.panel_fields(src, "/chart")[0]["pointer"] == "/chart/title"

def test_load_adapter_reports_bad_spec():
    with pytest.raises(ValueError, match="nope:X"):
        load_adapter("nope:X")

def test_agent_settings_and_substitution(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[agent.simple]\ncommand = ["agent", "{requests}"]\ntimeout_minutes = 5\n')
    s = agent_settings("simple", ["default"], path=cfg)
    assert s == AgentSettings(["agent", "{requests}"], 5)
    assert substitute(s.command, {"requests": "/p/Астро проект/edit_requests.json"}) == ["agent", "/p/Астро проект/edit_requests.json"]
    with pytest.raises(ConfigError, match="missing"):
        substitute(["{missing}"], {})
```

- [ ] **Step 2: Run** `uv run pytest tests/test_adapter_contract.py tests/test_config.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `adapter.py`, `config.py`, `examples/simple_doc.py` (add `examples` to pytest `pythonpath`).
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: adapter contract, user config, example adapter`.

### Task A7: Job runner

**Files:**
- Create: `src/documents_editor/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces:
  - `class Busy(RuntimeError)`
  - `@dataclass class Job: kind: str; command: Command; returncode: int | None = None; cancelled: bool = False; timed_out: bool = False; lines: list[str]`
  - `class JobRunner`: `start(kind: str, command: Command, *, timeout_s: float | None, on_line: Callable[[str], None], on_exit: Callable[[Job], None]) -> Job` (raises `Busy`); `cancel() -> None`; `busy: bool`; `current: Job | None`.
  - Output: stdout and stderr merged, decoded UTF-8 with `errors="replace"`, one callback per line from a reader thread; `on_exit` after the process ends and the reader drains.
  - Process tree: POSIX `start_new_session=True` and `os.killpg(SIGTERM)`, then `SIGKILL` after 5 s; Windows `CREATE_NEW_PROCESS_GROUP` and `taskkill /PID <pid> /T /F`. Timeout uses the same kill and sets `timed_out`.

- [ ] **Step 1: Write the failing tests**

```python
def run(cmd, tmp_path, **kw):
    done = threading.Event(); lines = []; jobs = []
    r = JobRunner()
    r.start("t", Command(cmd, tmp_path), timeout_s=kw.get("timeout"), on_line=lines.append,
            on_exit=lambda j: (jobs.append(j), done.set()))
    return r, done, lines, jobs

def test_streams_lines_in_cyrillic_path_with_spaces(tmp_path):  # Review Focus 5
    d = tmp_path / "Астро проект"; d.mkdir()
    r, done, lines, jobs = run([sys.executable, "-c", "import os;print(os.getcwd());print('ё')"], d)
    assert done.wait(10) and jobs[0].returncode == 0
    assert lines == [str(d), "ё"]

def test_busy_and_cancel_kills_child_tree(tmp_path):
    script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print('up',flush=True);time.sleep(60)"
    r, done, lines, jobs = run([sys.executable, "-c", script], tmp_path)
    with pytest.raises(Busy):
        r.start("t2", Command(["x"], tmp_path), timeout_s=None, on_line=print, on_exit=print)
    wait_until(lambda: lines == ["up"]); r.cancel()
    assert done.wait(10) and jobs[0].cancelled and not r.busy

def test_timeout(tmp_path):
    r, done, lines, jobs = run([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, timeout=0.5)
    assert done.wait(10) and jobs[0].timed_out
```

- [ ] **Step 2: Run** `uv run pytest tests/test_runner.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `runner.py`.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: job runner with streaming and process-tree cancel`.

### Task A8: Editor session

**Files:**
- Create: `src/documents_editor/session.py`, `src/documents_editor/events.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: everything from A1–A7.
- Produces:
  - `events.EventBus`: `subscribe() -> queue.Queue`, `unsubscribe(q)`, `publish(event: str, data: Any)`.
  - `class Locked(RuntimeError)`, `class Refused(RuntimeError)` (holds `issues: list[Issue]`), and `StaleSource` re-raised from the store.
  - `class EditorSession(adapter: Adapter, project_dir: Path, *, bus: EventBus, runner: JobRunner, config_file: Path | None = None)`; files: source from the adapter, `PROJECT_DIR/edit_requests.json`, `PROJECT_DIR/.editor/{history/, edits.json, lock}`. Methods:
    - `state() -> dict`: `{"hash", "valid", "locked", "job", "can_undo", "can_redo", "open_requests", "issues", "notices"}`. `notices` holds non-canonical-file and requests-file warnings.
    - `page_html() -> str`: the adapter's edit HTML for the current valid source, or the last good HTML while the source is invalid or unreadable.
    - `value(pointer: str) -> dict`: `{"value", "hash"}` from the raw source.
    - `check(pointer: str, value: str) -> list[Issue]`.
    - `patch(pointer: str, old: Any, new: str, source_hash: str) -> dict`: raises `Locked` while a job runs, raises `StaleSource` if the hash or the value at the pointer differs from `old`, and raises `Refused(issues)` if validation errors (an issue without a pointer gets the edited pointer). It converts `new` to a number when `old` is `int`/`float` and `new` parses as a number. On success it writes the source, records history `edit`, records the edit log, calls `figure_change` and `upsert_figure` (`old` = original value as `str`), publishes `state`, and returns `{"hash", "issues", "figure_request"}`. If `new == old` it writes nothing.
    - `undo() -> None`, `redo() -> None` (`Locked` while a job runs; publish `state`).
    - `requests() -> list[dict]`, `add_request(**kw) -> dict`, `update_request(id, **kw) -> dict`, `delete_request(id) -> None` (`Locked` while a job runs).
    - `agent_preview() -> dict`: `{"args": [...], "confirmed": bool}`, the substituted command (values `project_dir`, `source`, `requests` + `agent_context`).
    - `start_agent(confirm: bool = False) -> None`: refuses unless the command is confirmed or `confirm` is true (then `config.confirm`). Remembers the source before the run and `targeted` (pointers of open `text`/`format` requests), starts the runner with `cwd = adapter.agent_cwd`, and publishes `log` lines.
    - On agent exit, `_finish_agent(job)`: re-read the source. If it is unreadable or invalid, publish `agent-done` with `{"ok": false, "issues", "returncode"}` and keep the last good HTML. Otherwise record history `agent`, run `survival_check`, apply moves to the edit log, compute `changed` (pointers whose string values differ between before and after, via `iter_strings`), and publish `agent-done` with `{"ok", "returncode", "cancelled", "timed_out", "changed", "lost": [asdict], "issues"}`.
    - `reapply(pointers: list[str]) -> dict`: sets each lost pointer back to the user's value in one write, validated like `patch`. `accept(pointers: list[str]) -> None`: forgets them from the edit log.
    - `start_build() -> None` / on exit publish `build-done` with `{"ok", "pages": [names], "warnings"}`.
    - `cancel_job() -> None`.
    - `poll_external() -> bool`: called by the server about every second. If no job runs and the file hash changed, record history `external`, revalidate, publish `source-changed`, and return True.
  - Lock file: creating a session when `.editor/lock` holds a live PID adds a notice "another editor is open on this project"; the lock is removed by `close()`.

- [ ] **Step 1: Write the failing tests** (use `SimpleDocAdapter`, a fake agent script written to `tmp_path`, and a helper `wait_event(bus_queue, name)`):

```python
def test_patch_writes_source_records_history_and_edit_log(session, doc_path):
    h = session.state()["hash"]
    session.patch("/paragraphs/0", "one 'q'", "one 'q' **b**", h)
    assert json.loads(doc_path.read_text())["paragraphs"][0] == "one 'q' **b**"
    assert session.state()["can_undo"]
    session.undo(); assert json.loads(doc_path.read_text())["paragraphs"][0] == "one 'q'"

def test_patch_refuses_stale_value_and_invalid_result(session):
    h = session.state()["hash"]
    with pytest.raises(StaleSource):
        session.patch("/paragraphs/0", "not the old value", "x", h)
    with pytest.raises(Refused) as e:
        session.patch("/paragraphs/0", "one 'q'", "bad — dash", h)
    assert e.value.issues[0].pointer == "/paragraphs/0"
    with pytest.raises(Refused) as e:  # pointerless error attributed to the edit
        session.patch("/paragraphs/0", "one 'q'", "ref [^zz]", h)
    assert e.value.issues[0].pointer == "/paragraphs/0"

def test_number_cell_and_figure_request(session):
    h = session.state()["hash"]
    r = session.patch("/table/0/1", 2, "2.5", h)
    assert json.loads(session.adapter.source_path(session.project_dir).read_text())["table"][0][1] == 2.5
    assert r["figure_request"]["old"] == "2" and session.state()["open_requests"] == 1

def test_agent_run_reports_changes_and_lost_edits(session, bus, fake_agent):
    h = session.state()["hash"]; session.patch("/paragraphs/0", "one 'q'", "mine", h)
    fake_agent(set_paths={"/paragraphs/0": "regenerated", "/title": "New"}, mark_done=True)
    q = bus.subscribe(); session.start_agent(confirm=True)
    report = wait_event(q, "agent-done")
    assert report["ok"] and "/title" in report["changed"]
    assert report["lost"] == [{"pointer": "/paragraphs/0", "mine": "mine", "now": "regenerated"}]
    session.reapply(["/paragraphs/0"])
    assert session.value("/paragraphs/0")["value"] == "mine"

def test_locked_while_agent_runs(session, fake_agent):
    fake_agent(sleep=5); session.start_agent(confirm=True)
    with pytest.raises(Locked):
        session.patch("/title", "T", "X", session.state()["hash"])
    session.cancel_job()

def test_broken_agent_output_keeps_last_good_page(session, bus, fake_agent):  # Review Focus 2
    good = session.page_html()
    fake_agent(write_raw='{"title": ')
    q = bus.subscribe(); session.start_agent(confirm=True)
    assert wait_event(q, "agent-done")["ok"] is False
    assert session.page_html() == good and session.state()["valid"] is False
    session.undo(); assert session.state()["valid"] is True

def test_agent_requires_confirmation(session):
    assert session.agent_preview()["confirmed"] is False
    with pytest.raises(Refused):
        session.start_agent()

def test_external_change_detected(session, doc_path):
    d = json.loads(doc_path.read_text()); d["title"] = "Ext"
    doc_path.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    assert session.poll_external() and session.state()["can_undo"]

def test_comment_on_vanished_pointer_does_not_crash(session):  # Review Focus 4
    session.add_request(pointer="/paragraphs/7", element="paragraph", quote="old text",
                        category="format", scope="document", instruction="x")
    assert session.requests()[0]["pointer"] == "/paragraphs/7"
    assert session.state()["open_requests"] == 1
```

Fixtures: `doc_path` = `tmp_path/doc.json` with `{"title": "T", "paragraphs": ["one 'q'"], "table": [["x", 2]], "chart": {...}}`; `session` with `config_file=tmp_path/"cfg.toml"` and confirmation stored under `tmp_path` (patch `config.config_path`); `fake_agent(...)` writes a Python script and a config whose command runs it with `{source}` and `{requests}`.

- [ ] **Step 2: Run** `uv run pytest tests/test_session.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `events.py` and `session.py`.** All state changes happen under one `threading.RLock`, and runner callbacks take the same lock.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: editor session with patch, history, agent and build runs`.

### Task A9: HTTP server and entry points

**Files:**
- Create: `src/documents_editor/server.py`, `src/documents_editor/app.py`, `src/documents_editor/__main__.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `EditorSession`, `EventBus`, `JobRunner`, `load_adapter`.
- Produces:
  - `make_server(session: EditorSession, *, port: int = 0, token: str | None = None) -> tuple[ThreadingHTTPServer, str]` (server, URL `http://127.0.0.1:<port>/?t=<token>`).
  - `app.run(adapter: Adapter, project_dir: Path, *, port: int = 0, open_browser: bool = True) -> None`: prints the URL and the config path, starts the 1 s `poll_external` thread, opens the browser, serves until Ctrl+C, then `session.close()`.
  - `python -m documents_editor --adapter module:Class PROJECT_DIR [--port N] [--no-browser]`.
  - Auth: `GET /?t=<token>` sets cookie `de_token` (`HttpOnly; SameSite=Strict; Path=/`) and returns `static/index.html`. Every other route except `/static/*` needs the cookie or the header `X-Editor-Token`. A `Host` other than `127.0.0.1:<port>` / `localhost:<port>` gets 421, and an `Origin` that is present and not the same origin gets 403. A missing or wrong token gets 403 with the body "Open the editor from the URL printed in the terminal".
  - Routes (JSON in/out unless noted): `GET /api/state`; `GET /doc` (page HTML with `<base href="/project/">` after `<head>` and `<link rel="stylesheet" href="/static/page.css"><script type="module" src="/static/page.js"></script>` before `</body>`); `GET /project/<path>` (files under `project_dir`, URL-decoded, resolved path must stay inside, 404 otherwise); `GET /api/value?pointer=`; `POST /api/check`; `POST /api/patch` (409 `{"error":"locked"}`, 409 `{"error":"stale"}`, 422 `{"error":"invalid","issues"}`); `POST /api/undo`, `/api/redo`; `GET/POST /api/requests`, `PATCH/DELETE /api/requests/<id>`; `GET /api/fields?pointer=`; `GET /api/agent` (preview); `POST /api/agent` (`{"confirm": bool}`); `POST /api/agent/reapply`, `/api/agent/accept` (`{"pointers"}`); `POST /api/build`; `POST /api/cancel`; `GET /api/pages/<name>` (build previews, path-checked); `GET /events` (SSE: `event: <name>\ndata: <json>\n\n`, heartbeat comment every 15 s).

- [ ] **Step 1: Write the failing tests** (start `make_server` in a thread on port 0 with `SimpleDocAdapter`; use `urllib.request`):

```python
def test_token_host_and_origin_checks(server):
    assert get(server, "/api/state").status == 403
    assert get(server, "/api/state", token=True).status == 200
    assert get(server, "/api/state", token=True, headers={"Host": "evil.test"}).status == 421
    assert post(server, "/api/undo", token=True, headers={"Origin": "http://evil.test"}).status == 403

def test_patch_round_trip_and_error_codes(server):
    st = get_json(server, "/api/state")
    ok = post_json(server, "/api/patch", {"pointer": "/title", "old": "T", "new": "T2", "source_hash": st["hash"]})
    assert ok.status == 200
    stale = post_json(server, "/api/patch", {"pointer": "/title", "old": "T", "new": "T3", "source_hash": st["hash"]})
    assert stale.status == 409 and stale.json["error"] == "stale"
    bad = post_json(server, "/api/patch", {"pointer": "/title", "old": "T2", "new": "—", "source_hash": ok.json["hash"]})
    assert bad.status == 422 and bad.json["issues"][0]["pointer"] == "/title"

def test_doc_injects_editor_assets_and_project_files_are_confined(server, project_dir):  # Review Focus 5
    (project_dir / "Астро проект").mkdir(); (project_dir / "Астро проект" / "a.png").write_bytes(b"png")
    html = get(server, "/doc", token=True).text
    assert '<base href="/project/">' in html and "/static/page.js" in html
    assert get(server, "/project/" + quote("Астро проект/a.png"), token=True).body == b"png"
    assert get(server, "/project/..%2F..%2Fetc%2Fpasswd", token=True).status == 404

def test_events_stream_delivers_state(server):
    with open_sse(server) as stream:
        post(server, "/api/undo", token=True)
        assert next_event(stream, "state")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_server.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `server.py`, `app.py`, `__main__.py`. Run JSON handlers under the session lock; exceptions map to the status codes above and unexpected ones to 500 `{"error": "<type>: <message>"}`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: local HTTP server, SSE and entry points`.

### Task A10: Markup converter

**Files:**
- Create: `src/documents_editor/static/markup.js`
- Test: `tests/browser/test_markup.py`, `tests/browser/conftest.py` (Playwright `page` fixture loading `markup.js` from a `file://` test page or the running server)

**Interfaces:**
- Produces (ES module exports):
  - `toHtml(markup: string, kind: "inline" | "plain") -> string`: escaped text; for `inline` it renders `**x**` → `<strong>x</strong>`, `*x*` → `<em>x</em>`, and `[^id]` → `<span class="de-fn" contenteditable="false" data-fn="id">id</span>`, using the regex `\*\*(.+?)\*\*|\*([^*]+?)\*|\[\^([A-Za-z0-9_-]+)\]` (the memo renderer's).
  - `fromDom(root: Element, kind, original: string) -> {ok: true, markup: string, issues: string[]} | {ok: false, reason: string}`.
  - `literalStarIssue(markup: string) -> string | null`.
- Rules for `fromDom`:
  - If the root's normalised content equals that of `toHtml(original)`, return `original` unchanged.
  - Text nodes become text. NBSP becomes a space unless `original` contains NBSP. A trailing `<br>` is ignored.
  - `<strong>`/`<b>` become `**…**` and `<em>`/`<i>` become `*…*`, only when their children are plain text; nesting fails. Adjacent same-type siblings merge, and empty ones drop.
  - A `.de-fn` chip becomes `[^id]`.
  - Any other element (`<div>`, `<p>`, `<span style>`, `<a>`, inner `<br>`) returns `{ok: false}`, which sends the edit to the raw editor.
  - A literal `*` in a text node adds the issue "A literal * will be read as emphasis".
  - For `plain` kind, any element fails.

- [ ] **Step 1: Write the failing tests** (each runs `page.evaluate` against the module):

```python
@pytest.mark.parametrize("markup", ["", "plain 'q' – US$", "a **b** c *d* e[^n1]", "Ёлка **x**[^a_b]"])
def test_round_trip_unchanged(js, markup):
    assert js.round_trip(markup, "inline") == {"ok": True, "markup": markup, "issues": []}

@pytest.mark.parametrize("html,expected", [   # Review Focus 3
    ("a&nbsp;b<br>", "a b"),
    ("<strong>a</strong><strong>b</strong>", "**ab**"),
    ("<b>x</b> <i>y</i>", "**x** *y*"),
    ("x<strong></strong>", "x"),
])
def test_normalises_browser_artefacts(js, html, expected):
    assert js.from_html(html, "inline", original="seed")["markup"] == expected

@pytest.mark.parametrize("html", ["<div>a</div><div>b</div>", '<span style="color:red">a</span>',
                                  "<strong><em>x</em></strong>", "a<br>b", '<a href="x">l</a>'])
def test_unrepresentable_goes_to_raw_editor(js, html):
    assert js.from_html(html, "inline", original="seed")["ok"] is False

def test_literal_star_flagged(js):
    assert js.from_html("2 * 3", "inline", original="seed")["issues"] == ["A literal * will be read as emphasis"]

def test_plain_rejects_markup(js):
    assert js.from_html("<b>x</b>", "plain", original="x")["ok"] is False
```

- [ ] **Step 2: Run** `uv run playwright install chromium` (once), then `uv run pytest tests/browser/test_markup.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `markup.js`.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: lossless inline markup converter`.

### Task A11: Editor UI and end-to-end

**Files:**
- Create: `src/documents_editor/static/index.html`, `app.js`, `page.js`, `editor.css`, `page.css`
- Test: `tests/browser/test_e2e.py`

**Interfaces:**
- Consumes: the A9 routes, `markup.js`.
- Produces:
  - **Shell (`index.html`, `app.js`):**
    - Top bar: Undo, Redo, **Send to agent** (badge with the open-request count), **Rebuild**, status text, Cancel shown while a job runs.
    - The document iframe `src="/doc"` and a right panel with tabs Comments, Element, Checks, Pages, Log.
    - SSE handling: `state` updates the buttons and the lock banner. `source-changed` reloads the iframe and keeps the scroll position, or shows a "changed outside the editor" banner while an element is being edited. `log` appends to Log. `agent-done` reloads, sends the changed pointers to the page, refreshes Comments, and opens the lost-edits dialog when needed (per row: your text, current text, Re-apply / Keep agent's; plus "Revert to before the agent run" = Undo). `build-done` fills Pages with previews and warnings.
    - The first Send to agent shows the command from `GET /api/agent` in a confirm dialog when it is not yet confirmed.
    - Comments tab: list by status (open, needs_input, done, declined). Each item shows instruction, scope, category, status, agent note, and a "detached" tag when the pointer no longer resolves. Open items can be edited and deleted; done items can be reopened. A new-comment form has an instruction box, a category (`format`/`text`) and a scope (`document`/`house_style`).
    - Element tab: chart fields from `/api/fields`, and the raw editor (textarea plus Save, sending the same patch).
  - **Page script (`page.js`)**, injected into the iframe and talking to the shell via `window.parent.postMessage` with a same-origin check:
    - Hover outline on `[data-src]`.
    - Click on `inline`/`plain`/`number-cell` elements: fetch `/api/value`, replace the content with `toHtml(raw)`, set `contenteditable` (`plaintext-only` for plain and number-cell), and show a floating toolbar with **B** (`inline` only), *I* (`inline` only) and Comment. Ctrl/Cmd+B/I map to `document.execCommand('bold'|'italic')`.
    - Live check: 300 ms debounce, POST `/api/check`, issues shown under the element.
    - Enter or blur commits through `fromDom`. When it fails, the element is handed to the Element tab's raw editor with the current markup. Esc cancels.
    - Paste inserts plain text only.
    - Commit result: 200 swaps the page; 409 stale offers a reload; 422 shows the issues at the element and keeps it in edit mode; 409 locked shows a toast.
    - Clicking `chart` elements opens the Element tab. `none` elements and selected text offer Comment only (the quote is the selected text or the element's `textContent` cut to 200 characters).
    - Margin markers for elements with comments; `.de-changed` highlight on pointers from `agent-done` until the next edit.
    - While locked, the page is read-only.
  - Figure requests get a warning toast: "Figure changed – the agent will reconcile facts and calculations".

- [ ] **Step 1: Write the failing end-to-end test** (start the server with `SimpleDocAdapter` and a fake agent config, as in A8):

```python
def test_edit_bold_comment_agent_and_survival(page, editor):  # editor fixture yields url, doc_path, set_agent
    page.goto(editor.url)
    frame = page.frame_locator("iframe#doc")
    p = frame.locator('[data-src="/paragraphs/0"]')
    p.click(); page.keyboard.press("End"); page.keyboard.type(" plus")
    page.keyboard.press("Shift+Control+ArrowLeft"); page.keyboard.press("Control+b"); page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' **plus**")       # quote kept as '
    frame.locator('[data-src="/table/0/0"]').click(selector_comment_button=True)
    page.fill("#comment-instruction", "Right-align this column"); page.click("#comment-save")
    editor.set_agent(set_paths={"/title": "From agent"}, mark_done=True)
    page.click("#send-agent"); page.click("#confirm-command")
    expect(frame.locator('[data-src="/title"]')).to_have_text("From agent")
    expect(frame.locator('[data-src="/title"]')).to_have_class(re.compile("de-changed"))
    expect(page.locator(".comment-item .status")).to_have_text("done")
    editor.set_agent(set_paths={"/paragraphs/0": "regenerated"})
    page.click("#send-agent")
    expect(page.locator("#lost-edits")).to_contain_text("one 'q' **plus**")
    page.click("#lost-edits .reapply")
    expect_json(editor.doc_path, "/paragraphs/0", "one 'q' **plus**")

def test_invalid_edit_stays_open_with_issue(page, editor):
    page.goto(editor.url); f = page.frame_locator("iframe#doc")
    f.locator('[data-src="/title"]').click(); page.keyboard.type(" — x")
    expect(f.locator(".de-issue")).to_contain_text("em-dash")
    page.keyboard.press("Enter")
    expect_json(editor.doc_path, "/title", "T")
```

- [ ] **Step 2: Run** `uv run pytest tests/browser/test_e2e.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the shell and page script per the interface. CSS: editor chrome in `editor.css`; `page.css` holds only outline, chip, issue, marker and highlight styles, prefixed `de-`.
- [ ] **Step 4: Run** `uv run pytest -v` (whole suite). Expected: PASS.
- [ ] **Step 5: Manual check:** run `uv run python -m documents_editor --adapter examples.simple_doc:SimpleDocAdapter examples/sample` (add `examples/sample/doc.json`). Click, edit, bold, comment, Undo/Redo, and Rebuild; confirm nothing loads from outside 127.0.0.1 (DevTools Network).
- [ ] **Step 6: Run the suite on 3.11:** `uv run --python 3.11 pytest -v`. Expected: PASS.
- [ ] **Step 7:** Write `README.md`: what the editor does, adapter contract, config file and example agent command, security model. **Commit** `feat: editor UI with in-place editing, comments and agent runs`.

---

# Part B – Memo adapter (`investment-memo-generator` worktree)

Run memo commands from the memo worktree with `.venv/bin/python` (Linux) as `AGENTS.md` says.

### Task B1: Text rules in one function

**Files:**
- Modify: `memo_schema.py` (`validate_text_rules`, around line 539)
- Test: `tests/test_memo_schema.py`

**Interfaces:**
- Produces: `memo_schema.text_rule_issues(text: str) -> list[str]` returning, in this order, any of `"em-dash is not allowed; use en-dash"`, `"use е/Е instead of ё/Ё"`, `"bare currency symbol is not allowed; use US$"`. `MemoDocument.validate_text_rules` raises `ValueError` with the first issue of the first offending string (behaviour unchanged).

- [ ] **Step 1: Write the failing test**

```python
def test_text_rule_issues_lists_every_rule():
    assert text_rule_issues("ok US$ 5 – текст") == []
    assert text_rule_issues("— ё $5") == ["em-dash is not allowed; use en-dash",
                                          "use е/Е instead of ё/Ё",
                                          "bare currency symbol is not allowed; use US$"]
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_memo_schema.py -v`. Expected: the new test FAILS and the existing tests pass.
- [ ] **Step 3: Implement** `text_rule_issues` and make the validator use it.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `refactor: text rules as a reusable function`.

### Task B2: Source pointers in the renderer

**Files:**
- Modify: `html_renderer.py` (`_legacy_blocks`, `_render_block`, `_normalize_cell`, `_normalize_table`, `_section_runs`, `render_html`), `html_templates/components.html.j2`, `html_templates/company_full.html.j2`, `pyproject.toml`
- Test: `tests/test_html_renderer_pointers.py`

**Interfaces:**
- Consumes: `documents_editor.pointer.get` (test only).
- Produces:
  - `render_html_string(document: MemoDocument, exhibits: list[dict] | None = None) -> str`; `render_html` writes its result (unchanged signature).
  - Every source block dict gets `_ptr` (block pointer; for legacy fields the field pointer such as `/sections/i/paragraphs`). Prose and bullets blocks get `_item_ptrs: list[str]`, and table blocks get `_table_ptr`. These are set in `_section_runs` before the multi-paragraph split (each split block keeps its own one-item `_item_ptrs`) and before `_reorder_footnote_blocks`.
  - Rendered dicts carry `src` and the template macro `src_attrs(src, kind)` emits ` data-src="…" data-edit="…"` when `src` is set:
    - `<h1>` `/title` `plain`; `<h2>` `/sections/i/heading` `plain`.
    - Summary `<li>` `/summary/bullets/k` and `<aside>` `/summary/allocation`, both `inline`.
    - Prose `<p>` `…/paragraphs/k` and bullet `<li>` `…/items/k` (legacy `/sections/i/bullets/k`), both `inline`.
    - Table `<figure>` gets `_table_ptr` with `none`. The `<figcaption>` gets `…/title` `inline`. `<th>` gets `…/headers/c`. `<td>` gets `…/rows/r/c` or `…/rows/r/c/text` for a `MemoCell`, `inline` for strings and `number-cell` for `int`/`float`/`Decimal`. `None` cells get no `src`.
    - Footnote `<li>` `…/items/n/text` `inline`.
    - Chart `<figure>` gets the block pointer with `chart`. Asset, exhibit and exhibit-group figures get the block pointer with `none`.
  - Generated text (missing-component paragraphs, "Источник: …" captions, footnote numbers, exhibit content) carries no `data-src`.
  - `pyproject.toml`: `[dependency-groups] editor = ["documents-editor"]` and `[tool.uv.sources] documents-editor = { path = "../documents-editor", editable = true }`; add `documents-editor` to the `dev` group as well, so tests can import it.

- [ ] **Step 1: Write the failing tests.** The fixture `pointer_memo()` starts from `company_full_fixture()` and changes three sections:
  - section 0: blocks = prose with 2 paragraphs (one containing `'quoted'` and `**bold**[^f1]`), bullets, a table with a string cell `"(8.8%)"`, a `MemoCell` `{"text": "x[^f2]"}`, a numeric cell `407.0` and a `None` cell, then a footnotes block placed before the table that references `f2`, so it gets reordered;
  - section 1: legacy `paragraphs` and `bullets`;
  - section 2: a pie chart.

  It must pass `MemoDocument.model_validate`.

```python
def test_every_editable_pointer_resolves_to_its_raw_source(tmp_path):
    src = pointer_memo()
    html = render_html_string(MemoDocument.model_validate(src), [])
    marks = collect_data_src(html)          # list of (tag, pointer, kind, text) inside #memo-print-source
    for tag, ptr, kind, _ in marks:
        value = get(src, ptr)               # raises if the pointer is wrong
        if kind in ("inline", "plain"):
            assert isinstance(value, str), ptr
        if kind == "number-cell":
            assert isinstance(value, (int, float)), ptr
    ptrs = {m[1] for m in marks}
    assert {"/sections/0/blocks/0/paragraphs/1", "/sections/1/paragraphs/0", "/sections/1/bullets/0",
            "/sections/0/blocks/2/table/rows/0/1/text", "/sections/0/blocks/2/table/rows/0/2",
            "/sections/0/heading", "/title"} <= ptrs

def test_split_prose_and_reordered_footnotes_keep_original_indices():
    src = pointer_memo(); marks = collect_data_src(render_html_string(MemoDocument.model_validate(src), []))
    fn = [m for m in marks if m[1].endswith("/items/0/text")]
    assert fn and get(src, fn[0][1]) == src["sections"][0]["blocks"][3]["items"][0]["text"]

def test_editable_text_elements_all_carry_pointers():
    marks_missing = elements_without_src(render_html_string(MemoDocument.model_validate(pointer_memo()), []),
        tags=("p", "li", "td", "th", "h1", "h2"),
        skip_classes=("missing-component", "source-caption"), skip_inside=("exhibit", "sensitivity-pair", "exhibit-group"))
    assert marks_missing == []

def test_pointer_attributes_do_not_change_visible_text(tmp_path):
    doc = MemoDocument.model_validate(pointer_memo())
    assert visible_text(render_html_string(doc, [])) == visible_text(strip_data_src(render_html_string(doc, [])))
```

- [ ] **Step 2: Run** `uv sync --group dev` (pulls `documents-editor` from `../documents-editor`), then `.venv/bin/python -m pytest tests/test_html_renderer_pointers.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the renderer and template changes.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q` and the `AGENTS.md` compile check (`.venv/bin/python -m compileall …` list). Expected: PASS, including the existing renderer, pagination and assembler tests.
- [ ] **Step 5:** Build one real memo (`.venv/bin/python cli.py build output/<memo-project-b>/temp_memo.json --output-dir /tmp/<scratch>/memo`) and compare the PDF page count and `render_qa.json` warnings with a build from `main`. Expected: identical.
- [ ] **Step 6: Commit** `feat: source pointers on editable memo elements`.

### Task B3: Memo adapter and `cli.py edit`

**Files:**
- Create: `editor_adapter.py`
- Modify: `cli.py` (new `edit` command next to `build`)
- Test: `tests/test_editor_adapter.py`

**Interfaces:**
- Consumes: `documents_editor.adapter` (`Issue`, `Command`, `BuildResult`), `documents_editor.pointer`, `documents_editor.figures.numeric_tokens`, `documents_editor.app.run`, `render_html_string`, `text_rule_issues`, `calculate_exhibits`, `cli._QA_WARNINGS`.
- Produces: `class MemoAdapter` implementing `Adapter`:
  - `name = "memo"`; `source_path` → `project_dir / "temp_memo.json"`.
  - `render_edit_html`: validate the source, call `calculate_exhibits(document.calculations, date.today())`, then `render_html_string`.
  - `validate`: first, one located `Issue` per string per rule from `iter_strings` + `text_rule_issues`. Then `MemoDocument.model_validate`; each pydantic error becomes an `Issue` whose pointer is `pointer.format(loc)` when `loc` is non-empty, and `None` otherwise. Drop pointerless errors whose message repeats a located text-rule issue.
  - `check_text` → `text_rule_issues` as errors.
  - `figure_change`: when `numeric_tokens` differ, return `"Figure changed at {location}: {old} → {new}. Update the figure where it originates (facts.json, deal.yaml or calculation inputs), recompute, and check other text, tables and charts that state or depend on it."`. `{location}` is the section heading plus `table '<title>' row r col c` for cells, or `paragraph k` otherwise.
  - `panel_fields` for chart pointers: `title` (plain), pie `labels/i` (plain), bridge `steps/i/label` (plain).
  - `build_command` → `Command([sys.executable, "cli.py", "build", str(source)], cwd=<repo root>)`.
  - `build_result` → sorted `project_dir/pages/*.png`; one warning `Issue` per page listed under each `_QA_WARNINGS` key of `render_qa.json`, with messages like `"overflow on page 3"`.
  - `agent_cwd` → repo root; `agent_context` → `{"repo": str(repo_root)}`.
  - `default_agent_command = ["claude", "-p", "Process the open edit requests in {requests} for the memo at {source}. Follow the 'Edit requests' section of the memo skill.", "--permission-mode", "acceptEdits"]`.
  - `cli.py edit PROJECT_DIR [--port N] [--no-browser]`. When `documents_editor` is not importable, it prints `Editor not installed: run "uv sync --group editor"` and exits 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_validate_locates_text_rules_and_pydantic_errors():
    src = pointer_memo(); src["sections"][0]["blocks"][0]["paragraphs"][0] = "цена $5"
    issues = MemoAdapter().validate(src)
    assert Issue("bare currency symbol is not allowed; use US$", "/sections/0/blocks/0/paragraphs/0") in issues
    assert all(i.pointer for i in issues)          # the duplicate pointerless one is dropped
    src = pointer_memo(); src["sections"][0]["blocks"][2]["table"]["row_classes"] = ["total", "x"]
    assert MemoAdapter().validate(src)[0].pointer is not None or MemoAdapter().validate(src)[0].message

def test_figure_change_only_when_numbers_change():
    a = MemoAdapter(); ptr = "/sections/0/blocks/2/table/rows/0/0"
    assert a.figure_change(ptr, "(8.8%)", "(8,8%)") is None
    assert "→ (9.1%)" in a.figure_change(ptr, "(8.8%)", "(9.1%)")

def test_render_edit_html_has_pointers_and_build_result_reads_qa(tmp_path):
    a = MemoAdapter(); html = a.render_edit_html(pointer_memo(), tmp_path)
    assert 'data-edit="inline"' in html
    (tmp_path / "pages").mkdir(); (tmp_path / "pages" / "page-1.png").write_bytes(b"")
    (tmp_path / "render_qa.json").write_text('{"overflow_pages": [3]}')
    r = a.build_result(tmp_path)
    assert [p.name for p in r.pages] == ["page-1.png"] and r.warnings[0].message == "overflow on page 3"

def test_cli_edit_reports_missing_editor(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "documents_editor.app", None)
    assert cli_main_exit(["edit", "output/x"]) == 1
    assert "uv sync --group editor" in capsys.readouterr().err
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_editor_adapter.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `editor_adapter.py` and the `edit` command. Read `render_qa.json`'s real key shape from `render_qa.py` first; if pages are stored as objects rather than numbers, adapt the message builder and the test fixture.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q` and `.venv/bin/python cli.py --help`. Expected: PASS; `edit` is listed.
- [ ] **Step 5: Commit** `feat: memo editor adapter and cli edit`.

### Task B4: Agent instructions for edit requests

**Files:**
- Modify: `.agents/skills/memo/SKILL.md`, `.claude/skills/memo/skill.md` (same section in both), `README.md`/`USAGE.md` (one paragraph on `cli.py edit`)

**Interfaces:**
- Produces: a section titled **Edit requests** that tells the agent to:
  1. Read `PROJECT_DIR/edit_requests.json` and process requests with status `open`.
  2. Resolve `pointer` in `temp_memo.json`. If the value there no longer matches `quote`, find the quoted text; if that fails, set `needs_input`.
  3. Treat `temp_memo.json` as the source: change only that file, never re-run a script that rewrites it (`build_memo.py`, `write_memo.py` and similar), and keep every value the user edited directly unless a request asks for a change there.
  4. `text`: rewrite as asked, following the memo text rules.
  5. `format`: with `scope: document`, change only `temp_memo.json` fields the schema offers; when it can't express the request, set `needs_input` and say which field is missing. With `scope: house_style`, change `html_templates/` or `print.css` and say so in `agent_note`. Never edit HTML output.
  6. `figure`: update the figure where it originates (`facts.json`, `deal.yaml`, calculation inputs), recompute, check other text, tables and charts that state or depend on it, and list what changed in `agent_note`. If the source documents disagree with the user's figure, set `needs_input`.
  7. Write JSON atomically (temp file + rename). Run `cli.py validate` and fix any errors. Set `status` (`done` / `needs_input` / `declined`) and `agent_note` for each request. Do not run `cli.py build`: the user rebuilds from the editor.

- [ ] **Step 1: Write** the section in both skill files and the README paragraph.
- [ ] **Step 2: Verify** with the `AGENTS.md` skill validation (`quick_validate.py` on `.agents/skills/memo`, using the Linux path of the skill-creator scripts if present; otherwise note it was skipped) and `diff <(sed -n '/## Edit requests/,/^## /p' .agents/skills/memo/SKILL.md) <(sed -n '/## Edit requests/,/^## /p' .claude/skills/memo/skill.md)`. Expected: no difference.
- [ ] **Step 3: Commit** `docs: memo skill instructions for editor requests`.

### Task B5: Memo end-to-end smoke

**Files:**
- Test: `tests/test_editor_e2e.py` (marked `@pytest.mark.browser`; skipped when Playwright browsers are missing)

**Interfaces:**
- Consumes: `MemoAdapter`, `documents_editor.server.make_server`, `EditorSession`.

- [ ] **Step 1: Write the test.** Copy `pointer_memo()` to `tmp_path/temp_memo.json` and start the server. Then:
  - click `/sections/0/blocks/0/paragraphs/0`, type ` дополнение`, press Enter; that pointer now ends with ` дополнение`, the `'quoted'` text is still `'`, and every other byte of the file is unchanged (compare with `json.loads` minus that field);
  - type `—` in a heading: the live issue shows `em-dash` and the save is refused;
  - edit the numeric cell `407.0` → `412.5`: the file holds `412.5` as a number and `edit_requests.json` has one `figure` request whose instruction contains `407.0 → 412.5`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_editor_e2e.py -v`. Expected: PASS.
- [ ] **Step 3: Manual check on a real memo:** copy `output/<memo-project-b>` to `/tmp/<scratch>/memo_copy` and run `cli.py edit` on it. Edit text in a table and a footnote, add a format comment, press Rebuild, look at the page previews, then Send to agent with your configured command. Record what worked and what didn't in the branch handoff.
- [ ] **Step 4: Commit** `test: memo editor end-to-end smoke`.

---

## Handoff (after both parts)

Record in each branch's final message: branch, base commit, status, the manual-check notes from A11 and B5, and the follow-ups from the spec's "Later" section. Keep both worktrees until the branches are merged; remove them per `the workspace AGENTS.md`.
