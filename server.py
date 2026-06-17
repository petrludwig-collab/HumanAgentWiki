"""MCP server over your notes — hybrid semantic + keyword search via pgvector.

Runs as a local streamable-http service; the embedding model is loaded once and
kept in memory. Any MCP-compatible agent (Claude, etc.) connects to it.

Tools: brain_search, brain_get, brain_neighbors.
"""
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row

from common import connect, embed, MCP_HOST, MCP_PORT

mcp = FastMCP("humanagentwiki", host=MCP_HOST, port=MCP_PORT)

COLS = "id, file, category, node_type, title, links, text"


def _filters(category, node_type):
    clauses, params = [], []
    if category:
        clauses.append("category = %s"); params.append(category)
    if node_type:
        clauses.append("node_type = %s"); params.append(node_type)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _hit(row):
    text = row["text"]
    return dict(id=row["id"], file=row["file"], category=row["category"],
                node_type=row["node_type"], title=row["title"], links=row["links"],
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
    category / node_type: optional filters. Returns ranked notes with a snippet."""
    return search(query, k, category, node_type)


@mcp.tool()
def brain_get(title_or_file: str) -> list:
    """Return the full text of notes by exact title or file path."""
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT file, category, node_type, title, links, text FROM chunks "
                "WHERE title = %s OR file = %s LIMIT 25", (title_or_file, title_or_file))
    out = [dict(row) for row in cur.fetchall()]
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


def serve():
    embed("warmup")  # load the model into memory before accepting requests
    print(f"HumanAgentWiki MCP server: {MCP_HOST}:{MCP_PORT} (streamable-http, /mcp)", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    serve()
