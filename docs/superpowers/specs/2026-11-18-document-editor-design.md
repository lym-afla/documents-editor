# Local editor for agent-built documents – design

Date: 2026-11-18. Status: approved 2026-11-18.
Origin: `investment-memo-generator/docs/superpowers/specs/2026-11-17-html-document-editor-brief.md`.

## Intent

An agent writes a structured JSON source; a build step renders HTML/PDF/DOCX from it.
Today, changing wording means editing JSON by hand or asking the agent for every
change, and edits made to the HTML are lost on the next build.

The editor lets the user, in one browser window:

1. edit text directly on the rendered page, with edits written to the JSON source;
2. leave comments (mostly formatting: font size, colour, column alignment, chart
   axes/labels/ticks/colours, table placement) for an agent;
3. press **Send to agent**, wait, and see the same window refresh with the agent's
   changes highlighted;
4. rebuild the PDF and look at page previews and layout warnings.

The point is not to leave the document: no closing the editor, switching to the
agent, explaining, and reopening.

## Goals

- Edits go to the JSON source and survive every rebuild. The JSON is the only
  thing the editor writes (besides its own comments file and history).
- What-you-see editing on the real page (approach A, below).
- Comments stored in a plain file any agent or model can read.
- One button runs a configured agent command; the page refreshes when it exits.
- One adapter per document type; investment memos first.
- Local only, bound to 127.0.0.1, works on Windows, Linux and macOS from `uv sync`.

## Non-goals (version 1)

- AI inside the editor. The editor reads and writes files and runs commands.
- Simultaneous editing by user and agent. Work is turn-based (see Agent run).
- Structural editing: adding, deleting or moving paragraphs, list items, table rows,
  blocks, page breaks. These go through comments for now.
- Editing calculated exhibits or numeric chart values. Exhibits are computed from
  calculation inputs; chart values feed the chart geometry. Both are read-only;
  comments can still request changes. (Figures in tables and prose *are* editable; see
  Figure changes.)
- Applying formatting itself. Formatting is requested through comments and done by
  the agent.
- A second adapter. The contract is kept small; generality is proven later.
- Login, multi-user, remote hosting.

## Anti-goals (failure even if it "works")

- An edit that silently alters other source text (quote style, numbers, markup).
- An edit or comment that lands on the wrong field.
- Agent formatting that lives only in the HTML and disappears on rebuild.
- A failed agent run or invalid save that leaves the source broken with no way back.
- Any network call leaving the machine from the editor itself.

## Constraints

- Documents are confidential: no external calls, no CDN assets; all JS/CSS served
  from the package.
- Declared `requires-python >= 3.11`; developed on Python 3.14 (`.python-version`),
  tested on 3.14 and 3.11. The editor runs inside the host project's environment (memo
  repo: 3.13, floor 3.11), so it must not force an upgrade. Runtime uses only the
  standard library (no server framework), so adding it to any project is free of
  dependency conflicts.
- Plain JavaScript (ES modules), no Node build step.
- Subprocess commands are given as argument lists, not shell strings, so they work the
  same on Windows and POSIX.

## Approaches considered

For the editing surface:

- **A. Edit in place on the real page (chosen).** Click a paragraph, bullet, heading,
  table cell or footnote and type into it; bold/italic shown as such; footnote markers
  as protected chips. Comments and chart labels use a side panel. Feels like a word
  processor. Costs a careful, well-tested markup ↔ HTML converter.
- **B. Click on the page, edit raw source in a panel.** Simple and lossless, but the user
  edits `**markup**`, not what they see. Kept as A's fallback for any value A cannot
  convert exactly.
- **C. List of all text fields beside a preview.** Simplest, but far from the page.

The user chose A.

For the agent trigger: a configured command run by the button (chosen) versus an agent
that watches for a "ready" flag (needs an agent loop running all the time) versus an MCP
server (still needs the user to go to the agent). The command keeps the editor
model-agnostic and needs one line of config.

For concurrency: the user does not expect simultaneous work, so the editor locks while
the agent runs; a source-hash check on save covers edits made outside the editor.

## Architecture

