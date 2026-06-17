"""Local web UI for HumanAgentWiki.

Three things, wiki-style:
  • manage categories (+/-)        • write/link notes        • semantic search

Notes are saved as Markdown files under NOTES_DIR; every save is a git commit
(full history), and the saved file is re-indexed immediately. Run with:

  python cli.py web        (or: uvicorn web:app --port 8808)
"""
import os
import re
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel

import index
import server
from common import connect, NOTES_DIR

HERE = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(_app):
    ensure_setup()
    yield


app = FastAPI(title="HumanAgentWiki", lifespan=lifespan)


# ---------- helpers ----------
def _git(*args):
    # Best-effort versioning: a failing commit (e.g. "nothing to commit") must not
    # break a save. If git is missing, notes are still written — just unversioned.
    subprocess.run(["git", "-C", NOTES_DIR, *args], capture_output=True)


def ensure_setup():
    os.makedirs(NOTES_DIR, exist_ok=True)
    if not os.path.isdir(os.path.join(NOTES_DIR, ".git")):
        _git("init")
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS categories "
                 "(name text PRIMARY KEY, created_at timestamptz DEFAULT now())")
    conn.close()


def slugify(t):
    s = re.sub(r"[^\w\s-]", "", t.lower(), flags=re.UNICODE).strip()
    return re.sub(r"[\s_]+", "-", s) or "note"


def safe_md_path(rel):
    """Resolve a client-supplied path to an absolute .md path strictly inside
    NOTES_DIR. Rejects absolute paths, non-.md files, and `..` escapes."""
    if os.path.isabs(rel) or not rel.endswith(".md"):
        raise HTTPException(400, "path must be a relative .md file")
    path = os.path.normpath(os.path.join(NOTES_DIR, rel))
    if os.path.commonpath([NOTES_DIR, path]) != NOTES_DIR:
        raise HTTPException(400, "path escapes the notes directory")
    return path


# ---------- categories ----------
@app.get("/api/categories")
def categories():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT name FROM categories ORDER BY name")
    out = [r[0] for r in cur.fetchall()]; conn.close()
    return out


class CategoryIn(BaseModel):
    name: str


@app.post("/api/categories")
def add_category(c: CategoryIn):
    name = c.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    if "/" in name or "\\" in name or name.startswith("."):
        # the name becomes a folder under NOTES_DIR — keep it from escaping
        raise HTTPException(400, "category name cannot contain slashes or start with a dot")
    conn = connect()
    conn.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
    conn.close()
    os.makedirs(os.path.join(NOTES_DIR, name), exist_ok=True)
    return {"ok": True}


@app.delete("/api/categories/{name}")
def delete_category(name: str):
    # Removes the category from the list only; existing notes/files are kept.
    conn = connect()
    conn.execute("DELETE FROM categories WHERE name = %s", (name,))
    conn.close()
    return {"ok": True}


# ---------- notes ----------
@app.get("/api/notes")
def notes():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT title, file, category FROM chunks ORDER BY category, title")
    out = [dict(title=r[0], file=r[1], category=r[2]) for r in cur.fetchall()]; conn.close()
    return out


@app.get("/api/titles")
def titles():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT title FROM chunks ORDER BY title")
    out = [r[0] for r in cur.fetchall()]; conn.close()
    return out


@app.get("/api/note")
def get_note(file: str):
    path = safe_md_path(file)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    fm, body = index.parse_frontmatter(raw)
    title = fm.get("title", os.path.splitext(os.path.basename(file))[0])
    category = fm.get("category", index.category_of(file))
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT DISTINCT file, title FROM chunks WHERE %s = ANY(links) AND file <> %s",
                (title, file))
    backlinks = [{"id": r["file"], "label": r["title"]} for r in cur.fetchall()]
    conn.close()
    return {"id": file, "title": title, "category": category, "body": body.strip(),
            "backlinks": backlinks}


class NoteIn(BaseModel):
    category: str
    title: str
    text: str
    file: str | None = None


@app.post("/api/note")
def save_note(n: NoteIn):
    title = n.title.strip()
    cat = (n.category.strip() or "uncategorized")
    if not title:
        raise HTTPException(400, "title required")
    rel = n.file or os.path.join(cat, slugify(title) + ".md")
    path = safe_md_path(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\ntitle: {title}\ncategory: {cat}\n---\n\n{n.text.strip()}\n")
    # git versioning: every save is a commit
    _git("add", "-A")
    _git("commit", "-m", f"{'edit' if n.file else 'add'}: {title}")
    # incremental reindex of just this file (model already warm in this process)
    conn = connect(); cur = conn.cursor()
    index.reindex_files(cur, [path])
    cur.execute("""INSERT INTO files (file, hash, updated_at) VALUES (%s, %s, now())
                   ON CONFLICT (file) DO UPDATE SET hash = EXCLUDED.hash, updated_at = now()""",
                (rel, index.file_hash(path)))
    conn.close()
    return {"ok": True, "file": rel}


# ---------- graph ----------
@app.get("/api/graph")
def graph():
    """Nodes (one per note file) and links (from [[wikilinks]]) for the 3D view."""
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT DISTINCT ON (file) file, title, category, node_type FROM chunks ORDER BY file, id")
    base = cur.fetchall()
    cur.execute("SELECT file, array_agg(DISTINCT l) AS links "
                "FROM chunks CROSS JOIN LATERAL unnest(links) AS l GROUP BY file")
    links_by_file = {r["file"]: r["links"] for r in cur.fetchall()}
    conn.close()
    title_to_file = {r["title"]: r["file"] for r in base}

    def slug(s):
        return re.sub(r"[^a-z0-9-]", "", s.lower())

    # brains link by slug (filename), not by title - so resolve [[X]] against both.
    slug_to_file = {slug(os.path.splitext(os.path.basename(r["file"]))[0]): r["file"] for r in base}
    nodes = {r["file"]: {"id": r["file"], "label": r["title"], "group": r["category"],
                         "val": 54 if r["node_type"] == "hub" else 2} for r in base}
    # categories are nodes too: one hub per category; every note links to it.
    for c in sorted({r["category"] for r in base}):
        nodes["cat:" + c] = {"id": "cat:" + c, "label": c, "group": c, "val": 54, "is_cat": True}
    links = [{"source": r["file"], "target": "cat:" + r["category"]} for r in base]
    for src, targets in links_by_file.items():
        for t in targets:
            dst = title_to_file.get(t) or slug_to_file.get(slug(t))
            if dst is None:                       # link to a note that doesn't exist (yet)
                dst = "ext:" + t
                nodes.setdefault(dst, {"id": dst, "label": t, "group": "(unresolved)", "val": 2})
            links.append({"source": src, "target": dst})
    # three discrete sizes like the brain: hub/category = 54, well-connected = 16, leaf = 2.
    degree = {}
    for l in links:
        if str(l["target"]).startswith("cat:"):
            continue                              # the category anchor shouldn't inflate sizes
        degree[l["source"]] = degree.get(l["source"], 0) + 1
        degree[l["target"]] = degree.get(l["target"], 0) + 1
    for nid, node in nodes.items():
        if node.get("is_cat") or node["val"] == 54:   # category hubs / explicit hubs stay big
            continue
        d = degree.get(nid, 0)
        node["val"] = 16 if d >= 4 else 2   # 54 reserved for hubs/categories, like the brain
    return {"nodes": list(nodes.values()), "links": links}


# ---------- search ----------
@app.get("/api/search")
def api_search(q: str, k: int = 10):
    return server.search(q, k=k)


@app.get("/")
def root():
    return RedirectResponse("/static/index.html")


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
