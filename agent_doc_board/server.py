"""Local HTTP server for the Agent Doc Board UI."""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from agent_doc_board.scanner import build_manifest


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the documentation board for a project root until interrupted."""
    root = root.expanduser().resolve()

    class Handler(BoardRequestHandler):
        """Request handler bound to one project root."""

        project_root = root

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"Serving Agent Doc Board for {root} at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Agent Doc Board")
    finally:
        server.server_close()


class BoardRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler serving HTML, JSON, markdown content, and static assets."""

    project_root: Path

    def do_GET(self) -> None:
        """Handle HTTP GET requests."""
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_text(_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/manifest":
            manifest = _load_or_build_manifest(self.project_root)
            manifest["doc_state"] = _load_doc_state(self.project_root)
            _attach_annotation_counts(self.project_root, manifest)
            self._send_json(manifest)
            return
        if parsed.path == "/api/doc-state":
            self._send_json({"doc_state": _load_doc_state(self.project_root)})
            return
        if parsed.path == "/api/doc":
            self._send_doc(parsed.query)
            return
        if parsed.path == "/styles.css":
            self._send_text(_css(), "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._send_text(_js(), "application/javascript; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            self._send_static_asset(parsed.path)
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        """Handle HTTP POST requests for local document state."""
        parsed = urlparse(self.path)
        if parsed.path == "/api/doc-state":
            self._update_doc_state()
            return
        if parsed.path == "/api/annotations":
            self._update_annotations()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args) -> None:
        """Print compact access logs with the default server prefix."""
        super().log_message(format, *args)

    def _send_doc(self, query: str) -> None:
        """Serve one markdown document from the project manifest."""
        manifest = _load_or_build_manifest(self.project_root)
        requested = parse_qs(query).get("path", [""])[0]
        docs_by_path = {doc["path"]: doc for doc in manifest.get("docs", [])}
        doc = docs_by_path.get(requested)
        if doc is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Document is not in the board manifest")
            return

        path = (self.project_root / requested).resolve()
        if self.project_root not in path.parents or not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Document path is not readable")
            return

        self._send_json(
            {
                "title": doc["title"],
                "path": doc["path"],
                "abs_path": doc["abs_path"],
                "summary": doc["summary"],
                "tags": doc["tags"],
                "date": doc.get("date", ""),
                "citations": doc.get("citations", []),
                "missing_citations": doc.get("missing_citations", []),
                "outgoing_links": doc.get("outgoing_links", []),
                "backlinks": doc.get("backlinks", []),
                "related": doc.get("related", []),
                "topic_timeline": doc.get("topic_timeline", []),
                "state": _load_doc_state(self.project_root).get(doc["path"], {}),
                "annotations": _load_doc_annotations(self.project_root, doc["path"]),
                "content": path.read_text(errors="replace"),
            }
        )

    def _update_doc_state(self) -> None:
        """Persist read status or whole-document side notes for one board document."""
        body = self._read_json_body()
        requested = str(body.get("path", ""))
        manifest = _load_or_build_manifest(self.project_root)
        docs_by_path = {doc["path"]: doc for doc in manifest.get("docs", [])}
        if requested not in docs_by_path:
            self.send_error(HTTPStatus.NOT_FOUND, "Document is not in the board manifest")
            return

        state = _load_doc_state(self.project_root)
        entry = dict(state.get(requested, {}))
        if "read" in body:
            if body.get("read"):
                entry["read_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            else:
                entry.pop("read_at", None)
        if "side_comment" in body:
            entry["side_comment"] = str(body.get("side_comment", ""))[:20000]
        state[requested] = entry
        _write_doc_state(self.project_root, state)
        self._send_json({"path": requested, "state": entry})

    def _update_annotations(self) -> None:
        """Create, update, or delete element-level annotations for one document."""
        body = self._read_json_body()
        requested = str(body.get("path", ""))
        manifest = _load_or_build_manifest(self.project_root)
        docs_by_path = {doc["path"]: doc for doc in manifest.get("docs", [])}
        if requested not in docs_by_path:
            self.send_error(HTTPStatus.NOT_FOUND, "Document is not in the board manifest")
            return

        annotations = _load_doc_annotations(self.project_root, requested)
        action = str(body.get("action", "save"))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if action == "save":
            annotation = _normalize_annotation(body.get("annotation", {}))
            annotation_id = annotation["id"]
            previous = next((item for item in annotations if item.get("id") == annotation_id), None)
            annotation["created_at"] = previous.get("created_at", now) if previous else now
            annotation["updated_at"] = now
            annotations = [item for item in annotations if item.get("id") != annotation_id]
            annotations.append(annotation)
        elif action == "delete":
            annotation_id = str(body.get("id", ""))[:120]
            annotations = [item for item in annotations if item.get("id") != annotation_id]
        else:
            self.send_error(HTTPStatus.BAD_REQUEST, "Unsupported annotation action")
            return

        annotations.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))))
        _write_doc_annotations(self.project_root, requested, annotations)
        self._send_json({"path": requested, "annotations": annotations})

    def _read_json_body(self) -> dict:
        """Read a small JSON request body from the client."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = self.rfile.read(min(length, 100000)).decode("utf-8")
        return json.loads(payload or "{}")

    def _send_text(self, body: str, content_type: str) -> None:
        """Send a UTF-8 text response."""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_static_asset(self, request_path: str) -> None:
        """Serve a packaged static asset from the Agent Doc Board bundle."""
        static_root = Path(__file__).resolve().parent / "static"
        relative = request_path.removeprefix("/static/")
        path = (static_root / relative).resolve()
        if static_root not in path.parents or not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Static asset not found")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, body: dict) -> None:
        """Send a JSON response."""
        self._send_text(json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")


def _load_or_build_manifest(root: Path) -> dict:
    """Load a generated manifest or build one in memory if absent."""
    manifest_path = root / ".agent-docs" / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return build_manifest(root)


def _attach_annotation_counts(root: Path, manifest: dict) -> None:
    """Attach lightweight per-document annotation counts to a manifest payload."""
    for doc in manifest.get("docs", []):
        path = doc.get("path")
        doc["annotation_count"] = len(_load_doc_annotations(root, path)) if path else 0


def _annotation_path(root: Path, doc_path: str) -> Path:
    """Return the sidecar annotation path for a document path."""
    annotations_root = (root / ".agent-docs" / "annotations").resolve()
    relative = Path(*Path(doc_path).parts)
    path = (annotations_root / relative).resolve()
    return path.with_name(path.name + ".json")


def _load_doc_annotations(root: Path, doc_path: str) -> list[dict]:
    """Load element-level annotations for one document."""
    path = _annotation_path(root, doc_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        annotations = payload
    elif isinstance(payload, dict):
        annotations = payload.get("annotations", [])
    else:
        annotations = []
    return [item for item in annotations if isinstance(item, dict)]


def _write_doc_annotations(root: Path, doc_path: str, annotations: list[dict]) -> None:
    """Persist element-level annotations for one document."""
    path = _annotation_path(root, doc_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"path": doc_path, "annotations": annotations}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _normalize_annotation(raw: object) -> dict:
    """Clamp and normalize one annotation payload from the browser."""
    source = raw if isinstance(raw, dict) else {}
    anchor_source = source.get("anchor", {}) if isinstance(source.get("anchor", {}), dict) else {}
    annotation_id = str(source.get("id") or f"ann_{uuid4().hex}")[:120]
    anchor = {
        "type": _trim_text(anchor_source.get("type", "block"), 40),
        "selector": _trim_text(anchor_source.get("selector", ""), 500),
        "kind": _trim_text(anchor_source.get("kind", ""), 40),
        "text_fingerprint": _trim_text(anchor_source.get("text_fingerprint", ""), 500),
    }
    try:
        anchor["block_index"] = int(anchor_source.get("block_index", -1))
    except (TypeError, ValueError):
        anchor["block_index"] = -1
    return {
        "id": annotation_id,
        "anchor": anchor,
        "quote": _trim_text(source.get("quote", ""), 5000),
        "comment": _trim_text(source.get("comment", ""), 20000),
    }


def _trim_text(value: object, limit: int) -> str:
    """Convert a value to text and clamp it to a maximum length."""
    return str(value or "")[:limit]

def _doc_state_path(root: Path) -> Path:
    """Return the local per-document state file path."""
    return root / ".agent-docs" / "doc_state.json"


def _load_doc_state(root: Path) -> dict:
    """Load local per-document read status and side comments."""
    path = _doc_state_path(root)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def _write_doc_state(root: Path, state: dict) -> None:
    """Persist local per-document state in a deterministic JSON file."""
    path = _doc_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _html() -> str:
    """Return the board HTML shell."""
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Agent Doc Board</title>
    <link rel="stylesheet" href="/static/katex/katex.min.css">
    <link rel="stylesheet" href="/styles.css">
  </head>
  <body>
    <header class="site-header">
      <nav class="nav">
        <a class="brand" href="/">Agent Doc Board</a>
        <div class="nav-links">
          <button id="show-list" type="button">Docs</button>
          <button id="focus-todo" type="button">TODO</button>
          <button class="copy-inline" id="copy-root" type="button">Copy Root</button>
        </div>
      </nav>
    </header>
    <main class="shell">
      <section class="intro">
        <div>
          <h1>Project documentation board</h1>
          <p id="project-root"></p>
        </div>
        <div id="stats" class="stats"></div>
      </section>

      <section class="dashboard">
        <aside class="rail left-rail">
          <h2>Views</h2>
          <div id="categories" class="category-list"></div>
        </aside>

        <section class="workspace">
          <div class="toolbar">
            <div class="toolbar-row">
              <input id="search" type="search" placeholder="Search docs, notes, TODOs..." aria-label="Search docs">
              <select id="sort-docs" aria-label="Sort docs">
                <option value="date-desc">Newest first</option>
                <option value="date-asc">Oldest first</option>
                <option value="title-asc">Title A-Z</option>
                <option value="category-asc">Category</option>
              </select>
              <label class="archive-toggle">
                <input id="include-archived" type="checkbox">
                Show archived
              </label>
            </div>
          </div>

          <section id="reader" class="reader" hidden>
            <div class="reader-top">
              <button id="close-reader" type="button">Back to list</button>
              <button class="copy-inline" id="copy-doc-path" type="button">Copy Path</button>
            </div>
            <div class="reader-layout">
              <article id="reader-content" class="markdown"></article>
              <aside id="reader-context" class="reader-context" aria-label="Related documentation"></aside>
            </div>
          </section>

          <section id="list-view">
            <div id="docs" class="doc-list"></div>
          </section>
        </section>

        <aside class="rail right-rail">
          <section class="panel" id="todo-panel">
          <h2>TODO</h2>
          <div id="todos"></div>
          </section>
          <section class="panel">
          <h2>Data</h2>
          <div id="data-refs"></div>
          </section>
        </aside>
      </section>
    </main>
    <div id="toast" class="toast" role="status" aria-live="polite"></div>
    <script src="/static/katex/katex.min.js"></script>
    <script src="/app.js"></script>
  </body>
</html>
"""


