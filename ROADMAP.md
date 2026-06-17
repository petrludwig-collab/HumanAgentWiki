# Roadmap

HumanAgentWiki is being generalized from a working private system into a tool anyone can run.

## Done — runnable backend (v0.1)
- [x] **Decoupled paths & DB** — everything via env vars, no hardcoded folders/personal data.
- [x] **Pluggable embeddings** — BGE-M3 by default (multilingual); model/dim configurable. Auto device (CUDA / Apple MPS / CPU).
- [x] **Domain-specific logic removed from core** — generic frontmatter + headings + `[[links]]` only.
- [x] **Incremental indexing** — content-hash change detection; only changed files re-embedded.
- [x] **`schema.sql` + `init-db`** — extensions + tables + HNSW/GIN indexes in one command.
- [x] **MCP server** — `brain_search` / `brain_get` / `brain_neighbors` for any MCP client.
- [x] **Unified CLI** — `init-db` / `index` / `search` / `serve` / `selftest`.
- [x] **Docker compose** — Postgres + pgvector, one command.
- [x] **Self-test battery** — DB, embeddings, indexes, incremental-sync, registry consistency.
- [x] **Onboarding demo** — bundled `sample_notes/` (guide + linked examples).

## Done — web UI (v0.2)
- [x] **Web UI** (`cli.py web`) — wiki-style, no build step (vanilla JS).
- [x] **Categories managed in the UI** — add/remove with +/–, stored in the DB.
- [x] **Note editor** — category + title + body, `[[...]]` link autocomplete.
- [x] **Git versioning** — every save commits the Markdown file (full history).

## Done — quality pass (v0.3)
- [x] **Dict-row DB access** in `server.py` (named columns, no brittle positional indices).
- [x] **Unit tests** (`tests/`, pytest) for the pure helpers.
- [x] **Hardening**: category-name path checks, ASCII-only stdout, `EMB_DIM` wired into `init-db`.

## Done — 3D graph (v0.4)
- [x] **3D visualization** (3d-force-graph): notes as an interactive 3D graph, the
  centerpiece of the UI; nodes coloured by category, edges from `[[links]]`, click to edit.
- [x] **Layout**: 3D graph main canvas, categories (+/-) left, editor slides in on the right.

## Next
- [ ] Inline render of `[[links]]` as clickable links in a read view.
- [ ] Optional auth for exposing the web UI beyond localhost.

## Design decisions
- **No connection pool (intentional).** This is a local, single-user tool; a
  connection-per-request keeps the code simple and dependency-light. Revisit only
  if it's deployed for real concurrency.

## Principles
- **Local & private by default.** Your notes never leave your machine.
- **Multilingual by default.** Not English-only.
- **Markdown + `[[wikilinks]]`** — Obsidian-compatible: point it at an existing vault.
