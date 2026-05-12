"""Command-line interface for scanning and serving project documentation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_doc_board.scanner import build_manifest, write_project_outputs
from agent_doc_board.server import serve


def _root_arg(value: str) -> Path:
    """Resolve a CLI root argument into an absolute project path."""
    return Path(value).expanduser().resolve()


def _build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="agent-doc-board",
        description="Scan markdown docs and serve a local agent documentation board.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a project and optionally write outputs.")
    scan_parser.add_argument("--root", type=_root_arg, default=Path.cwd(), help="Project root to scan.")
    scan_parser.add_argument("--write", action="store_true", help="Write manifest, index, and TODO outputs.")
    scan_parser.add_argument("--json", action="store_true", help="Print the manifest JSON to stdout.")

    serve_parser = subparsers.add_parser("serve", help="Serve the local documentation board.")
    serve_parser.add_argument("--root", type=_root_arg, default=Path.cwd(), help="Project root to serve.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    serve_parser.add_argument("--port", type=int, default=8765, help="Bind port.")

    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the requested Agent Doc Board command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        manifest = build_manifest(args.root)
        if args.write:
            write_project_outputs(args.root, manifest)
        if args.json or not args.write:
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    if args.command == "serve":
        serve(root=args.root, host=args.host, port=args.port)
        return

    parser.error(f"unknown command: {args.command}")