```
documents-editor (this repo)                investment-memo-generator
┌──────────────────────────────┐           ┌───────────────────────────────┐
│ documents_editor             │  Adapter  │ editor_adapter.py (MemoAdapter)│
│  server (stdlib http)        │◄──────────│  render with data-src pointers │
│  pointer / patch / history   │  protocol │  validate → located errors     │
│  requests file               │           │  text rules per string         │
│  agent + build runner        │           │  build (cli.py build)          │
│  static/ (JS, CSS)           │           │ html_renderer + templates emit │
└──────────────────────────────┘           │  data-src / data-edit          │
                                           │ cli.py edit PROJECT_DIR        │
                                           └───────────────────────────────┘
```

The editor is its own package in this repository (`documents-editor`, import
`documents_editor`). The memo repository depends on it (uv path source now, a git URL
once this repo has a remote), implements the adapter, and adds `cli.py edit`. The adapter
runs in-process, in the memo project's environment, so it can call `render_html` and the
schema directly.

### Adapter contract

```python
class Adapter(Protocol):
    name: str
    def source_path(self, project_dir: Path) -> Path: ...
    def render_edit_html(self, source: dict, project_dir: Path) -> str:
        """Full HTML page with data-src pointers on editable elements. Fast, no PDF."""
    def validate(self, source: dict) -> list[Issue]:
        """Whole-document validation; each Issue carries a pointer when known."""
    def check_text(self, pointer: str, value: str) -> list[Issue]:
        """House rules for one string, for live checks while typing."""
    def build(self, project_dir: Path) -> BuildResult:
        """Full build. Returns page preview images and layout warnings."""
    def figure_change(self, pointer: str, old: str, new: str) -> str | None:
        """A note for the agent when an edit changes figures, else None."""
    def agent_context(self, project_dir: Path) -> dict:
        """Values substituted into the agent command (paths etc.)."""
```

`Issue = {pointer: str | None, message: str, severity: "error" | "warning"}`.
`validate` may return a single issue without a pointer; the memo validator stops at its
first model-level error. The editor copes with that (see Saving).

### Pointers

- Format: RFC 6901 JSON Pointer into the source JSON, e.g.
  `/sections/3/blocks/1/paragraphs/0`, `/sections/2/blocks/4/table/rows/1/2/text`.
- Emitted as `data-src="<pointer>"` plus `data-edit="<kind>"` on the element that shows
  exactly one source string. Kinds: `inline` (markup allowed: `**`, `*`, `[^id]`),
  `plain` (no markup, e.g. headings, chart labels), `number-cell` (a table cell whose
  JSON value is a number rather than a string).
- Attributes are invisible and always emitted; the paginator's copies inherit them, but
  the editor edits only the continuous view.
- Elements without `data-src` are not editable (exhibits, generated captions such as
  "Источник: …", footnote numbers). Charts carry `data-src` on the chart block and the
  panel lists its editable text fields.

### Source of truth and the raw-value rule

The renderer changes text on the way to HTML (`'` → `"`, brackets on negative numbers,
`[^id]` → numbers, emphasis markup → tags). Therefore the editor **never reads text from
the rendered page**. When an element enters edit mode, the browser fetches the raw source
string for its pointer and renders it with the editor's own minimal converter (text,
`<strong>`, `<em>`, footnote chip). On save, the converter turns the edited DOM back into
markup. Unchanged characters stay exactly as they were in the source.

## Memo adapter specifics

- **Source file:** `PROJECT_DIR/temp_memo.json`, the build input. `memo.json` is a
  normalised copy the build overwrites; the editor never writes it.
- **Renderer changes** (`html_renderer.py`, `components.html.j2`): thread the source
  pointer through rendering and emit it in the templates. Cases:
  - block paragraphs and bullet items: `/sections/i/blocks/j/paragraphs/k`, `/items/k`.
    The pointer is attached before multi-paragraph prose is split and before footnote
    blocks are reordered, so it keeps the original indices;
  - old-style section fields: `/sections/i/paragraphs/k`, `/sections/i/bullets/k`,
    `/sections/i/tables/t/...`;
  - section headings `/sections/i/heading`, document title, summary bullets and allocation;
  - table title, headers and cells: string cell `…/rows/r/c`, structured cell
    `…/rows/r/c/text`, numeric cell `…/rows/r/c` with kind `number-cell`;
  - footnote items `…/items/n/text`;
  - chart title, pie labels, bridge step labels (panel only).
