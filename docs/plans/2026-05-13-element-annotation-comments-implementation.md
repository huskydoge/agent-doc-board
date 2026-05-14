# Element Annotation Comments Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build document-bound element comments for Agent Doc Board while renaming the old whole-document comment box to Side Note.

**Architecture:** Keep the existing single-file standard-library server. Store annotations as per-document sidecar JSON files under `.agent-docs/annotations/`. Add a small client-side annotation layer that discovers rendered markdown blocks, opens a composer from selected text, and syncs comments through a new local API.

**Tech Stack:** Python standard-library HTTP server, local JSON files, vanilla JavaScript, vanilla CSS.

---

### Task 1: Add Annotation Storage API

**Files:**
- Modify: `agent_doc_board/server.py`

**Steps:**
1. Add `GET` inclusion of annotations in `/api/doc`.
2. Add `POST /api/annotations` with actions `save` and `delete`.
3. Store per-doc JSON at `.agent-docs/annotations/<doc-path>.json`.
4. Validate doc path against the manifest before reading or writing.
5. Clamp comment and quote lengths to local-safe limits.

### Task 2: Rename Whole-Doc Side Comment UI

**Files:**
- Modify: `agent_doc_board/server.py`

**Steps:**
1. Change visible labels from `Side Comment` to `Side Note`.
2. Keep the existing `side_comment` state key for backward compatibility.
3. Change button text to `Save Note`.

### Task 3: Add Element Anchors and Gutter Pins

**Files:**
- Modify: `agent_doc_board/server.py`

**Steps:**
1. After markdown render and KaTeX render, collect commentable elements.
2. Generate anchor metadata for each element.
3. Keep anchors invisible and use them only for selected-text binding.
4. Recompute anchors on resize and document changes.

### Task 4: Add Composer, Listing, Edit, and Delete

**Files:**
- Modify: `agent_doc_board/server.py`

**Steps:**
1. Add a `Comments` context panel.
2. Add a fixed composer dialog for new/edit annotation comments.
3. Save/delete annotations through `/api/annotations`.
4. Refresh the panel after each mutation.

### Task 5: Validate and Push

**Files:**
- Modify: `agent_doc_board/server.py`
- Create: `docs/plans/2026-05-13-element-annotation-comments-design.md`
- Create: `docs/plans/2026-05-13-element-annotation-comments-implementation.md`

**Steps:**
1. Run Python compile validation.
2. Extract and check generated JavaScript with `node --check`.
3. Run `git diff --check`.
4. Serve the board on `127.0.0.1:8765`.
5. Commit and push to `main`.
