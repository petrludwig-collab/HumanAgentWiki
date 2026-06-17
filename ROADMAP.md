# Roadmap

HumanAgentWiki is being generalized from a working private system into a tool anyone can run. The retrieval core (pgvector hybrid search, MCP server, incremental indexing) already works; the focus now is removing setup-specific assumptions and adding a friendly UX.

## Core (extract & generalize)
- [ ] **Decouple paths & DB** — no hardcoded folders/connection strings.
- [ ] **Pluggable embeddings** — BGE-M3 by default (multilingual), with optional alternatives (other local models / API). Auto device select (CUDA / Apple MPS / CPU).
- [ ] **Drop domain-specific parsing from core** — anything tied to one person's content conventions moves into an optional, opt-in plugin (kept out of the default path).
- [x] **Incremental indexing** — content-hash change detection; only changed files re-embedded (working in source, to be ported).
- [ ] **`schema.sql` / `init-db`** — create extensions + tables + HNSW/GIN indexes in one command (see [schema.sql](schema.sql)).

## No config files — it's in the UX
- [ ] **Categories managed in the UI** — define your own top-level categories, add/remove with +/–. Stored in the database, not in a config file.
- [ ] **Onboarding on first run** — ship sample notes + a short built-in guide, so nobody starts on a blank page.

## App
- [ ] **Web UI** — three things: manage categories, write/save notes, search.
- [ ] **MCP server** — `brain_search` / `brain_get` / `brain_neighbors` for any MCP client.
- [ ] **Unified CLI** — `init` / `index` / `search` / `serve` / `selftest`.
- [ ] **Docker compose** — Postgres + pgvector, one command to start.
- [ ] **Self-test battery** — health checks for DB, embeddings, indexes, incremental sync, live MCP search.

## Principles
- **Local & private by default.** Your notes never leave your machine.
- **Multilingual by default.** Not English-only.
- **Markdown + `[[wikilinks]]`** — Obsidian-compatible: point it at an existing vault.
