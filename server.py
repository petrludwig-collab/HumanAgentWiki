"""MCP server over your notes — hybrid semantic + keyword search via pgvector.

Runs as a local streamable-http service; the embedding model is loaded once and
kept in memory. Any MCP-compatible agent (Claude, etc.) connects to it.

Tools: brain_search, brain_get, brain_neighbors.
"""
from mcp.server.fastmcp import FastMCP
from common import connect, embed, MCP_HOST, MCP_PORT

mcp = FastMCP("humanagentwiki", host=MCP_HOST, port=MCP_PORT)

COLS = "id,file,category,node_type,title,links,text"


def _filters(category, node_type):
    cl, params = [], []
    if category:
        cl.append("category = %s"); params.append(category)
    if node_type:
        cl.append("node_type = %s"); params.append(node_type)
    return (" AND " + " AND ".join(cl)) if cl else "", params


def _row(r):
    txt = r[6]
    return dict(id=r[0], file=r[1], category=r[2], node_type=r[3], title=r[4],
                links=r[5], snippet=txt[:400] + ("…" if len(txt) > 400 else ""))


def search(query, k=8, category="", node_type=""):
    """Hybrid semantic + keyword search with Reciprocal Rank Fusion. Plain function
    so both the MCP tool and the CLI can call it."""
    qvec = embed(query)[0]
    fcl, fp = _filters(category, node_type)
    n = max(k * 4, 30)
    conn = connect(); cur = conn.cursor()
    cur.execute(f"SELECT {COLS} FROM chunks WHERE TRUE {fcl} "
                f"ORDER BY embedding <=> %s::vector LIMIT %s", fp + [qvec, n])
    vec = cur.fetchall()
    cur.execute(f"SELECT {COLS} FROM chunks "
                f"WHERE tsv @@ websearch_to_tsquery('simple', unaccent(%s)) {fcl} "
                f"ORDER BY ts_rank(tsv, websearch_to_tsquery('simple', unaccent(%s))) DESC "
                f"LIMIT %s", [query] + fp + [query, n])
    ft = cur.fetchall()
    conn.close()
    # Reciprocal Rank Fusion of the two result lists
    fused = {}
    for lst in (vec, ft):
        for rank, r in enumerate(lst):
            fused.setdefault(r[0], [r, 0.0])
            fused[r[0]][1] += 1.0 / (60 + rank)
    ranked = sorted(fused.values(), key=lambda x: -x[1])[:k]
    return [_row(r) for r, _ in ranked]


@mcp.tool()
def brain_search(query: str, k: int = 8, category: str = "", node_type: str = "") -> list:
    """Hybrid semantic + keyword search over the notes.
    query: search text (any language). k: number of results.
    category / node_type: optional filters. Returns ranked notes with a snippet."""
    return search(query, k, category, node_type)


@mcp.tool()
def brain_get(title_or_file: str) -> list:
    """Return the full text of notes by exact title or file path."""
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT file,category,node_type,title,links,text FROM chunks "
                "WHERE title = %s OR file = %s LIMIT 25", (title_or_file, title_or_file))
    out = [dict(file=r[0], category=r[1], node_type=r[2], title=r[3], links=r[4], text=r[5])
           for r in cur.fetchall()]
    conn.close()
    return out


@mcp.tool()
def brain_neighbors(name: str, k: int = 15) -> dict:
    """Graph: notes this one links to ([[links]]) and notes that link back to it."""
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT unnest(links) FROM chunks WHERE title = %s OR file = %s",
                (name, name))
    outgoing = [x[0] for x in cur.fetchall()]
    cur.execute("SELECT title,file,category FROM chunks WHERE %s = ANY(links) LIMIT %s", (name, k))
    incoming = [dict(title=r[0], file=r[1], category=r[2]) for r in cur.fetchall()]
    conn.close()
    return dict(links_to=outgoing, linked_from=incoming)


if __name__ == "__main__":
    embed("warmup")  # load the model into memory at startup
    print(f"HumanAgentWiki MCP server: {MCP_HOST}:{MCP_PORT} (streamable-http, /mcp)", flush=True)
    mcp.run(transport="streamable-http")
