# Element Annotation Comments Design

## Goal

Add document-bound comments that can attach to rendered markdown elements, not only selected text. The existing whole-document text area remains a separate `Side Note`.

## Model

Each markdown document gets a sidecar annotation file under `.agent-docs/annotations/<doc-path>.json`. For example, `docs/foo.md` stores comments in `.agent-docs/annotations/docs/foo.md.json`. This keeps comments bound to the source document while avoiding a giant shared state file.

Each annotation stores:

- `id`: stable annotation id.
- `anchor`: rendered element anchor metadata.
- `quote`: selected text when available, otherwise a compact snapshot of the target element.
- `comment`: user-written note.
- `created_at` and `updated_at`: server timestamps.

The first implementation uses element anchors with a DOM selector, block index, element kind, and text fingerprint. This is more robust than arbitrary DOM Range offsets for markdown that may re-render tables, math, and code blocks.

## UX

The reader assigns annotation targets to headings, paragraphs, list items, blockquotes, code blocks, tables, math blocks, and images. A small gutter button appears beside each target. Clicking it opens a composer for that element.

The primary text workflow is selection-first: when the user highlights text inside the document, a small `Add comment` toolbar appears near the selection. Clicking it opens the composer and stores the highlighted text as the quote while binding the comment to the nearest rendered block anchor. This lets comments refer to an exact phrase without depending on fragile arbitrary DOM Range persistence.

Existing comments appear in a `Comments` panel in the document context and as numbered gutter pins beside anchored elements. Clicking a pin focuses the relevant comments. Comments can be edited or deleted.

## Scope

This version is intentionally block/element-level first. Precise Google-Docs-style text-range anchoring can be added later by storing offsets inside the nearest element anchor.