- **Text rules:** move the checks out of `MemoDocument.validate_text_rules` into a
  function `text_rule_issues(text) -> list[str]` that both the schema validator and the
  adapter use, so the rules exist once. Rules: no em-dash, no `ё`/`Ё`, no bare `$`. The
  brief's "brackets for negatives" is a display rule the renderer already applies, not
  a text check.
- **Validation:** `MemoDocument.model_validate(source)`; each pydantic error's `loc` is
  turned into a pointer. Model-level validators (e.g. text rules) have no `loc`, so the
  adapter also runs `text_rule_issues` per string to locate them.
- **Build:** `assemble_html(temp_memo.json, PROJECT_DIR)`; previews from `pages/*.png`,
  warnings from `render_qa.json` (the `_QA_WARNINGS` keys in `cli.py`).
- **Agent instructions:** a section in the memo skill (`.agents/skills/memo/SKILL.md`
  and the Claude copy) on processing `edit_requests.json`: resolve pointers, use the
  quote if a pointer no longer matches, make text changes in `temp_memo.json`, make
  formatting changes only in the JSON or the templates (never in the HTML), respect
  scope, validate, set each request's status and note, write the JSON atomically
  (temp file + rename).
- **Source is `temp_memo.json`, not the scripts.** Some projects generate
  `temp_memo.json` with a per-project script that holds the text inline
  (`output/<memo-project-a>/build_memo.py`, `<memo-project-b>/write_memo.py`). Once a document
  is opened in the editor, `temp_memo.json` is the source: the agent instructions forbid
  re-running any script that rewrites it, and the editor checks after each run (see
  Agent run).
- **Figures:** `figure_change` compares the numeric tokens (digits with `.`/`,`
  separators, `%`, `x`, brackets for negatives) in the old and new string. If they
  differ, it returns a note such as "Figure changed in table 'Выручка' row 2 col 3:
  407.0 → 412.5". Figures come from `facts.json` and calculation inputs, so the agent
  instructions say how to reconcile: update `facts.json` / `deal.yaml` / calculation
  inputs where the figure originates, recompute, and check other text, tables and
  charts that state or depend on it; report what changed in `agent_note`, or mark
  `needs_input` if the source document disagrees.
- **Formatting gap:** the memo schema has few style fields today (`table_class`,
  `row_classes`, chart `show`). "This document" formatting requests that the schema
  cannot express should be marked `needs_input` by the agent with a note, not hacked
  into HTML. Growing the schema with style fields is follow-up work in the memo repo,
  driven by the comments that actually arrive.

## Editor behaviour

### Page

- The adapter's edit HTML is shown in an iframe (same origin) with the real stylesheet,
  continuous view only. The editor injects its script and CSS: hover outline on
  editable elements, a dimmed look for read-only ones.
- Right-hand panel with tabs: **Comments**, **Element** (chart fields, raw-text
  fallback), **Checks** (current issues), **Pages** (previews after a build), **Log**
  (agent/build output).

### Text editing (approach A)

- Click an editable element → it becomes `contenteditable`, filled from the raw source
  value. Toolbar: **B**, *I*, Comment. Ctrl/Cmd+B / I work.
- Allowed content: text, one level of `<strong>` or `<em>` (no nesting: the markup does
  not support `***`), footnote chips. Paste is converted to plain text.
- Footnote chips cannot be edited; they can be deleted (validation then reports an
  unreferenced footnote if that matters).
- Enter commits (no new paragraphs in v1); Esc cancels; clicking elsewhere commits.
- A literal `*` typed in text would be read as markup by the renderer: the converter
  flags it as a check issue and the user can fix it or use the raw editor.
- `plain` elements get no B/I. `number-cell` elements keep a number if the new text
  parses as one, otherwise become a string.

### Figure changes

