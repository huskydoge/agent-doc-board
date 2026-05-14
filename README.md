# Agent Doc Board

Agent Doc Board is a small local documentation board for agent-managed projects.
It scans markdown documents, writes a project manifest, generates a markdown
index and TODO file, and serves a local browser UI for reading, linking, and
tracking project notes.

The tool is intentionally dependency-light: the server and scanner use the
Python standard library. Project-specific behavior lives in
`.agent-docs/config.toml` inside the project being monitored.

## Features

- Scan selected markdown files into `.agent-docs/manifest.json`.
- Generate `docs/INDEX.md` and seed `docs/TODO.md`.
- Browse docs by category with search and date/title/category sorting.
- Render markdown and KaTeX math locally.
- Resolve Pandoc-style citations such as `[@key]` from configured BibTeX files.
- Show outgoing links, backlinks, related docs, and topic timelines.
- Keep local per-document read checks and side comments in
  `.agent-docs/doc_state.json`.
- Expose configured non-markdown data artifacts in the board sidebar.

## Install For Local Development

```bash
cd /path/to/agent-doc-board
python -m pip install -e .
```

You can also run without installing by setting `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/agent-doc-board python -m agent_doc_board --help
```

## Usage

From any project with markdown docs:

```bash
agent-doc-board scan --root /path/to/project --write
agent-doc-board serve --root /path/to/project --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

## Project Files

- `.agent-docs/config.toml`: project-specific include rules, categories, TODO seeds, BibTeX files, and data references.
- `.agent-docs/manifest.json`: generated manifest consumed by the board.
- `.agent-docs/doc_state.json`: local read-check timestamps and side comments.
- `docs/INDEX.md`: generated documentation index.
- `docs/TODO.md`: maintained project TODO list.

The scanner does not move or rename existing documents.

## Minimal Config

```toml
include = ["docs/**/*.md"]
exclude = ["docs/INDEX.md", "docs/TODO.md"]
bibliography = ["docs/paper/references.bib"]

[[categories]]
id = "topics"
title = "Topics"
patterns = ["docs/topics/*.md"]

[[categories]]
id = "plans"
title = "Plans"
patterns = ["docs/plans/*.md"]
```