def _css() -> str:
    """Return the board stylesheet."""
    return """
:root {
  color-scheme: light;
  --bg: #f1f1f1;
  --paper: #ffffff;
  --text: #242424;
  --muted: #6d6d6d;
  --soft: #9b9b9b;
  --border: #e2e2e2;
  --rule: #d5d5d5;
  --accent: #2f2f2f;
  --accent-soft: #d9d9d9;
  --done: #5f6f5f;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.035);
  --shadow-strong: 0 8px 24px rgba(20, 20, 20, 0.045);
}

* { box-sizing: border-box; }

html {
  width: 100%;
  max-width: 100%;
  overflow-x: hidden;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  overflow-x: hidden;
}

button, input {
  font: inherit;
}

.site-header {
  background: var(--bg);
}

.nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  max-width: 1760px;
  margin: 0 auto;
  padding: 14px 32px;
}

.brand {
  color: var(--text);
  font-size: 24px;
  font-weight: 700;
  font-family: Georgia, "Times New Roman", Times, serif;
  text-decoration: none;
}

.nav-links {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 18px;
}

.nav-links button,
.copy-inline,
.reader-top button {
  border: 0;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  padding: 0;
  font-size: 14px;
}

.shell {
  max-width: 1760px;
  margin: 0 auto;
  padding: 30px 32px 72px;
  width: 100%;
}

.intro {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
  min-width: 0;
}

.intro > div {
  min-width: 0;
}

.intro h1 {
  margin: 0;
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 34px;
  line-height: 1.2;
  letter-spacing: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.intro p {
  max-width: 980px;
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.dashboard {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) minmax(360px, 420px);
  gap: 20px;
  align-items: start;
  min-width: 0;
}

.rail {
  position: sticky;
  top: 18px;
  display: grid;
  gap: 16px;
}

.rail h2,
.panel h2 {
  margin: 0 0 12px;
  color: var(--text);
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 18px;
  line-height: 1.25;
}

.left-rail,
.panel {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--paper);
  box-shadow: var(--shadow);
  padding: 16px;
}

.workspace {
  min-width: 0;
  max-width: 100%;
}

.toolbar {
  display: grid;
  gap: 12px;
  margin-bottom: 14px;
}

.toolbar-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 170px max-content;
  gap: 10px;
  align-items: center;
}

.archive-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}

.archive-toggle input {
  accent-color: var(--text);
}

.category-list {
  display: grid;
  gap: 8px;
}

.tab {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: #fafafa;
  color: var(--text);
  cursor: pointer;
  font-weight: 600;
  padding: 10px 11px;
  text-align: left;
}

.tab.active {
  background: var(--paper);
  border-color: #cfcfcf;
  box-shadow: inset 4px 0 0 var(--rule);
}

.tab .count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  margin-left: 8px;
  border-radius: 50%;
  background: #e1e1e1;
  color: var(--muted);
  font-size: 12px;
  flex: 0 0 auto;
}

input[type="search"],
select {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--text);
  padding: 12px 14px;
  font-size: 14px;
}

select {
  cursor: pointer;
}

.stats {
  min-width: 170px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--paper);
  padding: 12px 14px;
  color: var(--muted);
  font-size: 14px;
  text-align: right;
}

.doc-list {
  display: grid;
  gap: 12px;
}

.post-card {
  display: block;
  width: 100%;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-align: left;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  box-shadow: var(--shadow);
  padding: 16px 18px;
  cursor: pointer;
}

.post-card:hover {
  border-color: #cfcfcf;
}

.post-card h3 {
  margin: 0 0 8px;
  color: var(--text);
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 21px;
  line-height: 1.25;
  letter-spacing: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.summary {
  margin: 0 0 10px;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.meta-line {
  display: block;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.actions {
  display: flex;
  gap: 16px;
  margin-top: 10px;
}

.actions button {
  border: 0;
  background: transparent;
  color: var(--text);
  padding: 0;
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
}

.panel {
  padding: 16px;
}

.todo-item,
.data-item {
  padding: 10px 0;
  border-top: 1px solid #eeeeee;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
}

.todo-item:first-child,
.data-item:first-child {
  border-top: 0;
}

.todo-item strong,
.data-item strong {
  color: var(--text);
}

.todo-summary {
  display: grid;
  gap: 7px;
  margin-bottom: 12px;
  color: var(--muted);
  font-size: 12px;
}

.progress {
  height: 7px;
  overflow: hidden;
  border-radius: 999px;
  background: #ededed;
}

.progress-fill {
  display: block;
  height: 100%;
  width: 0;
  background: var(--accent);
}

.todo-group {
  border-top: 1px solid #eeeeee;
  padding-top: 12px;
  margin-top: 12px;
}

.todo-group:first-of-type {
  border-top: 0;
  padding-top: 0;
  margin-top: 0;
}

.todo-group-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  color: var(--text);
  font-size: 12px;
  font-weight: 800;
  gap: 10px;
}

.todo-card {
  display: grid;
  gap: 8px;
  padding: 10px 0;
  border-top: 1px solid #eeeeee;
}

.todo-card:first-child {
  border-top: 0;
}

.todo-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.todo-priority {
  flex: 0 0 auto;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #f8f8f8;
  color: var(--text);
  padding: 1px 5px;
  font-size: 11px;
  font-weight: 800;
}

.todo-title {
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.4;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.todo-subtasks {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 0 0 0 2px;
  list-style: none;
}

.todo-subtasks li {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.todo-check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 13px;
  height: 13px;
  margin-top: 2px;
  border: 1px solid #c9c9c9;
  border-radius: 3px;
  color: white;
  font-size: 9px;
  line-height: 1;
}

.todo-check.done {
  background: var(--done);
  border-color: var(--done);
}

.todo-links {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.todo-links button {
  border: 0;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  padding: 0;
  font-size: 12px;
  font-weight: 700;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.todo-progress-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  color: var(--muted);
  font-size: 11px;
}

.data-item code {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.reader {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  box-shadow: var(--shadow-strong);
  padding: 18px 24px 34px;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
}

.reader-top {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
  margin-bottom: 20px;
}

.reader-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 26px;
  align-items: start;
}

.reader-context {
  position: sticky;
  top: 18px;
  display: grid;
  gap: 14px;
  min-width: 0;
}

.context-panel {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: #fafafa;
  padding: 13px 14px;
}

.context-panel h2 {
  margin: 0 0 10px;
  color: var(--text);
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 17px;
  line-height: 1.25;
}

.context-list,
.timeline-list {
  display: grid;
  gap: 8px;
}

.context-link,
.timeline-link {
  display: grid;
  gap: 3px;
  width: 100%;
  border: 0;
  border-left: 3px solid transparent;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  padding: 4px 0 4px 8px;
  text-align: left;
}

.context-link:hover,
.timeline-link:hover {
  border-left-color: var(--rule);
}

.timeline-link.current {
  border-left-color: var(--accent);
  cursor: default;
}

.context-title {
  color: var(--text);
  font-size: 13px;
  font-weight: 750;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.context-reason,
.context-date {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.context-empty {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.reference-key {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.reference-title {
  color: var(--text);
  font-weight: 750;
  line-height: 1.35;
}

.reference-meta {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.citation-link {
  display: inline;
  border: 0;
  background: transparent;
  color: #245a8d;
  cursor: pointer;
  font: inherit;
  padding: 0;
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}

.citation-link.missing {
  color: #9b3b34;
  text-decoration-style: dotted;
}

.markdown-references {
  margin-top: 44px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
}

.markdown-references h2 {
  margin-top: 0;
}

.paper-reference-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding-left: 26px;
}

.paper-reference-item {
  padding-left: 4px;
}

.paper-reference-item.is-focused {
  background: #f7f7f7;
  outline: 1px solid var(--border);
  outline-offset: 6px;
}

.paper-reference-title {
  font-weight: 700;
}

.paper-reference-link {
  display: inline-block;
  margin-left: 6px;
  color: #245a8d;
  font-size: 0.92em;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.read-status {
  display: grid;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.state-action {
  width: fit-content;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--text);
  cursor: pointer;
  padding: 6px 9px;
  font-size: 12px;
  font-weight: 750;
}

.side-comment-panel {
  position: fixed;
  top: 18px;
  right: 22px;
  z-index: 50;
  width: min(360px, calc(100vw - 36px));
  max-height: calc(100vh - 36px);
  overflow: auto;
  box-shadow: var(--shadow-strong);
}

.side-comment-panel.is-dragging {
  opacity: 0.98;
  user-select: none;
}

.side-comment-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  cursor: grab;
  touch-action: none;
}

.side-comment-head:active {
  cursor: grabbing;
}

.side-comment-head h2 {
  margin: 0;
}

.comment-reset {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--muted);
  cursor: pointer;
  padding: 4px 7px;
  font-size: 11px;
  font-weight: 750;
}

.comment-reset:hover {
  color: var(--text);
}

.side-comment-box {
  display: grid;
  gap: 8px;
}

.side-comment-box textarea {
  width: 100%;
  min-height: 150px;
  max-height: min(42vh, 320px);
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--text);
  padding: 9px 10px;
  font: inherit;
  font-size: 13px;
  line-height: 1.45;
}

.read-mark {
  display: inline-block;
  margin-left: 6px;
  color: var(--done);
  font-size: 12px;
  font-weight: 800;
}

.markdown {
  color: var(--text);
  font-size: 16px;
  line-height: 1.72;
  max-width: 1180px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.markdown h1,
.markdown h2,
.markdown h3 {
  font-family: Georgia, "Times New Roman", Times, serif;
  line-height: 1.25;
  margin: 28px 0 12px;
}

.markdown h1 { font-size: 30px; }
.markdown h2 { font-size: 23px; }
.markdown h3 { font-size: 19px; }

.markdown p,
.markdown ul,
.markdown ol,
.markdown blockquote,
.markdown pre,
.markdown table {
  margin: 0 0 16px;
}

.markdown code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: #f5f5f5;
  padding: 1px 4px;
  border-radius: 4px;
}

.markdown pre {
  overflow: auto;
  background: #f7f7f7;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 14px;
}

.markdown pre code {
  background: transparent;
  padding: 0;
}

.markdown blockquote {
  border-left: 4px solid var(--rule);
  padding-left: 14px;
  color: var(--muted);
}

.markdown table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.markdown .katex {
  font-size: 1.05em;
}

.markdown .katex-display {
  overflow-x: auto;
  overflow-y: hidden;
  margin: 1.25em 0 1.35em;
  padding: 0.1em 0;
}

.markdown .katex-display > .katex {
  display: inline-block;
  max-width: 100%;
}

.math-source {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.math-error {
  display: inline-block;
  border: 1px solid #d6b6b6;
  border-radius: 4px;
  background: #fff7f7;
  color: #8a2d2d;
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}

.markdown th,
.markdown td {
  border: 1px solid var(--border);
  padding: 7px 9px;
  vertical-align: top;
}

.annotation-target.annotation-focused {
  outline: 2px solid #b5b5b5;
  outline-offset: 3px;
  background: #fbfbfb;
}

.annotation-selection-toolbar {
  position: absolute;
  z-index: 75;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--paper);
  box-shadow: var(--shadow-strong);
  padding: 5px 7px;
  pointer-events: auto;
}

.annotation-selection-toolbar button {
  border: 0;
  border-radius: 999px;
  background: var(--text);
  color: white;
  cursor: pointer;
  padding: 5px 9px;
  font-size: 12px;
  font-weight: 750;
  line-height: 1;
}

.annotation-selection-toolbar span {
  max-width: 260px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.annotation-panel-body {
  display: grid;
  gap: 9px;
}

.annotation-card {
  display: grid;
  gap: 7px;
  padding: 9px 0;
  border-top: 1px solid #eeeeee;
}

.annotation-card:first-child {
  border-top: 0;
  padding-top: 0;
}

.annotation-quote {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.annotation-comment {
  margin: 0;
  color: var(--text);
  font-size: 13px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.annotation-meta {
  color: var(--soft);
  font-size: 11px;
}

.annotation-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.annotation-actions button,
.annotation-inline-action {
  border: 0;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  padding: 0;
  font-size: 12px;
  font-weight: 750;
}

.annotation-composer-backdrop {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: start center;
  padding: 72px 18px 18px;
  background: rgba(20, 20, 20, 0.12);
}

.annotation-composer {
  width: min(560px, calc(100vw - 36px));
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--paper);
  box-shadow: var(--shadow-strong);
  padding: 15px;
}

.annotation-composer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.annotation-composer h2 {
  margin: 0;
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 19px;
}

.annotation-composer textarea {
  width: 100%;
  min-height: 140px;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--text);
  padding: 9px 10px;
  font: inherit;
  font-size: 13px;
  line-height: 1.45;
}

.annotation-composer-buttons {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 10px;
}

.annotation-composer-buttons button {
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--paper);
  color: var(--text);
  cursor: pointer;
  padding: 6px 9px;
  font-size: 12px;
  font-weight: 750;
}

.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  background: var(--text);
  color: white;
  padding: 9px 13px;
  border-radius: 5px;
  font-size: 13px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 160ms ease;
}

.toast.show { opacity: 1; }

@media (max-width: 760px) {
  body {
    width: 100%;
    max-width: 100%;
  }

  .nav,
  .intro,
  .dashboard {
    grid-template-columns: minmax(0, 1fr);
    width: 100%;
    max-width: 100%;
    min-width: 0;
  }

  .nav {
    align-items: flex-start;
    flex-direction: column;
    padding: 14px;
  }

  .shell {
    padding: 22px 14px 48px;
    width: 100vw;
    max-width: 100vw;
    overflow-x: hidden;
  }

  .dashboard > *,
  .rail,
  .left-rail,
  .right-rail,
  .panel,
  .workspace,
  .doc-list,
  .category-list {
    width: 100%;
    max-width: calc(100vw - 28px);
    min-width: 0;
  }

  .intro {
    display: grid;
    gap: 14px;
  }

  .intro h1 {
    font-size: 28px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .stats {
    text-align: left;
    width: 100%;
    max-width: calc(100vw - 28px);
    min-width: 0;
  }

  .rail {
    position: static;
  }

  .toolbar {
    display: grid;
  }

  .toolbar-row {
    grid-template-columns: minmax(0, 1fr);
  }

  input[type="search"] {
    margin-bottom: 0;
  }

  .post-card {
    padding: 15px 16px;
    width: 100%;
    max-width: calc(100vw - 28px);
  }

  .post-card h3 {
    font-size: 19px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .reader {
    padding: 16px 16px 28px;
  }

  .reader-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .reader-context {
    position: static;
  }

  .side-comment-panel {
    position: sticky;
    top: 10px;
    right: auto;
    width: auto;
    max-height: none;
    overflow: visible;
  }

  .side-comment-head {
    cursor: default;
  }

  .comment-reset {
    display: none;
  }

  .annotation-selection-toolbar {
    display: none;
  }

  .annotation-composer-backdrop {
    align-items: start;
    padding-top: 28px;
  }

  .markdown {
    font-size: 15px;
    max-width: 100%;
  }

  .actions {
    flex-wrap: wrap;
    gap: 10px 14px;
  }
}
"""


