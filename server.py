"""MCP server over your notes — hybrid semantic + keyword search via pgvector.

Runs as a local streamable-http service; the embedding model is loaded once and
kept in memory. Any MCP-compatible agent (Claude, etc.) connects to it.

Tools: brain_search, brain_get, brain_neighbors, brain_write.
"""
import os
import re
import subprocess

from psycopg.rows import dict_row

from common import connect, embed, MCP_HOST, MCP_PORT, NOTES_DIR
import index

# FastMCP needs the `mcp` package < 2.0 (mcp 2.x removed `mcp.server.fastmcp`) on python >= 3.10.
# When it is unavailable — mcp 2.x already installed, or no mcp on a python 3.9 interpreter — fall
# back to a no-op so the web / search side keeps working and only serve() reports a clear error.
# requirements.txt pins mcp<2 so a normal install never hits this; the guard just protects a
# pre-existing environment from a hard crash on import.
try:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("humanagentwiki", host=MCP_HOST, port=MCP_PORT)
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False

    class _NoMCP:
        """Stand-in when FastMCP is unavailable: tool() is a transparent decorator, run() explains."""
        def tool(self, *a, **k):
            return lambda fn: fn

        def run(self, *a, **k):
            raise SystemExit("The MCP server needs the 'mcp' package >=1.2,<2 on python >=3.10.\n"
                             "Install it with:  pip install 'mcp>=1.2,<2'")

    mcp = _NoMCP()

COLS = "id, file, category, node_type, title, links, text, updated_at"


def _filters(category, node_type):
    clauses, params = [], []
    if category:
        clauses.append("category = %s"); params.append(category)
    if node_type:
        clauses.append("node_type = %s"); params.append(node_type)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _iso_date(value):
    """A note's last-changed date as YYYY-MM-DD, so an agent can judge staleness."""
    return value.date().isoformat() if value is not None else None


def _hit(row):
    text = row["text"]
    return dict(id=row["id"], file=row["file"], category=row["category"],
                node_type=row["node_type"], title=row["title"], links=row["links"],
                updated=_iso_date(row.get("updated_at")),
                snippet=text[:400] + ("..." if len(text) > 400 else ""))


def search(query, k=8, category="", node_type=""):
    """Hybrid semantic + keyword search with Reciprocal Rank Fusion. A plain
    function so both the MCP tool and the CLI can call it."""
    qvec = embed(query)[0]
    fcl, fparams = _filters(category, node_type)
    pool = max(k * 4, 30)
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(f"SELECT {COLS} FROM chunks WHERE TRUE {fcl} "
                f"ORDER BY embedding <=> %s::vector LIMIT %s", fparams + [qvec, pool])
    by_vector = cur.fetchall()
    cur.execute(f"SELECT {COLS} FROM chunks "
                f"WHERE tsv @@ websearch_to_tsquery('simple', unaccent(%s)) {fcl} "
                f"ORDER BY ts_rank(tsv, websearch_to_tsquery('simple', unaccent(%s))) DESC "
                f"LIMIT %s", [query] + fparams + [query, pool])
    by_text = cur.fetchall()
    conn.close()
    # Reciprocal Rank Fusion of the two ranked lists.
    fused = {}
    for rows in (by_vector, by_text):
        for rank, row in enumerate(rows):
            fused.setdefault(row["id"], [row, 0.0])
            fused[row["id"]][1] += 1.0 / (60 + rank)
    ranked = sorted(fused.values(), key=lambda pair: -pair[1])[:k]
    return [_hit(row) for row, _score in ranked]


@mcp.tool()
def brain_search(query: str, k: int = 8, category: str = "", node_type: str = "") -> list:
    """Hybrid semantic + keyword search over the notes.
    query: search text (any language). k: number of results.
    category / node_type: optional filters. Returns ranked notes with a snippet and
    `updated` (YYYY-MM-DD, the note's last-changed date) so you can spot stale information."""
    return search(query, k, category, node_type)


