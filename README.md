# documents-editor

Local browser editor for agent-built JSON documents.

A document generator (for example an investment memo pipeline) keeps its content in a
JSON source file and renders it to HTML. This editor shows that rendered page in your
browser and lets you:

- click a piece of text and edit it in place, with **bold** and *italic* (Ctrl/Cmd+B, I;
  Enter saves, Esc cancels, paste is plain text);
- see house-rule problems while you type, and never save a source that fails validation;
- leave comments for an agent on any element, a text selection or the whole document;
- press **Send to agent** to run your agent command on the open comments; the page then
  reloads with the fields the agent changed highlighted, comment statuses updated, and a
  warning if the agent overwrote any of your direct edits (re-apply, keep, or revert);
- undo and redo every save and agent run (the comments file is restored with the text);
- press **Rebuild** to run the full build and see page previews and layout warnings.

Edits always start from the raw source value at the element's JSON Pointer, never from
the rendered text, so unchanged characters stay exactly as they were.

## Running

```sh
uv sync
uv run python -m documents_editor --adapter examples.simple_doc:SimpleDocAdapter examples/sample
```

Options: `--port N` (default: a random free port) and `--no-browser`. The command prints
the editor URL (with its access token) and the path of the user config file. Projects
built on a document generator usually wrap this, e.g. `cli.py edit PROJECT_DIR`.

The editor writes only the source JSON, `PROJECT_DIR/edit_requests.json` (comments) and
files under `PROJECT_DIR/.editor/` (history snapshots, the edit log, a lock file).

## Adapter contract

An adapter connects the editor to one kind of document. It is a class loaded as
`module:Class` (see `documents_editor.adapter.Adapter` and `examples/simple_doc.py`):

| Member | Purpose |
| --- | --- |
| `name` | Key of the adapter's section in the user config file. |
| `default_agent_command` | Agent command used until the user configures one. |
| `source_path(project_dir)` | The JSON source file the editor edits. |
| `render_edit_html(source, project_dir)` | Fast HTML of the whole document. Editable elements carry `data-src="<JSON Pointer>"` and `data-edit="inline"` (markup `**`, `*`, `[^id]`), `"plain"`, `"number-cell"`, `"chart"` (fields in the panel) or `"none"` (comments only). |
| `validate(source)` | Whole-document `Issue`s; errors block saving. |
| `check_text(pointer, value)` | House rules for one string, for live checks while typing. |
| `figure_change(pointer, old, new)` | A note for the agent when an edit changes figures, else `None`; the editor then files a `figure` request. |
| `panel_fields(source, pointer)` | Editable text fields of a `chart` element: `{pointer, label, kind}`. |
| `build_command(project_dir)` / `build_result(project_dir)` | The full build (an argument list and cwd) and its page previews and warnings. |
| `agent_cwd(project_dir)` / `agent_context(project_dir)` | Where the agent runs and extra placeholder values for its command. |

`Issue(message, pointer=None, severity="error")`; an issue without a pointer is shown at
the element being saved.

## Agent command

The adapter's default command is only an example. Configure the real one per adapter in
`documents-editor.toml` in the user config directory (`~/.config/documents-editor/` on
Linux, `~/Library/Application Support/documents-editor/` on macOS, `%APPDATA%\documents-editor\`
on Windows; the exact path is printed at startup):

```toml
[agent.memo]
command = ["claude", "-p", "Process the open edit requests in {requests} for the memo at {source}, following the memo skill."]
timeout_minutes = 30
```

The command is an argument list (never a shell string). Placeholders: `{project_dir}`,
`{source}`, `{requests}`, plus any the adapter's `agent_context` adds. Non-interactive
agents need their own write-permission flags in the command. The first time a command is
used, the editor shows it and asks you to confirm; confirmations are stored next to the
config file.

## Security model

- The server binds to `127.0.0.1` only. For remote use, tunnel the port over SSH.
- Every API call needs the random session token. Opening the printed URL (`/?t=<token>`)
  sets an `HttpOnly`, `SameSite=Strict` cookie named `de_token_<port>`, so editors on
  different ports never share or overwrite each other's token.
- Requests with a foreign `Host` or `Origin` header are refused, so other local web pages
  cannot drive the editor.
- Project files are served only from inside the project directory, never `.editor/`.
- The editor makes no external calls and loads no external assets: all JavaScript and CSS
  ship with the package. Which agent you configure, local or cloud, is your choice.
- Subprocesses (agent, build) run as argument lists; Cancel stops the whole process tree.

## Development

```sh
uv run pytest -q                    # unit, server and Playwright browser tests
uv run --python 3.11 pytest -q      # oldest supported Python
uv run playwright install chromium  # once, for the browser tests
```