def _js() -> str:
    """Return the board JavaScript."""
    return r"""
let manifest = null;
let activeCategory = "all";
let activeDoc = null;
let activeDocData = null;
let docState = {};
let activeDocAnnotations = [];
let activeAnnotationAnchors = {};
let pendingSelectionAnnotation = null;
let showArchived = false;
const COMMENT_PANEL_POSITION_KEY = "agent-doc-board:comment-panel-position:v2";

const el = (id) => document.getElementById(id);

async function init() {
  const response = await fetch("/api/manifest");
  manifest = await response.json();
  docState = manifest.doc_state || {};
  el("project-root").textContent = "Local documentation board for this project. Click any note to read it in place.";
  el("copy-root").dataset.copy = manifest.project_root;
  renderCategories();
  renderDataRefs();
  renderTodos();
  renderDocs();
  bindChrome();
  openDocFromHash();
}

function bindChrome() {
  el("search").addEventListener("input", renderDocs);
  el("sort-docs").addEventListener("change", renderDocs);
  el("include-archived").addEventListener("change", (event) => {
    showArchived = event.target.checked;
    renderCategories();
    renderDocs();
  });
  el("show-list").addEventListener("click", closeReader);
  el("close-reader").addEventListener("click", closeReader);
  el("focus-todo").addEventListener("click", () => el("todo-panel").scrollIntoView({ behavior: "smooth" }));
  window.addEventListener("hashchange", openDocFromHash);
  window.addEventListener("resize", () => {
    refreshCommentPanelLayout();
    setupAnnotationLayer();
  });
  window.addEventListener("scroll", () => {
    syncCommentPanelToViewport();
    reflowSelectionAnnotationToolbar();
  }, { passive: true });
  document.addEventListener("selectionchange", handleAnnotationSelectionChange);
  document.addEventListener("keyup", (event) => {
    if (event.key === "Escape") {
      hideSelectionAnnotationToolbar();
    }
  });
  document.addEventListener("mousedown", (event) => {
    if (!event.target.closest("[data-selection-annotation-toolbar]")) {
      hideSelectionAnnotationToolbar();
    }
  });
  document.body.addEventListener("click", (event) => {
    const jump = event.target.closest("[data-jump-doc]");
    if (jump && !jump.classList.contains("current")) {
      event.preventDefault();
      event.stopPropagation();
      location.hash = `doc=${encodeURIComponent(jump.dataset.jumpDoc)}`;
      return;
    }
    const readButton = event.target.closest("[data-mark-read]");
    if (readButton) {
      event.preventDefault();
      event.stopPropagation();
      markDocRead(readButton.dataset.markRead);
      return;
    }
    const saveButton = event.target.closest("[data-save-comment]");
    if (saveButton) {
      event.preventDefault();
      event.stopPropagation();
      saveSideComment(saveButton.dataset.saveComment);
      return;
    }
    const selectionComment = event.target.closest("[data-create-selection-comment]");
    if (selectionComment) {
      event.preventDefault();
      event.stopPropagation();
      openSelectionAnnotationComposer();
      return;
    }
    const annotationJump = event.target.closest("[data-jump-annotation]");
    if (annotationJump) {
      event.preventDefault();
      event.stopPropagation();
      focusAnnotation(annotationJump.dataset.jumpAnnotation);
      return;
    }
    const annotationEdit = event.target.closest("[data-edit-annotation]");
    if (annotationEdit) {
      event.preventDefault();
      event.stopPropagation();
      editAnnotation(annotationEdit.dataset.editAnnotation);
      return;
    }
    const annotationDelete = event.target.closest("[data-delete-annotation]");
    if (annotationDelete) {
      event.preventDefault();
      event.stopPropagation();
      deleteAnnotation(annotationDelete.dataset.deleteAnnotation);
      return;
    }
    const annotationCopy = event.target.closest("[data-copy-annotation]");
    if (annotationCopy) {
      event.preventDefault();
      event.stopPropagation();
      copyAnnotationContext(annotationCopy.dataset.copyAnnotation);
      return;
    }
    const annotationSave = event.target.closest("[data-save-annotation]");
    if (annotationSave) {
      event.preventDefault();
      event.stopPropagation();
      saveAnnotationFromComposer();
      return;
    }
    const annotationClose = event.target.closest("[data-close-annotation-composer]");
    if (annotationClose) {
      event.preventDefault();
      event.stopPropagation();
      closeAnnotationComposer();
      return;
    }
    const resetCommentPanel = event.target.closest("[data-reset-comment-panel]");
    if (resetCommentPanel) {
      event.preventDefault();
      event.stopPropagation();
      resetCommentPanelPosition();
      return;
    }
    const referenceJump = event.target.closest("[data-ref-key]");
    if (referenceJump) {
      event.preventDefault();
      event.stopPropagation();
      focusReference(referenceJump.dataset.refKey);
    }
  });
  bindCopyButtons();
}

function renderCategories() {
  const holder = el("categories");
  const visibleDocs = visibleManifestDocs();
  const total = visibleDocs.length;
  const categoryCounts = visibleDocs.reduce((counts, doc) => {
    counts[doc.category] = (counts[doc.category] || 0) + 1;
    return counts;
  }, {});
  const categories = [
    { id: "all", title: "All", count: total },
    ...manifest.categories
      .map((category) => ({ ...category, count: categoryCounts[category.id] || 0 }))
      .filter((category) => category.count > 0),
  ];
  holder.innerHTML = categories.map((category) => `
    <button class="tab ${category.id === activeCategory ? "active" : ""}" data-category="${escapeAttr(category.id)}" type="button">
      ${escapeHtml(category.title)} <span class="count">${category.count}</span>
    </button>
  `).join("");
  holder.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      activeCategory = button.dataset.category;
      renderCategories();
      renderDocs();
      closeReader();
    });
  });
}

function renderDataRefs() {
  const refs = manifest.data_refs || [];
  el("data-refs").innerHTML = refs.length ? refs.map((ref) => `
    <div class="data-item">
      <strong>${escapeHtml(ref.title)}</strong>
      <div>${escapeHtml(ref.description || "")}</div>
      <code>${escapeHtml(ref.path)}</code>
      <div class="actions">
        <button data-copy="${escapeAttr(ref.abs_path)}" type="button">Copy Path</button>
      </div>
    </div>
  `).join("") : `<p class="summary">No data references configured.</p>`;
  bindCopyButtons();
}

function renderTodos() {
  const todos = manifest.todos || [];
  if (!todos.length) {
    el("todos").innerHTML = `<p class="summary">No TODO entries yet.</p>`;
    return;
  }

  const totalUnits = todos.reduce((sum, todo) => sum + todoTotal(todo), 0);
  const doneUnits = todos.reduce((sum, todo) => sum + todoDone(todo), 0);
  const groups = groupTodosByPriority(todos);
  el("todos").innerHTML = `
    <div class="todo-summary">
      <div>${doneUnits}/${totalUnits} checklist items done</div>
      ${progressBar(doneUnits, totalUnits)}
    </div>
    ${groups.map(renderTodoGroup).join("")}
  `;
  bindCopyButtons();
}

function groupTodosByPriority(todos) {
  const order = ["P0", "P1", "P2", "P3"];
  return order
    .map((priority) => ({ priority, todos: todos.filter((todo) => todo.priority === priority) }))
    .filter((group) => group.todos.length);
}

function renderTodoGroup(group) {
  const done = group.todos.reduce((sum, todo) => sum + todoDone(todo), 0);
  const total = group.todos.reduce((sum, todo) => sum + todoTotal(todo), 0);
  return `
    <section class="todo-group">
      <div class="todo-group-title">
        <span>${escapeHtml(group.priority)}</span>
        <span>${done}/${total}</span>
      </div>
      ${group.todos.map(renderTodoCard).join("")}
    </section>
  `;
}

function renderTodoCard(todo) {
  const done = todoDone(todo);
  const total = todoTotal(todo);
  const subtasks = todo.children || [];
  const links = todo.links || [];
  return `
    <article class="todo-card">
      <div class="todo-head">
        <span class="todo-priority">${escapeHtml(todo.priority || "P?")}</span>
        <span class="todo-title">${renderInline(todo.title || "")}</span>
      </div>
      <div class="todo-progress-row">
        ${progressBar(done, total)}
        <span>${done}/${total}</span>
      </div>
      ${subtasks.length ? `<ul class="todo-subtasks">${subtasks.map(renderSubTodo).join("")}</ul>` : ""}
      ${links.length ? `<div class="todo-links">${links.map((link) => `<button data-copy="${escapeAttr(link)}" type="button">Copy ${escapeHtml(shortPath(link))}</button>`).join("")}</div>` : ""}
    </article>
  `;
}

function renderSubTodo(todo) {
  const done = todo.status === "done" || todo.done === true;
  return `
    <li>
      <span class="todo-check ${done ? "done" : ""}">${done ? "✓" : ""}</span>
      <span>${renderInline(todo.title || "")}</span>
    </li>
  `;
}

function todoTotal(todo) {
  const children = todo.children || [];
  return children.length || 1;
}

function todoDone(todo) {
  const children = todo.children || [];
  if (children.length) {
    return children.filter((child) => child.status === "done" || child.done === true).length;
  }
  return todo.status === "done" || todo.done === true ? 1 : 0;
}

function progressBar(done, total) {
  const percent = total ? Math.round((done / total) * 100) : 0;
  return `<div class="progress" aria-label="${done} of ${total} complete"><span class="progress-fill" style="width: ${percent}%"></span></div>`;
}

function shortPath(path) {
  const text = String(path || "");
  const parts = text.split("/");
  return parts.slice(-1)[0] || text;
}

function renderDocs() {
  const query = el("search").value.trim().toLowerCase();
  const visibleDocs = visibleManifestDocs();
  const docs = visibleDocs.filter((doc) => {
    const categoryOk = activeCategory === "all" || doc.category === activeCategory;
    const haystack = [doc.title, doc.path, doc.summary, doc.category, ...(doc.tags || [])].join(" ").toLowerCase();
    return categoryOk && (!query || haystack.includes(query));
  }).sort(compareDocs);
  const archivedCount = (manifest.docs || []).filter(isArchivedDoc).length;
  const archiveNote = !showArchived && archivedCount ? ` (${archivedCount} archived hidden)` : "";
  el("stats").textContent = `${docs.length} shown / ${visibleDocs.length} docs${archiveNote}`;
  el("docs").innerHTML = docs.map(renderDocCard).join("");
  document.querySelectorAll("[data-open-doc]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const copyButton = event.target.closest("[data-copy]");
      if (copyButton) {
        return;
      }
      location.hash = `doc=${encodeURIComponent(node.dataset.openDoc)}`;
    });
  });
  bindCopyButtons();
}

function visibleManifestDocs() {
  return (manifest.docs || []).filter((doc) => showArchived || !isArchivedDoc(doc));
}

function isArchivedDoc(doc) {
  return doc.status === "archive" || doc.status === "archived";
}

function compareDocs(a, b) {
  const mode = el("sort-docs") ? el("sort-docs").value : "date-desc";
  if (mode === "date-asc") {
    return docDateValue(a) - docDateValue(b) || a.title.localeCompare(b.title);
  }
  if (mode === "title-asc") {
    return a.title.localeCompare(b.title) || docDateValue(b) - docDateValue(a);
  }
  if (mode === "category-asc") {
    return String(a.category || "").localeCompare(String(b.category || "")) || docDateValue(b) - docDateValue(a);
  }
  return docDateValue(b) - docDateValue(a) || a.title.localeCompare(b.title);
}

function docDateValue(doc) {
  const value = doc.date || doc.mtime || "";
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function renderDocCard(doc) {
  const markdownLink = `[${doc.title}](${doc.abs_path})`;
  const tags = (doc.tags || []).map((tag) => `#${tag}`).join(" · ");
  const state = docState[doc.path] || {};
  const relationCount = (doc.related || []).length || (doc.backlinks || []).length || (doc.outgoing_links || []).length;
  const relationText = relationCount ? ` · ${relationCount} related` : "";
  const readText = state.read_at ? ` · read ${formatDateTime(state.read_at)}` : "";
  const noteText = state.side_comment ? " · side note" : "";
  const commentText = doc.annotation_count ? ` · ${doc.annotation_count} comment${doc.annotation_count === 1 ? "" : "s"}` : "";
  const statusText = isArchivedDoc(doc) ? " · archived" : "";
  const referenceText = (doc.citations || []).length ? ` · ${(doc.citations || []).length} refs` : "";
  return `
    <article class="post-card" data-open-doc="${escapeAttr(doc.path)}" role="button" tabindex="0">
      <h3>${escapeHtml(doc.title)}${state.read_at ? `<span class="read-mark">Read</span>` : ""}</h3>
      <p class="summary">${escapeHtml(doc.summary || "No summary extracted yet.")}</p>
      <span class="meta-line">${escapeHtml(formatDate(doc.mtime))} · ${escapeHtml(doc.path)} · ${escapeHtml(tags)}${escapeHtml(statusText)}${escapeHtml(relationText)}${escapeHtml(referenceText)}${escapeHtml(readText)}${escapeHtml(noteText)}${escapeHtml(commentText)}</span>
      <span class="actions">
        <button data-copy="${escapeAttr(doc.abs_path)}" type="button">Copy Path</button>
        <button data-copy="${escapeAttr(markdownLink)}" type="button">Copy Markdown Link</button>
      </span>
    </article>
  `;
}

async function openDocFromHash() {
  if (!location.hash.startsWith("#doc=")) {
    return;
  }
  const path = decodeURIComponent(location.hash.slice("#doc=".length));
  await openDoc(path);
}

async function openDoc(path) {
  activeDoc = path;
  const response = await fetch(`/api/doc?path=${encodeURIComponent(path)}`);
  if (!response.ok) {
    showToast("Could not open document");
    return;
  }
  const doc = await response.json();
  activeDocData = doc;
  activeDocAnnotations = doc.annotations || [];
  docState[doc.path] = doc.state || docState[doc.path] || {};
  el("copy-doc-path").dataset.copy = doc.abs_path;
  el("reader-content").innerHTML = renderMarkdown(doc.content) + renderDocumentReferences(doc);
  el("reader-context").innerHTML = renderDocContext(doc);
  mountCommentPanel();
  bindCommentPanelDrag();
  typesetMath(el("reader-content"));
  el("reader").hidden = false;
  el("list-view").hidden = true;
  setupAnnotationLayer();
  hideSelectionAnnotationToolbar();
  el("reader").scrollIntoView({ behavior: "smooth", block: "start" });
  bindCopyButtons();
}

function closeReader() {
  activeDoc = null;
  activeDocData = null;
  activeDocAnnotations = [];
  activeAnnotationAnchors = {};
  pendingSelectionAnnotation = null;
  hideSelectionAnnotationToolbar();
  closeAnnotationComposer();
  removeFloatingCommentPanel();
  el("reader").hidden = true;
  el("list-view").hidden = false;
  if (location.hash.startsWith("#doc=")) {
    history.pushState("", document.title, window.location.pathname + window.location.search);
  }
}

function renderDocContext(doc) {
  const related = doc.related || [];
  const backlinks = doc.backlinks || [];
  const outgoing = doc.outgoing_links || [];
  const timeline = doc.topic_timeline || [];
  const state = doc.state || docState[doc.path] || {};
  const readLine = state.read_at ? `Read on ${formatDateTime(state.read_at)}` : "Not marked as read yet.";
  return `
    <section class="context-panel">
      <h2>Read Check</h2>
      <div class="read-status">
        <div>${escapeHtml(readLine)}</div>
        <button class="state-action" data-mark-read="${escapeAttr(doc.path)}" type="button">Mark Read Now</button>
      </div>
    </section>
    <section class="context-panel side-comment-panel" data-comment-panel>
      <div class="side-comment-head" data-comment-drag>
        <h2>Side Note</h2>
        <button class="comment-reset" data-reset-comment-panel type="button" title="Reset note panel position">Reset</button>
      </div>
      <div class="side-comment-box">
        <textarea id="side-comment" placeholder="Write a private note for this doc...">${escapeHtml(state.side_comment || "")}</textarea>
        <button class="state-action" data-save-comment="${escapeAttr(doc.path)}" type="button">Save Note</button>
      </div>
    </section>
    ${renderAnnotationPanel(doc)}
    <section class="context-panel">
      <h2>Related</h2>
      ${related.length ? `<div class="context-list">${related.map(renderContextLink).join("")}</div>` : `<p class="context-empty">No related docs found yet. Add markdown links or shared topic tags.</p>`}
    </section>
    <section class="context-panel">
      <h2>Backlinks</h2>
      ${backlinks.length ? `<div class="context-list">${backlinks.map(renderContextLink).join("")}</div>` : `<p class="context-empty">No docs link here yet.</p>`}
    </section>
    <section class="context-panel">
      <h2>Forward Links</h2>
      ${outgoing.length ? `<div class="context-list">${outgoing.map(renderContextLink).join("")}</div>` : `<p class="context-empty">This doc has no board-local markdown links.</p>`}
    </section>
    <section class="context-panel">
      <h2>Topic Timeline</h2>
      ${timeline.length ? `<div class="timeline-list">${timeline.map(renderTimelineLink).join("")}</div>` : `<p class="context-empty">No timeline entries for this topic yet.</p>`}
    </section>
  `;
}

function renderDocumentReferences(doc) {
  // Append a paper-style reference list to the bottom of the rendered document.
  const citations = doc.citations || [];
  if (!citations.length) return "";
  const refs = manifest.references || {};
  return `
    <section class="markdown-references" aria-label="References">
      <h2>References</h2>
      <ol class="paper-reference-list">
        ${citations.map((key) => renderPaperReferenceItem(key, refs[key])).join("")}
      </ol>
    </section>
  `;
}

function renderPaperReferenceItem(key, ref) {
  // Render one paper-style bibliography item in citation order.
  if (!ref) {
    return `
      <li class="paper-reference-item missing" id="ref-${escapeAttr(key)}" data-reference-entry="${escapeAttr(key)}">
        <span class="reference-key">@${escapeHtml(key)}</span>. Missing BibTeX entry.
      </li>
    `;
  }
  const author = ref.author || "Unknown author";
  const year = ref.year ? ` (${ref.year}).` : ".";
  const venue = ref.venue ? ` ${ref.venue}.` : "";
  const link = referenceUrl(ref);
  const linkLabel = referenceLinkLabel(ref, link);
  return `
    <li class="paper-reference-item" id="ref-${escapeAttr(key)}" data-reference-entry="${escapeAttr(key)}">
      ${escapeHtml(author)}${escapeHtml(year)}
      <span class="paper-reference-title">${escapeHtml(ref.title || key)}.</span>${escapeHtml(venue)}
      <span class="reference-key">@${escapeHtml(key)}</span>
      ${link ? `<a class="paper-reference-link" href="${escapeAttr(link)}" target="_blank" rel="noreferrer">${escapeHtml(linkLabel)}</a>` : ""}
    </li>
  `;
}

function referenceMeta(ref) {
  // Format a compact author-year-venue line.
  return [ref.author || "", ref.year || "", ref.venue || ""].filter(Boolean).join(". ");
}

function referenceUrl(ref) {
  // Prefer explicit URLs, then DOI, then arXiv eprint links.
  if (ref.url) return ref.url;
  if (ref.doi) return `https://doi.org/${ref.doi}`;
  if ((ref.archive_prefix || "").toLowerCase() === "arxiv" && ref.eprint) return `https://arxiv.org/abs/${ref.eprint}`;
  return "";
}

function referenceLinkLabel(ref, link) {
  // Show paper-friendly external-link labels.
  if ((ref.archive_prefix || "").toLowerCase() === "arxiv" && ref.eprint) return `arXiv:${ref.eprint}`;
  if (ref.doi) return "doi";
  if (link) return "link";
  return "";
}

function focusReference(key) {
  // Scroll from an in-text citation to the document-bottom reference entry when possible.
  const target =
    document.querySelector(`.markdown-references [data-reference-entry="${cssEscape(key)}"]`);
  if (!target) {
    showToast("Reference not found");
    return;
  }
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  document.querySelectorAll(".paper-reference-item.is-focused").forEach((node) => node.classList.remove("is-focused"));
  target.classList.add("is-focused");
  setTimeout(() => target.classList.remove("is-focused"), 1800);
}

async function markDocRead(path) {
  // Persist a read timestamp for the active document.
  const state = await saveDocState(path, { read: true });
  updateDocState(path, state);
  showToast("Marked read");
}

async function saveSideComment(path) {
  // Persist the side comment textarea for the active document.
  const area = el("side-comment");
  const state = await saveDocState(path, { side_comment: area ? area.value : "" });
  updateDocState(path, state);
  showToast("Note saved");
}

async function saveDocState(path, patch) {
  // Send a document-state patch to the local board server.
  const response = await fetch("/api/doc-state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, ...patch }),
  });
  if (!response.ok) {
    showToast("Could not save doc state");
    throw new Error("Could not save doc state");
  }
  const payload = await response.json();
  return payload.state || {};
}

function updateDocState(path, state) {
  // Update in-memory state and re-render affected board regions.
  docState[path] = state;
  if (manifest) manifest.doc_state = docState;
  if (activeDocData && activeDocData.path === path) {
    activeDocData.state = state;
    el("reader-context").innerHTML = renderDocContext(activeDocData);
    mountCommentPanel();
    bindCommentPanelDrag();
  }
  renderDocs();
}


function mountCommentPanel() {
  // Move the note editor out of the reader grid so fixed positioning is relative to the viewport.
  const panel = el("reader-context").querySelector("[data-comment-panel]");
  removeFloatingCommentPanel();
  if (!panel || isNarrowViewport()) {
    return;
  }
  document.body.appendChild(panel);
}

function removeFloatingCommentPanel() {
  Array.from(document.body.children)
    .filter((node) => node.matches && node.matches("[data-comment-panel]"))
    .forEach((node) => node.remove());
}

function refreshCommentPanelLayout() {
  // Reconcile body-mounted and in-flow layouts when the viewport crosses the mobile breakpoint.
  if (!activeDocData) {
    return;
  }
  el("reader-context").innerHTML = renderDocContext(activeDocData);
  mountCommentPanel();
  bindCommentPanelDrag();
}

function bindCommentPanelDrag() {
  // Keep the side-comment panel visible while allowing users to place it where it is least intrusive.
  const panel = document.querySelector("[data-comment-panel]");
  const handle = document.querySelector("[data-comment-drag]");
  restoreCommentPanelPosition();
  if (!panel || !handle || panel.dataset.dragBound === "true") {
    return;
  }
  panel.dataset.dragBound = "true";
  handle.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button, textarea, input, a")) {
      return;
    }
    if (isNarrowViewport()) {
      return;
    }
    event.preventDefault();
    const rect = panel.getBoundingClientRect();
    const offsetX = event.clientX - rect.left;
    const offsetY = event.clientY - rect.top;
    panel.classList.add("is-dragging");

    const movePanel = (moveEvent) => {
      const position = clampCommentPanelPosition(panel, moveEvent.clientX - offsetX, moveEvent.clientY - offsetY);
      placeCommentPanelAtViewportPosition(panel, position.left, position.top);
    };

    const stopDrag = () => {
      panel.classList.remove("is-dragging");
      saveCommentPanelPosition(panel);
      window.removeEventListener("pointermove", movePanel);
      window.removeEventListener("pointerup", stopDrag);
      window.removeEventListener("pointercancel", stopDrag);
    };

    window.addEventListener("pointermove", movePanel);
    window.addEventListener("pointerup", stopDrag);
    window.addEventListener("pointercancel", stopDrag);
  });
}

function restoreCommentPanelPosition() {
  // Reapply the saved floating-panel position and clamp it to the current viewport.
  const panel = document.querySelector("[data-comment-panel]");
  if (!panel) {
    return;
  }
  if (isNarrowViewport()) {
    panel.style.removeProperty("left");
    panel.style.removeProperty("top");
    panel.style.removeProperty("right");
    return;
  }
  const position = loadCommentPanelPosition();
  if (!position) {
    applyDefaultCommentPanelPosition(panel);
    return;
  }
  const clamped = clampCommentPanelPosition(panel, position.left, position.top);
  placeCommentPanelAtViewportPosition(panel, clamped.left, clamped.top);
}

function resetCommentPanelPosition() {
  // Return the floating comment panel to its default top-right placement.
  localStorage.removeItem(COMMENT_PANEL_POSITION_KEY);
  const panel = document.querySelector("[data-comment-panel]");
  if (panel) {
    applyDefaultCommentPanelPosition(panel);
  }
  showToast("Note panel reset");
}

function applyDefaultCommentPanelPosition(panel) {
  // Place the panel near the viewport top-right, independent of page scroll offset.
  const rect = panel.getBoundingClientRect();
  const width = rect.width || 360;
  const left = Math.max(8, window.innerWidth - width - 22);
  placeCommentPanelAtViewportPosition(panel, left, 18);
}

function placeCommentPanelAtViewportPosition(panel, left, top) {
  // Fixed positioning pins the panel to the viewport while preserving draggable coordinates.
  panel.dataset.viewportLeft = String(Math.round(left));
  panel.dataset.viewportTop = String(Math.round(top));
  panel.style.setProperty("position", "fixed", "important");
  panel.style.setProperty("left", `${Math.round(left)}px`, "important");
  panel.style.setProperty("top", `${Math.round(top)}px`, "important");
  panel.style.setProperty("right", "auto", "important");
}

function syncCommentPanelToViewport() {
  const panel = document.querySelector("[data-comment-panel]");
  if (!panel || isNarrowViewport() || panel.classList.contains("is-dragging")) {
    return;
  }
  const rect = panel.getBoundingClientRect();
  const left = Number(panel.dataset.viewportLeft || rect.left || 0);
  const top = Number(panel.dataset.viewportTop || rect.top || 18);
  const clamped = clampCommentPanelPosition(panel, left, top);
  placeCommentPanelAtViewportPosition(panel, clamped.left, clamped.top);
}

function loadCommentPanelPosition() {
  try {
    const saved = JSON.parse(localStorage.getItem(COMMENT_PANEL_POSITION_KEY) || "null");
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      return saved;
    }
  } catch (_) {
    localStorage.removeItem(COMMENT_PANEL_POSITION_KEY);
  }
  return null;
}

function saveCommentPanelPosition(panel) {
  const rect = panel.getBoundingClientRect();
  localStorage.setItem(COMMENT_PANEL_POSITION_KEY, JSON.stringify({
    left: Math.round(Number(panel.dataset.viewportLeft || rect.left)),
    top: Math.round(Number(panel.dataset.viewportTop || rect.top)),
  }));
}

function clampCommentPanelPosition(panel, left, top) {
  const rect = panel.getBoundingClientRect();
  const margin = 8;
  const width = rect.width || 360;
  const height = Math.min(rect.height || 240, window.innerHeight - margin * 2);
  const maxLeft = Math.max(margin, window.innerWidth - width - margin);
  const maxTop = Math.max(margin, window.innerHeight - height - margin);
  return {
    left: Math.min(Math.max(left, margin), maxLeft),
    top: Math.min(Math.max(top, margin), maxTop),
  };
}

function isNarrowViewport() {
  return window.matchMedia("(max-width: 760px)").matches;
}


function renderAnnotationPanel(doc) {
  // Render the per-element comments panel for the active document.
  const count = activeDocAnnotations.length;
  return `
    <section class="context-panel" data-annotation-panel>
      <h2>Comments${count ? ` (${count})` : ""}</h2>
      <div class="annotation-panel-body">${renderAnnotationPanelBody()}</div>
    </section>
  `;
}

function renderAnnotationPanelBody() {
  // Render the active document annotations as jumpable cards.
  if (!activeDocAnnotations.length) {
    return `<p class="context-empty">No element comments yet. Highlight text in the document and choose Add comment.</p>`;
  }
  return activeDocAnnotations.map(renderAnnotationCard).join("");
}

function renderAnnotationCard(annotation) {
  // Render one saved annotation with local edit and delete controls.
  const quote = annotation.quote || annotation.anchor?.text_fingerprint || annotation.anchor?.kind || "Selected element";
  return `
    <article class="annotation-card" data-annotation-card="${escapeAttr(annotation.id)}">
      <p class="annotation-quote">${escapeHtml(shortenText(quote, 180))}</p>
      <p class="annotation-comment">${escapeHtml(annotation.comment || "")}</p>
      <div class="annotation-meta">${escapeHtml(annotation.anchor?.kind || "block")} · ${escapeHtml(formatDateTime(annotation.updated_at || annotation.created_at || ""))}</div>
      <div class="annotation-actions">
        <button data-jump-annotation="${escapeAttr(annotation.id)}" type="button">Jump</button>
        <button data-edit-annotation="${escapeAttr(annotation.id)}" type="button">Edit</button>
        <button data-copy-annotation="${escapeAttr(annotation.id)}" type="button">Copy Context</button>
        <button data-delete-annotation="${escapeAttr(annotation.id)}" type="button">Delete</button>
      </div>
    </article>
  `;
}

function setupAnnotationLayer() {
  // Discover rendered markdown elements so selected text can bind to stable block anchors.
  hideSelectionAnnotationToolbar();
  activeAnnotationAnchors = {};
  if (!activeDocData || isNarrowViewport()) {
    refreshAnnotationPanel();
    return;
  }
  const root = el("reader-content");
  const targets = collectAnnotationTargets(root);
  targets.forEach((target, index) => {
    const anchor = buildAnnotationAnchor(root, target, index);
    target.dataset.annotationAnchorId = anchor.id;
    target.classList.add("annotation-target");
    activeAnnotationAnchors[anchor.id] = { anchor, target };
  });
  refreshAnnotationPanel();
}

function collectAnnotationTargets(root) {
  // Collect block-like rendered elements, including non-text blocks such as tables, code, math, and images.
  const selector = [
    "h1", "h2", "h3", "p", "li", "blockquote", "pre", "table", "img", ".katex-display"
  ].join(",");
  return Array.from(root.querySelectorAll(selector)).filter((node) => {
    if (node.closest("[data-annotation-panel], [data-comment-panel]")) return false;
    const rect = node.getBoundingClientRect();
    if (!rect.width && !rect.height) return false;
    const text = annotationElementText(node);
    return text || node.tagName.toLowerCase() === "img" || node.tagName.toLowerCase() === "table";
  });
}

function buildAnnotationAnchor(root, target, index) {
  // Build a stable-enough anchor from the target's rendered path and text fingerprint.
  const kind = target.tagName.toLowerCase();
  const selector = cssPathFromRoot(root, target);
  const text = annotationElementText(target);
  const fingerprint = makeTextFingerprint(text || target.getAttribute("alt") || target.getAttribute("src") || kind);
  return {
    id: `${index}:${kind}:${fingerprint.slice(0, 28)}`,
    type: "block",
    selector,
    kind,
    block_index: index,
    text_fingerprint: fingerprint,
  };
}

function cssPathFromRoot(root, node) {
  // Generate a compact CSS path relative to the markdown root.
  const parts = [];
  let current = node;
  while (current && current !== root) {
    const parent = current.parentElement;
    if (!parent) break;
    const tag = current.tagName.toLowerCase();
    const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
    const index = siblings.indexOf(current) + 1;
    parts.unshift(`${tag}:nth-of-type(${index})`);
    current = parent;
  }
  return parts.join(" > ");
}

function annotationElementText(node) {
  // Produce a readable text snapshot for any commentable element.
  if (!node) return "";
  if (node.tagName && node.tagName.toLowerCase() === "img") {
    return node.getAttribute("alt") || node.getAttribute("src") || "image";
  }
  return normalizeWhitespace(node.textContent || "");
}

function makeTextFingerprint(value) {
  // Normalize text for anchor matching across small rendering changes.
  return normalizeWhitespace(value).toLowerCase().slice(0, 240);
}

function annotationsForAnchor(anchor) {
  // Return comments attached to the current rendered anchor.
  return activeDocAnnotations.filter((annotation) => annotationMatchesAnchor(annotation, anchor));
}

function annotationMatchesAnchor(annotation, anchor) {
  // Match by selector first, with fingerprint and block index as fallback.
  const saved = annotation.anchor || {};
  if (saved.selector && saved.selector === anchor.selector) return true;
  if (saved.block_index === anchor.block_index && saved.kind === anchor.kind) return true;
  return Boolean(saved.text_fingerprint && saved.text_fingerprint === anchor.text_fingerprint);
}

function openAnnotationComposer(anchorId, annotation = null, quoteOverride = "") {
  // Open the comment composer for a rendered anchor or an existing annotation.
  const entry = activeAnnotationAnchors[anchorId];
  if (!entry && !annotation) {
    showToast("Could not find annotation target");
    return;
  }
  const anchor = annotation ? annotation.anchor : entry.anchor;
  const quote = annotation ? annotation.quote : quoteOverride || selectedTextWithin(entry.target) || annotationElementText(entry.target);
  closeAnnotationComposer();
  const backdrop = document.createElement("div");
  backdrop.className = "annotation-composer-backdrop";
  backdrop.dataset.annotationComposer = "true";
  backdrop.innerHTML = `
    <section class="annotation-composer">
      <div class="annotation-composer-head">
        <h2>${annotation ? "Edit Comment" : "Add Comment"}</h2>
        <button class="annotation-inline-action" data-close-annotation-composer type="button">Close</button>
      </div>
      <p class="annotation-quote">${escapeHtml(shortenText(quote || anchor.text_fingerprint || anchor.kind, 300))}</p>
      <textarea id="annotation-comment-input" placeholder="Write a comment bound to this part of the doc...">${escapeHtml(annotation ? annotation.comment || "" : "")}</textarea>
      <div class="annotation-composer-buttons">
        <button data-close-annotation-composer type="button">Cancel</button>
        <button data-save-annotation type="button">Save Comment</button>
      </div>
    </section>
  `;
  backdrop.dataset.annotationId = annotation ? annotation.id : makeAnnotationId();
  backdrop.dataset.annotationAnchor = JSON.stringify(anchor);
  backdrop.dataset.annotationQuote = quote || anchor.text_fingerprint || "";
  document.body.appendChild(backdrop);
  const input = el("annotation-comment-input");
  if (input) input.focus();
}

function editAnnotation(annotationId) {
  // Open an existing annotation in the composer.
  const annotation = activeDocAnnotations.find((item) => item.id === annotationId);
  if (!annotation) {
    showToast("Comment not found");
    return;
  }
  const anchorId = findAnchorIdForAnnotation(annotation);
  openAnnotationComposer(anchorId, annotation);
}

async function saveAnnotationFromComposer() {
  // Persist the currently open annotation composer.
  const composer = document.querySelector("[data-annotation-composer]");
  const input = el("annotation-comment-input");
  if (!composer || !input || !activeDocData) return;
  const comment = input.value.trim();
  if (!comment) {
    showToast("Write a comment first");
    return;
  }
  const annotation = {
    id: composer.dataset.annotationId,
    anchor: JSON.parse(composer.dataset.annotationAnchor || "{}"),
    quote: composer.dataset.annotationQuote || "",
    comment,
  };
  await persistAnnotation({ action: "save", annotation });
  closeAnnotationComposer();
  showToast("Comment saved");
}

async function deleteAnnotation(annotationId) {
  // Delete a saved annotation after a browser-level confirmation.
  const annotation = activeDocAnnotations.find((item) => item.id === annotationId);
  if (!annotation) return;
  if (!window.confirm("Delete this comment?")) return;
  await persistAnnotation({ action: "delete", id: annotationId });
  showToast("Comment deleted");
}

async function persistAnnotation(payload) {
  // Send an annotation mutation to the local board server and refresh local UI state.
  const response = await fetch("/api/annotations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: activeDocData.path, ...payload }),
  });
  if (!response.ok) {
    showToast("Could not save comment");
    throw new Error("Could not save annotation");
  }
  const result = await response.json();
  activeDocAnnotations = result.annotations || [];
  activeDocData.annotations = activeDocAnnotations;
  refreshAnnotationPanel();
}

function refreshAnnotationPanel() {
  // Refresh the document comments panel without disturbing the floating Side Note editor.
  const panel = document.querySelector("[data-annotation-panel] .annotation-panel-body");
  if (panel) panel.innerHTML = renderAnnotationPanelBody();
  const heading = document.querySelector("[data-annotation-panel] h2");
  if (heading) heading.textContent = `Comments${activeDocAnnotations.length ? ` (${activeDocAnnotations.length})` : ""}`;
}

function closeAnnotationComposer() {
  // Close any open annotation composer dialog.
  document.querySelectorAll("[data-annotation-composer]").forEach((node) => node.remove());
}

function focusAnnotation(annotationId) {
  // Scroll to and highlight the element associated with an annotation.
  const annotation = activeDocAnnotations.find((item) => item.id === annotationId);
  if (!annotation) return;
  const anchorId = findAnchorIdForAnnotation(annotation);
  const entry = activeAnnotationAnchors[anchorId];
  if (!entry) {
    showToast("Anchor not found in current render");
    return;
  }
  entry.target.scrollIntoView({ behavior: "smooth", block: "center" });
  document.querySelectorAll(".annotation-focused").forEach((node) => node.classList.remove("annotation-focused"));
  entry.target.classList.add("annotation-focused");
  setTimeout(() => entry.target.classList.remove("annotation-focused"), 1800);
}

function findAnchorIdForAnnotation(annotation) {
  // Find the current rendered anchor id for a saved annotation.
  const match = Object.values(activeAnnotationAnchors).find(({ anchor }) => annotationMatchesAnchor(annotation, anchor));
  return match ? match.anchor.id : "";
}

async function copyAnnotationContext(annotationId) {
  // Copy a compact annotation packet for discussion with an agent.
  const annotation = activeDocAnnotations.find((item) => item.id === annotationId);
  if (!annotation || !activeDocData) return;
  const text = [
    `Doc: ${activeDocData.path}`,
    `Anchor: ${annotation.anchor?.kind || "block"} ${annotation.anchor?.selector || ""}`.trim(),
    `Quote: ${annotation.quote || ""}`,
    `Comment: ${annotation.comment || ""}`,
  ].join("\n");
  await copyText(text);
  showToast("Comment context copied");
}

function handleAnnotationSelectionChange() {
  // Show a small Add comment action when text is selected inside the active document.
  window.clearTimeout(handleAnnotationSelectionChange.timer);
  handleAnnotationSelectionChange.timer = window.setTimeout(showSelectionAnnotationToolbar, 80);
}

function showSelectionAnnotationToolbar() {
  // Position the selection toolbar near a valid text selection inside a commentable block.
  if (!activeDocData || isNarrowViewport()) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const selection = window.getSelection ? window.getSelection() : null;
  if (!selection || selection.isCollapsed || !selection.rangeCount) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const root = el("reader-content");
  const range = selection.getRangeAt(0);
  if (!root || !root.contains(range.commonAncestorContainer)) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const quote = normalizeWhitespace(selection.toString()).slice(0, 1000);
  if (!quote) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const target = annotationTargetForRange(range);
  if (!target) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const anchorId = target.dataset.annotationAnchorId;
  if (!anchorId || !activeAnnotationAnchors[anchorId]) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const rect = firstVisibleSelectionRect(range);
  if (!rect) {
    hideSelectionAnnotationToolbar();
    return;
  }
  pendingSelectionAnnotation = { anchorId, quote };
  let toolbar = document.querySelector("[data-selection-annotation-toolbar]");
  if (!toolbar) {
    toolbar = document.createElement("div");
    toolbar.className = "annotation-selection-toolbar";
    toolbar.dataset.selectionAnnotationToolbar = "true";
    toolbar.innerHTML = `
      <button data-create-selection-comment type="button">Add comment</button>
      <span data-selection-annotation-preview></span>
    `;
    document.body.appendChild(toolbar);
  }
  const preview = toolbar.querySelector("[data-selection-annotation-preview]");
  if (preview) preview.textContent = shortenText(quote, 80);
  placeSelectionAnnotationToolbar(toolbar, rect);
}

function annotationTargetForRange(range) {
  // Find the closest discovered annotation block for the selection start.
  const startNode = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
  const commonNode = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
  const startTarget = startNode ? startNode.closest(".annotation-target") : null;
  if (startTarget) return startTarget;
  return commonNode ? commonNode.closest(".annotation-target") : null;
}

function firstVisibleSelectionRect(range) {
  // Prefer the first non-empty client rect so multi-line selections place the toolbar near the selected text.
  const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
  return rects[0] || range.getBoundingClientRect();
}

function placeSelectionAnnotationToolbar(toolbar, rect) {
  // Place the toolbar above the selection when possible, otherwise below it.
  const margin = 8;
  const toolbarRect = toolbar.getBoundingClientRect();
  const width = toolbarRect.width || 180;
  const height = toolbarRect.height || 34;
  const left = Math.min(Math.max(window.scrollX + rect.left, window.scrollX + margin), window.scrollX + window.innerWidth - width - margin);
  const above = window.scrollY + rect.top - height - 8;
  const below = window.scrollY + rect.bottom + 8;
  const top = above > window.scrollY + margin ? above : below;
  toolbar.style.left = `${Math.round(left)}px`;
  toolbar.style.top = `${Math.round(top)}px`;
}

function reflowSelectionAnnotationToolbar() {
  // Keep the toolbar aligned while scrolling if the browser selection remains active.
  const toolbar = document.querySelector("[data-selection-annotation-toolbar]");
  if (!toolbar || !pendingSelectionAnnotation) return;
  const selection = window.getSelection ? window.getSelection() : null;
  if (!selection || selection.isCollapsed || !selection.rangeCount) {
    hideSelectionAnnotationToolbar();
    return;
  }
  const rect = firstVisibleSelectionRect(selection.getRangeAt(0));
  if (rect) placeSelectionAnnotationToolbar(toolbar, rect);
}

function hideSelectionAnnotationToolbar() {
  // Remove the selection toolbar while preserving saved annotations.
  document.querySelectorAll("[data-selection-annotation-toolbar]").forEach((node) => node.remove());
}

function openSelectionAnnotationComposer() {
  // Convert the current selection into an element-bound comment with a selected-text quote.
  if (!pendingSelectionAnnotation) {
    showToast("Select text inside the doc first");
    return;
  }
  const { anchorId, quote } = pendingSelectionAnnotation;
  hideSelectionAnnotationToolbar();
  openAnnotationComposer(anchorId, null, quote);
}

function selectedTextWithin(target) {
  // Return the active text selection only when it belongs to the target element.
  const selection = window.getSelection ? window.getSelection() : null;
  if (!selection || selection.isCollapsed || !selection.rangeCount) return "";
  const range = selection.getRangeAt(0);
  if (!target.contains(range.commonAncestorContainer)) return "";
  return normalizeWhitespace(selection.toString()).slice(0, 1000);
}

function makeAnnotationId() {
  // Create a compact browser-side id; the server preserves or replaces it as needed.
  return `ann_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeWhitespace(value) {
  // Collapse whitespace for compact quotes and fingerprints.
  return String(value || "").replace(/\s+/g, " ").trim();
}

function shortenText(value, limit) {
  // Shorten display text without changing the stored annotation payload.
  const text = normalizeWhitespace(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1))}…`;
}

function renderContextLink(item) {
  return `
    <button class="context-link" data-jump-doc="${escapeAttr(item.path)}" type="button">
      <span class="context-title">${escapeHtml(item.title)}</span>
      <span class="context-reason">${escapeHtml(item.reason || item.category || "related")}</span>
    </button>
  `;
}

function renderTimelineLink(item) {
  return `
    <button class="timeline-link ${item.current ? "current" : ""}" data-jump-doc="${escapeAttr(item.path)}" type="button">
      <span class="context-date">${escapeHtml(item.date || "undated")}</span>
      <span class="context-title">${escapeHtml(item.title)}</span>
      <span class="context-reason">${escapeHtml(item.reason || "topic")}</span>
    </button>
  `;
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.trim().startsWith("$$")) {
      const mathLines = [];
      let current = line.trim();
      const sameLine = current.slice(2).trim();
      if (sameLine.endsWith("$$")) {
        html.push(renderMath(sameLine.slice(0, -2), true));
        index += 1;
        continue;
      }
      if (sameLine.length) {
        mathLines.push(sameLine);
      }
      index += 1;
      while (index < lines.length && !lines[index].trim().endsWith("$$")) {
        mathLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        const endLine = lines[index].trim();
        mathLines.push(endLine.slice(0, -2));
        index += 1;
      }
      html.push(renderMath(mathLines.join("\n"), true));
      continue;
    }
    if (line.trim().startsWith("\\[")) {
      const sameLine = line.trim().slice(2).trim();
      if (sameLine.endsWith("\\]")) {
        html.push(renderMath(sameLine.slice(0, -2), true));
        index += 1;
        continue;
      }
      const mathLines = sameLine ? [sameLine] : [];
      index += 1;
      while (index < lines.length && !lines[index].trim().endsWith("\\]")) {
        mathLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        const endLine = lines[index].trim();
        mathLines.push(endLine.slice(0, -2));
        index += 1;
      }
      html.push(renderMath(mathLines.join("\n"), true));
      continue;
    }
    if (line.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\|.+\|$/.test(line) && index + 1 < lines.length && /^\|[\s:|\-]+\|$/.test(lines[index + 1])) {
      const rows = [line];
      index += 2;
      while (index < lines.length && /^\|.+\|$/.test(lines[index])) {
        rows.push(lines[index]);
        index += 1;
      }
      html.push(renderTable(rows));
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (line.startsWith("> ")) {
      html.push(`<blockquote>${renderInline(line.slice(2))}</blockquote>`);
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(`<li>${renderInline(lines[index].replace(/^\s*[-*]\s+/, ""))}</li>`);
        index += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{1,3})\s+/.test(lines[index]) && !lines[index].startsWith("```")) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    html.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
  }
  return html.join("\n");
}

function renderTable(rows) {
  const cells = rows.map((row) => row.split("|").slice(1, -1).map((cell) => cell.trim()));
  const head = cells[0] || [];
  const body = cells.slice(1);
  return `<table><thead><tr>${head.map((cell) => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead><tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderInline(value) {
  const segments = splitMathSegments(String(value));
  return segments.map((segment) => {
    if (segment.kind === "math") {
      return renderMath(segment.text, segment.display);
    }
    return renderPlainInline(segment.text);
  }).join("");
}

function renderPlainInline(value) {
  return escapeHtml(value)
    .replace(/\[((?:[^\]]*@[\w:-]+[^\]]*)+)\]/g, renderCitationGroup)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, href) => {
      const target = normalizeDocHref(href);
      if (target) {
        return `<a href="#doc=${encodeURIComponent(target)}">${label}</a>`;
      }
      return `<a href="${escapeAttr(href)}" target="_blank" rel="noreferrer">${label}</a>`;
    });
}

function renderCitationGroup(match, inner) {
  // Convert Pandoc-style citation groups such as [@a; @b] into clickable labels.
  const keys = [];
  String(inner).replace(/@([A-Za-z][A-Za-z0-9_:-]+)/g, (full, key) => {
    if (!keys.includes(key)) keys.push(key);
    return full;
  });
  if (!keys.length) return match;
  return `[${keys.map(renderCitationLink).join("; ")}]`;
}

function renderCitationLink(key) {
  // Render one in-text citation from the loaded BibTeX manifest.
  const ref = (manifest.references || {})[key];
  const label = ref ? citationLabel(ref, key) : `@${key}`;
  const title = ref ? referenceMeta(ref) : "Missing BibTeX entry";
  return `<button class="citation-link ${ref ? "" : "missing"}" data-ref-key="${escapeAttr(key)}" type="button" title="${escapeAttr(title)}">${escapeHtml(label)}</button>`;
}

function citationLabel(ref, key) {
  // Use a compact author-year label, falling back to the BibTeX key.
  const author = citationAuthor(ref.author || "");
  const year = ref.year || "";
  if (author && year) return `${author} ${year}`;
  if (author) return author;
  if (year) return `${key} ${year}`;
  return key;
}

function citationAuthor(authorField) {
  // Extract a short author label from a BibTeX author field.
  const authors = String(authorField || "").split(/\s+and\s+/i).map((item) => item.trim()).filter(Boolean);
  if (!authors.length) return "";
  const lastNames = authors.map((author) => {
    if (author.includes(",")) return author.split(",", 1)[0].trim();
    const parts = author.split(/\s+/).filter(Boolean);
    return parts[parts.length - 1] || author;
  });
  if (lastNames.length === 1) return lastNames[0];
  if (lastNames.length === 2) return `${lastNames[0]} & ${lastNames[1]}`;
  return `${lastNames[0]} et al.`;
}

function normalizeDocHref(href) {
  if (!manifest || !href) return null;
  const docPaths = new Set((manifest.docs || []).map((doc) => doc.path));
  let clean = String(href).replace(/&amp;/g, "&").split("#")[0].split("?")[0].trim();
  if (!clean || clean.startsWith("http://") || clean.startsWith("https://") || clean.startsWith("mailto:")) return null;
  if (clean.startsWith("/")) {
    const marker = "/docs/";
    clean = clean.includes(marker) ? `docs/${clean.split(marker)[1]}` : clean.slice(1);
  } else if (activeDoc && !clean.startsWith("docs/")) {
    const base = activeDoc.split("/").slice(0, -1);
    clean = [...base, clean].join("/");
  }
  const parts = [];
  clean.split("/").forEach((part) => {
    if (!part || part === ".") return;
    if (part === "..") {
      parts.pop();
      return;
    }
    parts.push(part);
  });
  clean = parts.join("/");
  if (docPaths.has(clean)) return clean;
  if (!clean.endsWith(".md") && docPaths.has(`${clean}.md`)) return `${clean}.md`;
  return null;
}

function splitMathSegments(value) {
  const segments = [];
  let index = 0;
  while (index < value.length) {
    const next = findNextMathStart(value, index);
    if (!next) {
      segments.push({ kind: "text", text: value.slice(index) });
      break;
    }
    if (next.start > index) {
      segments.push({ kind: "text", text: value.slice(index, next.start) });
    }
    const end = value.indexOf(next.close, next.start + next.open.length);
    if (end === -1) {
      segments.push({ kind: "text", text: value.slice(next.start) });
      break;
    }
    segments.push({
      kind: "math",
      display: next.display,
      text: value.slice(next.start + next.open.length, end),
    });
    index = end + next.close.length;
  }
  return segments.filter((segment) => segment.text.length);
}

function findNextMathStart(value, start) {
  const candidates = [
    { open: "$$", close: "$$", display: true },
    { open: "\\[", close: "\\]", display: true },
    { open: "\\(", close: "\\)", display: false },
    { open: "$", close: "$", display: false },
  ];
  let best = null;
  for (const candidate of candidates) {
    const position = value.indexOf(candidate.open, start);
    if (position === -1) continue;
    if (candidate.open === "$" && value[position + 1] === "$") continue;
    if (!best || position < best.start) {
      best = { ...candidate, start: position };
    }
  }
  return best;
}

function renderMath(raw, display) {
  const tag = display ? "div" : "span";
  return `<${tag} class="math-source" data-display="${display ? "true" : "false"}">${escapeHtml(String(raw || "").trim())}</${tag}>`;
}

function typesetMath(root) {
  if (!window.katex) {
    root.querySelectorAll(".math-source").forEach((node) => {
      node.classList.add("math-error");
    });
    return;
  }
  root.querySelectorAll(".math-source").forEach((node) => {
    const tex = node.textContent || "";
    const displayMode = node.dataset.display === "true";
    try {
      window.katex.render(tex, node, {
        displayMode,
        throwOnError: false,
        strict: "ignore",
        trust: false,
        output: "htmlAndMathml",
      });
      node.classList.remove("math-source");
    } catch (error) {
      node.classList.add("math-error");
      node.title = error && error.message ? error.message : "KaTeX render error";
    }
  });
}

function bindCopyButtons() {
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      await copyText(button.dataset.copy);
      showToast("Copied");
    });
  });
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.focus();
  area.select();
  document.execCommand("copy");
  area.remove();
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 1100);
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatDateTime(value) {
  // Format a timestamp in the browser local timezone.
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function cssEscape(value) {
  // Use the browser implementation when available; otherwise escape common selector hazards.
  if (window.CSS && window.CSS.escape) return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

init().catch((error) => {
  document.body.innerHTML = `<pre>${escapeHtml(error.stack || error.message || error)}</pre>`;
});
"""