@mcp.tool()
def brain_get(title_or_file: str) -> list:
    """Return the full text of notes by exact title or file path.
    Each note includes `updated` (YYYY-MM-DD, its last-changed date) so you can judge staleness."""
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT file, category, node_type, title, links, text, updated_at FROM chunks "
                "WHERE title = %s OR file = %s LIMIT 25", (title_or_file, title_or_file))
    out = []
    for row in cur.fetchall():
        d = dict(row)
        d["updated"] = _iso_date(d.pop("updated_at", None))
        out.append(d)
    conn.close()
    return out


@mcp.tool()
def brain_neighbors(name: str, k: int = 15) -> dict:
    """Graph: notes this one links to ([[links]]) and notes that link back to it."""
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT DISTINCT unnest(links) AS link FROM chunks WHERE title = %s OR file = %s",
                (name, name))
    outgoing = [row["link"] for row in cur.fetchall()]
    cur.execute("SELECT title, file, category FROM chunks WHERE %s = ANY(links) LIMIT %s", (name, k))
    incoming = [dict(row) for row in cur.fetchall()]
    conn.close()
    return dict(links_to=outgoing, linked_from=incoming)


def _slugify(t):
    s = re.sub(r"[^\w\s-]", "", t.lower(), flags=re.UNICODE).strip()
    return re.sub(r"[\s_]+", "-", s) or "note"


def _safe_folder(name):
    """A single safe folder name for a category — no separators, no traversal."""
    return os.path.basename((name or "").strip().strip("/").replace("..", "")) or "uncategorized"


def _safe_note_path(rel):
    """Absolute .md path strictly inside NOTES_DIR, or raise ValueError (blocks traversal)."""
    if os.path.isabs(rel) or not rel.endswith(".md"):
        raise ValueError("path must be a relative .md file")
    path = os.path.normpath(os.path.join(NOTES_DIR, rel))
    if os.path.commonpath([NOTES_DIR, path]) != NOTES_DIR:
        raise ValueError("path escapes the notes directory")
    return path


def _git(*args):
    # Best-effort versioning — a missing git or an empty commit must never fail a write.
    subprocess.run(["git", "-C", NOTES_DIR, *args], capture_output=True)


@mcp.tool()
def brain_write(title: str, text: str, category: str = "uncategorized",
                tags: list = None, links: list = None, node_type: str = "note") -> dict:
    """Create or update a note: writes a Markdown file, git-commits it, and re-indexes it so
    search/read see it immediately. Before creating a new note it looks for an existing note with
    the same title and updates that one instead of making a duplicate.

    title: note title (also its kebab-cased filename). text: Markdown body.
    category: top-level folder. tags / links: optional lists (links become [[wikilinks]]).
    node_type: 'note' or 'hub'. Returns {ok, file, action: 'created'|'updated'}."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "title required"}
    cat = _safe_folder(category)
    rel = os.path.join(cat, _slugify(title) + ".md")
    # de-dup: if a note with this exact title already exists, update it in place
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT file FROM chunks WHERE lower(title) = lower(%s) LIMIT 1", (title,))
    row = cur.fetchone(); conn.close()
    if row:
        rel = row[0]
    try:
        path = _safe_note_path(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    existed = os.path.exists(path)
    front = [f"title: {title}", f"category: {cat}"]
    if node_type and node_type != "note":
        front.append(f"type: {node_type}")
    if tags:
        clean = [t.strip() for t in tags if t and t.strip()]
        if clean:
            front.append("tags: [" + ", ".join(clean) + "]")
    body = (text or "").strip()
    if links:
        refs = " ".join(f"[[{l.strip()}]]" for l in links if l and l.strip())
        if refs:
            body = f"{body}\n\n{refs}".strip()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + f"\n---\n\n{body}\n")
    _git("add", "-A"); _git("commit", "-m", f"{'edit' if existed else 'add'}: {title}")
    conn = connect(); cur = conn.cursor()
    index.reindex_files(cur, [path])
    cur.execute("INSERT INTO files (file, hash, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (file) DO UPDATE SET hash = EXCLUDED.hash, updated_at = now()",
                (rel, index.file_hash(path)))
    conn.close()
    return {"ok": True, "file": rel, "action": "updated" if existed else "created"}


def serve():
    embed("warmup")  # load the model into memory before accepting requests
    print(f"HumanAgentWiki MCP server: {MCP_HOST}:{MCP_PORT} (streamable-http, /mcp)", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    serve()
