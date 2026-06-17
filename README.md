# HumanAgentWiki

> A local, private **"second brain" your AI agents can actually read** — semantic search + [MCP](https://modelcontextprotocol.io) over your own Markdown notes.

**Status: early WIP** — being extracted and generalized from a working private system. See [ROADMAP.md](ROADMAP.md).

---

## What it is

HumanAgentWiki turns *your own notes* (plain Markdown) into a shared, searchable knowledge base that any **MCP-compatible AI agent** can plug into. Everything runs **locally on your machine** — your knowledge never leaves your computer.

## Why

- 🧠 **LLMs don't know your context.** HumanAgentWiki gives them retrieval over what *you* actually wrote and think (RAG), so answers are grounded in your knowledge — not generic web fluff, and far fewer hallucinations.
- 🔒 **Local & private.** PostgreSQL + [pgvector](https://github.com/pgvector/pgvector) on your own machine. No cloud upload.
- 🌍 **Multilingual.** Uses [BGE-M3](https://huggingface.co/BAAI/bge-m3) embeddings — excellent for Czech and 100+ languages, not just English.
- 🔌 **Agent-native.** Exposes an MCP server, so Claude and other MCP clients connect with zero glue code. Multiple agents share one brain.

## Install

One line:

```bash
curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/HumanAgentWiki/main/install.sh | bash
```

Or clone and run the installer:

```bash
git clone https://github.com/petrludwig-collab/HumanAgentWiki.git
cd HumanAgentWiki && ./install.sh
```

It checks prerequisites, creates a virtualenv, installs dependencies, brings up PostgreSQL (Docker if available, otherwise a local instance), writes `.env`, and creates the schema. Then use the `./haw` launcher:

```bash
./haw index     # index your notes (Markdown under ./notes)
./haw web       # web UI at http://127.0.0.1:8808
./haw serve     # MCP server for your agents
```

> Try it instantly with the bundled demo: `NOTES_DIR=./sample_notes ./haw index && ./haw web`

## How it works (plain English)

1. Your notes are **Markdown files**, linked like a wiki with `[[links]]`.
2. They get chunked and **embedded** — each idea gets a "meaning fingerprint" stored in pgvector.
3. Search finds things **by meaning, not keywords** — search *"how not to get dumber from AI"* and it finds your note about *"cognitive laziness"* even without those exact words.
4. **Incremental indexing** — only changed files get re-embedded (seconds, not minutes).
5. An **MCP server** (`brain_search` / `brain_get` / `brain_neighbors`) lets your agents query it live.

## Categories - no config files

You define your top-level **categories right in the app** (add/remove with +/–) — e.g. *Books, Podcast, Interviews, Notes* — and start saving into them. No YAML, no config editing. On first run you get **sample notes + a built-in guide** so you instantly see how it works.

## Quick start

```bash
# 1. Start PostgreSQL + pgvector
docker compose up -d

# 2. Install dependencies (a virtualenv is recommended)
pip install -r requirements.txt

# 3. Configure (copy and edit if needed)
cp .env.example .env && set -a && . ./.env && set +a

# 4. Create the schema
python cli.py init-db

# 5. Index your notes  (put Markdown in ./notes — folders are categories;
#    or try the bundled demo:)
NOTES_DIR=./sample_notes python cli.py index

# 6. Search from the terminal…
NOTES_DIR=./sample_notes python cli.py search "how do small habits compound"

# 7. …or run the MCP server for your agents
python cli.py serve

# 8. …or open the web UI (manage categories, write/link notes, search)
python cli.py web      # then open http://127.0.0.1:8808
```

Then point any MCP client at `http://127.0.0.1:8802/mcp`. For Claude Code:

```bash
claude mcp add --transport http humanagentwiki http://127.0.0.1:8802/mcp
```

Tools exposed: `brain_search`, `brain_get`, `brain_neighbors`.

## Web UI

`python cli.py web` opens a wiki-style interface (no build step, vanilla JS):
- **3D graph** — your notes as an interactive 3D graph (rotate / zoom / pan), nodes coloured by category, edges from `[[links]]`. Click a node to open it. This is the centerpiece.
- **Categories** — add/remove your top-level categories with +/- (left panel).
- **Note editor** — slides in on the right: pick a category, write, and link other notes with `[[...]]` (autocomplete). Saving writes a Markdown file, **commits it to git** (full history), and re-indexes it.
- **Search** — semantic search across everything.

## Development

```bash
pip install -r requirements-dev.txt
pytest        # unit tests for the pure helpers (no DB needed)
```

## On the roadmap

See [ROADMAP.md](ROADMAP.md).

## License

[MIT](LICENSE)
