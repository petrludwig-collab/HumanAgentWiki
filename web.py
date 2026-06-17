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

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import index
import server
from common import connect, NOTES_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="HumanAgentWiki")


# ---------- helpers ----------
def _git(*args):
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


@app.on_event("startup")
def _startup():
    ensure_setup()


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
    path = os.path.normpath(os.path.join(NOTES_DIR, file))
    if not path.startswith(NOTES_DIR) or not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return {"file": file, "content": open(path, encoding="utf-8").read()}


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
    path = os.path.normpath(os.path.join(NOTES_DIR, rel))
    if not path.startswith(NOTES_DIR):
        raise HTTPException(400, "bad path")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(
        f"---\ntitle: {title}\ncategory: {cat}\n---\n\n{n.text.strip()}\n")
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


# ---------- search ----------
@app.get("/api/search")
def api_search(q: str, k: int = 10):
    return server.search(q, k=k)


@app.get("/")
def root():
    return RedirectResponse("/static/index.html")


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
