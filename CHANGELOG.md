# Changelog

All notable changes to HumanAgentWiki are recorded here. Format based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-08-19

Fresh-install fixes reported by the community, plus several retrieval and graph
improvements. Verified end-to-end on a clean Ubuntu 22.04.

### Added
- **Long notes are no longer truncated before embedding.** Overlong sections are split into
  overlapping token windows (each within the model's limit) and every window is embedded, so
  text deep inside a long note is now searchable. New `EMBED_MAX_TOKENS` cap. (#16, from #5)
- **Note staleness date.** `brain_search` and `brain_get` return each note's `updated`
  (YYYY-MM-DD) so an agent can tell fresh notes from months-old ones. (#17, from #2)
- **`brain_write` MCP tool.** Agents can now create or update notes (not just read): writes a
  Markdown file, git-commits it, and re-indexes it, with same-title de-duplication and path
  safety (rejects `../` traversal, stays inside `NOTES_DIR`). (#18, from #2)
- **Graph: a hub note titled like its category becomes the category node** — one node per
  category instead of a duplicate `cat:` node. (#15)
- **Graph: per-category visibility toggle** (🌐/🚫 in the legend) to hide a large folder from
  the 3D view while keeping it in search. Persisted in `category_meta.graph_excluded`. (#15)

### Fixed
- **Fresh install crashed on `mcp` 2.0.** `mcp>=1.2` resolved to 2.0.0, where
  `mcp.server.fastmcp` was removed. Pinned `mcp>=1.2,<2` and guarded the import so a stray
  2.x no longer takes down the web UI and MCP server. (#14, closes #9/#11/#12)
- **`install.sh` finished "green" without pgvector.** It now fails loudly with an actionable
  message (PGDG apt repo / Docker) instead of leaving a broken install. (#14, closes #10)
- **`install.sh` could abort with `USER: unbound variable`** under `set -u` in cron / minimal
  shells. `$USER` is now defaulted. (#14)
- **False `(unresolved)` graph nodes.** `[[wikilink|alias]]` / `[[wikilink#heading]]` targets
  are now stripped to the note name before matching. (#14)
- **`test_category_of` was environment-dependent**, failing the suite 11/12. (#14, closes #6)

## [1.0-mozek3] - 2026-06-18

Initial engine: local hybrid semantic + keyword search over Markdown notes
(PostgreSQL + pgvector + a multilingual embedder), an MCP server exposing
`brain_search` / `brain_get` / `brain_neighbors`, a 3D graph web UI with inline
editing, and a one-shot `install.sh`.