- Figures in table cells and prose are editable. When a save changes figures (the
  adapter's `figure_change` returns a note), the editor shows a warning on the element
  ("figure changed – the agent will reconcile facts and calculations") and adds a
  request automatically: `category: "figure"`, `origin: "editor"`, `pointer`, `quote`
  (new text), `old` and `new` values, instruction = the adapter's note, status `open`.
- Several edits to the same field before an agent run update one request (keeping the
  first `old`). An edit that restores the original value removes it.
- These requests show in the Comments tab like any other and count as open work for
  **Send to agent**.
- If the converter cannot represent an edit exactly, the element falls back to the raw
  text editor in the panel (approach B) instead of guessing.
- Live checks: while typing, debounced (~300 ms) call to `check_text`; issues are shown
  under the element and in the Checks tab.

### Saving

- Every commit sends a patch: `{pointer, old, new, source_hash}`.
- The server applies it only if the file's current hash equals `source_hash` and the
  value at `pointer` equals `old`. Otherwise it refuses and the page offers to reload
  (the file was changed outside the editor).
- Before writing, the server runs `validate`. The editor never writes a source that
  fails validation (the memo renderer needs a valid document). Each save changes one
  field of a document that was valid, so any error is caused by that edit: it is shown
  at the edited element even when the validator gives no location (most memo rules are
  model-level and report one error without a path). Example: deleting the only marker
  of a footnote fails with "footnote … is never referenced"; removing a footnote is a
  structural change and goes through a comment in v1.
- If the source on disk is already invalid (a half-finished draft, a broken agent run),
  the page keeps showing the last good render, read-only, with the error and, when a
  snapshot exists, **Revert**. Editing resumes once the file is valid again.
- Writes are atomic (temp file + rename) and keep the JSON style of the file (UTF-8,
  `ensure_ascii=False`, two-space indent, key order kept).
- After a save the server re-renders; the page swaps content and keeps scroll position.

### History

- Every save and every agent run records a snapshot of the source **and**
  `edit_requests.json` in `PROJECT_DIR/.editor/history/<timestamp>-<reason>/`
  (reason: `edit`, `agent`, `undo`, `revert`).
- **Undo / Redo** is a linear stack with a cursor over these snapshots, stepping back
  past agent runs too. A new save after undo drops the redo branch (snapshots stay on
  disk). Restoring a snapshot restores the comments file with it, so comment statuses
  always match the text. Inside an element being edited, the browser's own undo works.
- **Revert to before the agent run** is undo to the `agent` snapshot.
- History is kept for the session plus a cap (default 200 files); the builder may add
  pruning.

### Comments

Stored in `PROJECT_DIR/edit_requests.json`:

```json
{
  "version": 1,
  "requests": [
    {
      "id": "r-20261118-0007",
      "created": "2026-11-18T15:04:00",
      "pointer": "/sections/4/blocks/2/table",
      "element": "table",
      "quote": "Выручка, US$ млн | 2023A | 2024A",
      "category": "format",
      "scope": "document",
      "instruction": "Right-align column 2 and keep the table on one page",
      "status": "open",
      "agent_note": ""
    }
  ]
}
```

- `pointer` may be `null` for a comment on the whole document.
- `category`: `format`, `text` (a rewrite request, as opposed to a direct edit), or
  `figure` (added by the editor when a direct edit changed figures; carries `old`,
  `new` and `origin: "editor"`).
- `scope`: `document` (this document only) or `house_style` (the template / all
  documents).
- `status`: `open` → `done` | `needs_input` | `declined` (set by the agent, with
  `agent_note`).
- Comment targets: any element with `data-src`, any block-level element (paragraph,
  table, chart, exhibit, figure – exhibits included, they are read-only for editing but
  not for comments), a text selection (stored as the element pointer plus the selected
  text as `quote`), or the whole document.
- Comments show as margin markers on their element; the Comments tab lists them by
  status. The user can edit or delete open comments, and reopen done ones.

### Agent run

- **Send to agent** is enabled when there are open comments (or always; it can also be
  used after direct edits only). It:
  1. commits any element being edited, saves, snapshots the source (`agent`);
  2. locks the page (read-only, banner "Agent working…", Cancel button);
  3. runs the configured command with substitutions, cwd = the project root the
     adapter names, output streamed to the Log tab;
  4. on exit, re-reads the source, runs `validate`, re-renders, unlocks.
- After the run: changed fields (diff of the snapshot and the new source by pointer) are
  highlighted on the page; comment statuses and notes update.
- **Edit survival check:** the editor keeps the list of fields the user edited directly
  (pointer and value). After every agent run it checks each one still holds the user's
  value, unless an open request on that pointer asked for a change there. Any that
  don't (for example because the agent re-ran a generator script) are listed in a
  warning with the user's value, the new value, and **Revert to before the agent
  run** / **Re-apply my edits** / **Keep agent's version**. Pointers are matched by
  value if the document structure moved. The list is kept in `.editor/` and cleared
  per field once the user accepts. If the source is invalid
  or the command failed, the page shows the error and offers **Revert to before the
  agent run**.
- Cancel terminates the process tree and leaves the source as it is, with the revert
  offer.
- The no-network rule applies to the editor process. Which agent runs, local or cloud,
  is the user's choice, as it is today when they run the agent by hand. The shipped
  default is an example the user confirms on first use; startup prints the command that
  will run. Non-interactive agents need write permission flags (e.g. Claude Code's
  permission mode / allowed tools), which belong in the configured command.
- Command configuration: the adapter supplies a default; a user config file
  (`documents-editor.toml` in the user config dir, path printed at startup) can override
  it per adapter. Command is an argument list with placeholders `{project_dir}`,
  `{source}`, `{requests}` plus any from `agent_context`. Example:

```toml
[agent.memo]
command = ["claude", "-p", "Process the open edit requests in {requests} for the memo at {source}, following the memo skill."]
timeout_minutes = 30
```

### Build and previews

- **Rebuild** saves, locks, runs `adapter.build`, then shows page previews and layout
  warnings in the Pages tab. A warning that names a page links to that preview.

### Server

- `ThreadingHTTPServer` on 127.0.0.1, random free port by default (`--port` to fix it),
  opens the browser. Remote use through an SSH tunnel.
- A random token in the URL, required on every API call, so other local pages cannot
  drive the editor. Requests with a foreign `Origin`/`Host` are rejected.
- JSON API, roughly: `GET /` (shell), `GET /doc` (edit HTML), `GET /value?pointer=`,
  `POST /check`, `POST /patch`, `POST /undo`, `GET/POST /requests`, `POST /agent`,
  `POST /agent/cancel`, `POST /build`, `GET /events` (server-sent events for log lines,
  lock state and "source changed"), static assets.
- The server checks the source file's hash about once a second while idle and pushes
  "source changed outside the editor" so the page can reload.
- One editor process per project directory; a lock file in `.editor/` warns when a
  second one starts.

### Entry points

- Memo repo: `cli.py edit PROJECT_DIR [--port N] [--no-browser]`.
- Generic: `python -m documents_editor --adapter module:Class PROJECT_DIR`.

## Testing

- Python unit tests: pointer resolution, patch apply with old-value and hash checks,
  atomic write and JSON style preservation, history and undo, requests file, agent
  runner (fake command), path/token security.
- Converter tests: markup → DOM → markup round-trips (emphasis, footnotes, quotes,
  literal `*`, Cyrillic, empty strings) run in a real browser through Playwright as a
  dev dependency; no Node.
- Memo adapter tests (memo repo): every editable element in a representative memo has a
  pointer that resolves to a string equal to its source; legacy fields; split prose;
  reordered footnotes; structured and numeric cells; pydantic `loc` → pointer.
- Figure detection tests (memo adapter): numeric token diff in cells and prose, brackets,
  `%`, thousands separators, no false positive on unchanged figures.
- One end-to-end test: open editor on a sample memo, edit a paragraph, bold a word,
  add a comment, run a fake agent command that edits the JSON and marks the comment
  done, check the page shows both; a second fake command that overwrites the edited paragraph
  must trigger the survival warning.

## Decided calls (made during design, open to correction)

- Separate package in `documents-editor`; memo repo holds the adapter.
- Standard-library server, plain JS, no external assets.
- Continuous view for editing; PDF previews only after Rebuild.
- Turn-based agent runs with page lock; hash check on save for outside edits.
- Comments carry scope (document / house style) and a quote for re-anchoring.
- No structural editing in v1. Figures in tables and prose are editable; a figure
  change automatically creates a request so the agent reconciles facts and
  calculations. Exhibits and chart values stay read-only.
- Editor never saves an invalid source; undo/redo covers source and comments together.
- After each agent run the editor checks that the user's direct edits survived.
- `temp_memo.json` is the memo source the editor writes.

## Left to the builder

- Internal module layout, API naming details, CSS look of the editor chrome.
- History pruning policy beyond the cap.
- Exact SSE vs polling mechanics.
- How the converter is structured, provided it round-trips exactly and falls back to
  the raw editor otherwise.

## Later

Block operations (add/delete/move paragraphs, bullets, rows, page breaks); style fields
in the memo schema driven by real comments; side-by-side diff of snapshots; a second
adapter to prove the contract; DOCX build in the adapter.
