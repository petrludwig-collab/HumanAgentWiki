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

## Next
- [ ] **3D visualization** of the note graph (rotate / zoom / focus a slice).
- [ ] **Git versioning** — every edit (by you *or* an agent) is committed → full history.
- [ ] **Web UI** — manage categories (+/–), write/save notes, search in the browser.
- [ ] **Categories managed in the UI** — defined in-app, stored in the DB (no config file).

## Principles
- **Local & private by default.** Your notes never leave your machine.
- **Multilingual by default.** Not English-only.
- **Markdown + `[[wikilinks]]`** — Obsidian-compatible: point it at an existing vault.
