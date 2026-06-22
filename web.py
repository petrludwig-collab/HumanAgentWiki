"""Local web UI for HumanAgentWiki.

Three things, wiki-style:
  • manage categories (+/-)        • write/link notes        • semantic search

Notes are saved as Markdown files under NOTES_DIR; every save is a git commit
(full history), and the saved file is re-indexed immediately. Run with:

  python cli.py web        (or: uvicorn web:app --port 8808)
"""
import os
import re
import json
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

# Optional: hide body lines matching this regex when displaying a note
# (e.g. HIDE_BODY_LINES='^Patri do \\[\\[' to drop a category-footer line). Display only.
try:
    HIDE_BODY_RE = re.compile(os.environ["HIDE_BODY_LINES"]) if os.environ.get("HIDE_BODY_LINES") else None
except Exception:
    HIDE_BODY_RE = None

# Notes carrying this tag are treated as "hubs" (medium nodes). Categories are the
# largest nodes; everything else is a small leaf.
HUB_TAG = os.environ.get("HUB_TAG")


@asynccontextmanager
async def lifespan(_app):
    ensure_setup()
    # Warm the embedding model in the background so the first save/search isn't a
    # 30s "stuck" wait while the model loads on demand.
    import threading
    from common import embed
    threading.Thread(target=lambda: embed(["warmup"]), daemon=True).start()
    yield


app = FastAPI(title="HumanAgentWiki", lifespan=lifespan)


# Optional HTTP Basic auth for the whole web UI. Set WEB_AUTH="user:password" to enable;
# unset = open (unchanged). Protects the personal notes when the UI is reachable over a
# LAN/VPN. The MCP server (separate process, bound to 127.0.0.1) is not affected.
WEB_AUTH = os.environ.get("WEB_AUTH", "").strip()
if WEB_AUTH:
    import base64
    import secrets
    from starlette.responses import Response as _Response

    @app.middleware("http")
    async def _basic_auth(request, call_next):
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.startswith("Basic "):
            try:
                ok = secrets.compare_digest(
                    base64.b64decode(hdr[6:]).decode("utf-8"), WEB_AUTH)
            except Exception:
                ok = False
        if not ok:
            return _Response(status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="HumanAgentWiki"'})
        return await call_next(request)


# ---------- helpers ----------
def _git(*args):
    # Best-effort versioning: a failing commit (e.g. "nothing to commit") must not
    # break a save. If git is missing, notes are still written — just unversioned.
    subprocess.run(["git", "-C", NOTES_DIR, *args], capture_output=True)


def ensure_setup():
    os.makedirs(NOTES_DIR, exist_ok=True)
    # Initialise a repo only if NOTES_DIR isn't already inside one — it may be a
    # subdirectory of a larger repo, where a nested .git would split history.
    inside = subprocess.run(["git", "-C", NOTES_DIR, "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True)
    if inside.stdout.strip() != "true":
        _git("init")
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS categories "
                 "(name text PRIMARY KEY, created_at timestamptz DEFAULT now())")
    conn.execute("CREATE TABLE IF NOT EXISTS node_tags "
                 "(tag text PRIMARY KEY, category text, created_at timestamptz DEFAULT now())")
    conn.execute("ALTER TABLE node_tags ADD COLUMN IF NOT EXISTS category text")
    conn.execute("CREATE TABLE IF NOT EXISTS category_meta "
                 "(name text PRIMARY KEY, color text, sort_order double precision, "
                 " updated_at timestamptz DEFAULT now())")
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


# Optional fixed colours/order for categories, e.g.
# CATEGORY_COLORS='{"Books":"#34d399","Notes":"#c084fc"}'. Pins a category's colour (and
# its legend order) regardless of how many others exist — so adding a category never
# reshuffles the palette. Empty = colours auto-assigned from the built-in palette.
try:
    CATEGORY_COLORS = json.loads(os.environ.get("CATEGORY_COLORS", "{}"))
except Exception:
    CATEGORY_COLORS = {}


@app.get("/api/config")
def config():
    # Colours/order: env CATEGORY_COLORS as defaults, overridden by anything edited in the UI
    # (persisted in category_meta). Order = saved sort_order, else env key order.
    colors = dict(CATEGORY_COLORS)
    order = list(CATEGORY_COLORS.keys())
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT name, color, sort_order FROM category_meta")
    rows = cur.fetchall(); conn.close()
    ordered = sorted([r for r in rows if r[2] is not None], key=lambda r: r[2])
    for name, color, _ in rows:
        if color:
            colors[name] = color
    if ordered:
        order = [r[0] for r in ordered]
    return {"categoryColors": colors, "categoryOrder": order}


@app.get("/api/stats")
def stats():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT count(DISTINCT file) FROM chunks"); notes = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT category) FROM chunks"); cats = cur.fetchone()[0]
    # characters/words: one representative text per file (the file's chunks concatenated)
    cur.execute("SELECT coalesce(sum(char_length(text)), 0), coalesce(sum(array_length(regexp_split_to_array(btrim(text), '\\s+'), 1)), 0) FROM chunks")
    chars, words = cur.fetchone()
    cur.execute("SELECT count(DISTINCT t) FROM chunks CROSS JOIN LATERAL unnest(tags) AS t"); tags = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM node_tags"); node_tags = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM (SELECT file, unnest(links) FROM chunks) x"); wikilinks = cur.fetchone()[0]
    cur.execute("SELECT category, count(DISTINCT file) c FROM chunks GROUP BY category ORDER BY c DESC LIMIT 1")
    big = cur.fetchone()
    conn.close()
    return {"notes": notes, "categories": cats, "characters": int(chars), "words": int(words),
            "tags": tags, "node_tags": node_tags, "wikilinks": wikilinks,
            "pages": round(chars / 1800), "biggest": {"name": big[0], "count": big[1]} if big else None}


class CategoryMeta(BaseModel):
    name: str
    color: str | None = None


@app.post("/api/category-meta")
def set_category_meta(m: CategoryMeta):
    conn = connect()
    conn.execute("INSERT INTO category_meta (name, color) VALUES (%s, %s) "
                 "ON CONFLICT (name) DO UPDATE SET color = EXCLUDED.color, updated_at = now()",
                 (m.name.strip(), m.color))
    conn.close()
    return {"ok": True}


class CategoryOrder(BaseModel):
    order: list[str]


@app.post("/api/category-order")
def set_category_order(o: CategoryOrder):
    conn = connect()
    for i, name in enumerate(o.order):
        conn.execute("INSERT INTO category_meta (name, sort_order) VALUES (%s, %s) "
                     "ON CONFLICT (name) DO UPDATE SET sort_order = EXCLUDED.sort_order, updated_at = now()",
                     (name.strip(), float(i)))
    conn.close()
    return {"ok": True}


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


class CategoryRename(BaseModel):
    old: str
    new: str


@app.post("/api/categories/rename")
def rename_category(r: CategoryRename):
    old = r.old.strip()
    new = r.new.strip()
    if not new:
        raise HTTPException(400, "name required")
    if "/" in new or "\\" in new or new.startswith("."):
        raise HTTPException(400, "category name cannot contain slashes or start with a dot")
    conn = connect()
    conn.execute("UPDATE chunks    SET category = %s WHERE category = %s", (new, old))
    conn.execute("UPDATE node_tags SET category = %s WHERE category = %s", (new, old))
    conn.execute("UPDATE categories SET name    = %s WHERE name     = %s", (new, old))
    conn.close()
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
    if HIDE_BODY_RE:
        body = "\n".join(ln for ln in body.splitlines() if not HIDE_BODY_RE.search(ln))
    title = fm.get("title", os.path.splitext(os.path.basename(file))[0])
    tags = [t.strip().strip('"').strip("'") for t in re.sub(r"[\[\]]", "", fm.get("tags", "")).split(",") if t.strip()]
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT category FROM chunks WHERE file = %s LIMIT 1", (file,))
    crow = cur.fetchone()
    category = crow["category"] if crow else index.category_label(
        fm.get("category") or fm.get("project") or index.category_of(file))
    # Backlinks: brains link by slug (filename) as well as by title, so match the
    # incoming [[wikilinks]] against both forms of this note.
    slug = os.path.splitext(os.path.basename(file))[0]
    cur.execute("SELECT DISTINCT ON (file) file, title FROM chunks "
                "WHERE links && %s AND file <> %s ORDER BY file, id", ([title, slug], file))
    backlinks = [{"id": r["file"], "label": r["title"]} for r in cur.fetchall()]
    conn.close()
    return {"id": file, "title": title, "category": category, "body": body.strip(),
            "tags": tags, "backlinks": backlinks}


class NoteIn(BaseModel):
    category: str
    title: str
    text: str
    file: str | None = None
    tags: list[str] | None = None


@app.post("/api/note")
def save_note(n: NoteIn):
    title = n.title.strip()
    cat = (n.category.strip() or "uncategorized")
    if not title:
        raise HTTPException(400, "title required")
    rel = n.file or os.path.join(cat, slugify(title) + ".md")
    path = safe_md_path(rel)
    # Preserve existing frontmatter (tags, dates, custom fields); only title and
    # category are edited here — never silently drop the rest of the note's metadata.
    extra = []
    if n.file and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
        if old.startswith("---"):
            end = old.find("\n---", 3)
            if end != -1:
                for line in old[3:end].splitlines():
                    key = line.split(":", 1)[0].strip() if ":" in line else ""
                    # drop title/category (re-set below); drop tags only if the client sent new ones
                    if key and key not in ("title", "category") and not (key == "tags" and n.tags is not None):
                        extra.append(line)
    front_lines = [f"title: {title}", f"category: {cat}"]
    if n.tags is not None:
        front_lines.append("tags: [" + ", ".join(t.strip() for t in n.tags if t.strip()) + "]")
    front = "\n".join(front_lines + extra)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{front}\n---\n\n{n.text.strip()}\n")
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


@app.delete("/api/note")
def delete_note(file: str):
    path = safe_md_path(file)
    if os.path.exists(path):
        os.remove(path)
    conn = connect()
    conn.execute("DELETE FROM chunks WHERE file = %s", (file,))
    conn.execute("DELETE FROM files  WHERE file = %s", (file,))
    conn.close()
    _git("add", "-A")
    _git("commit", "-m", f"delete: {file}")
    return {"ok": True}


@app.delete("/api/tag")
def delete_tag(name: str):
    """Remove a tag from every note that carries it (rewrite frontmatter), reindex those
    files, and drop any node-tag promotion. Notes themselves are kept."""
    name = name.strip()
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT file FROM chunks WHERE %s = ANY(tags)", (name,))
    rels = [r[0] for r in cur.fetchall()]
    changed = []
    tag_re = re.compile(r'^tags:\s*\[(.*)\]\s*$', re.M)
    for rel in rels:
        path = safe_md_path(rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            s = f.read()
        m = tag_re.search(s)
        if not m:
            continue
        items = [x.strip() for x in m.group(1).split(",") if x.strip()]
        if name not in items:
            continue
        items = [x for x in items if x != name]
        s2 = s[:m.start()] + "tags: [" + ", ".join(items) + "]" + s[m.end():]
        with open(path, "w", encoding="utf-8") as f:
            f.write(s2)
        changed.append(path)
    conn.execute("DELETE FROM node_tags WHERE tag = %s", (name,))
    if changed:
        index.reindex_files(cur, changed)
        for path in changed:
            r = os.path.relpath(path, NOTES_DIR)
            cur.execute("""INSERT INTO files (file, hash, updated_at) VALUES (%s, %s, now())
                           ON CONFLICT (file) DO UPDATE SET hash = EXCLUDED.hash, updated_at = now()""",
                        (r, index.file_hash(path)))
        _git("add", "-A")
        _git("commit", "-m", f"delete tag: {name} ({len(changed)} notes)")
    conn.close()
    return {"ok": True, "files": len(changed)}


# ---------- graph ----------
@app.get("/api/graph")
def graph():
    """Nodes (one per note file) and links (from [[wikilinks]]) for the 3D view."""
    conn = connect()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT DISTINCT ON (file) file, title, category, node_type, tags FROM chunks ORDER BY file, id")
    base = cur.fetchall()
    cur.execute("SELECT file, array_agg(DISTINCT l) AS links "
                "FROM chunks CROSS JOIN LATERAL unnest(links) AS l GROUP BY file")
    links_by_file = {r["file"]: r["links"] for r in cur.fetchall()}
    cur.execute("SELECT tag, category FROM node_tags")
    node_tag_cats = {r["tag"]: r["category"] for r in cur.fetchall()}
    conn.close()
    title_to_file = {r["title"]: r["file"] for r in base}

    def slug(s):
        return re.sub(r"[^a-z0-9-]", "", s.lower())

    # brains link by slug (filename), not by title - so resolve [[X]] against both.
    slug_to_file = {slug(os.path.splitext(os.path.basename(r["file"]))[0]): r["file"] for r in base}
    # Level 1 leaves = 0.7; notes flagged `type: hub` in frontmatter become Level 2
    # hubs (16) — labeled, same size as node-tags. Categories=54, node-tags=16.
    nodes = {r["file"]: {"id": r["file"], "label": r["title"], "group": r["category"],
                         "tags": r["tags"] or [], "val": 16 if r["node_type"] == "hub" else 0.7}
             for r in base}
    # categories are nodes too: one hub per category; every note links to it.
    cats = sorted({r["category"] for r in base})
    for c in cats:
        nodes["cat:" + c] = {"id": "cat:" + c, "label": c, "group": c, "val": 54, "is_cat": True}
    # A wikilink to a category (its label, its raw folder name, or a slug of either) should
    # point at that category node — not spawn a duplicate empty node. e.g. [[longevity]] -> cat:Longevity.
    cat_key = {}
    for c in cats:
        cat_key[c] = "cat:" + c; cat_key[slug(c)] = "cat:" + c
    for raw, label in getattr(index, "CATEGORY_LABELS", {}).items():
        if "cat:" + label in nodes:
            for k in (raw, label, slug(raw), slug(label)):
                cat_key[k] = "cat:" + label
    links = [{"source": r["file"], "target": "cat:" + r["category"]} for r in base]
    for src, targets in links_by_file.items():
        for t in targets:
            dst = title_to_file.get(t) or slug_to_file.get(slug(t)) or cat_key.get(t) or cat_key.get(slug(t))
            if dst is None:                       # link to a note that doesn't exist (yet)
                dst = "ext:" + t
                nodes.setdefault(dst, {"id": dst, "label": t, "group": "(unresolved)", "val": 0.7})
            links.append({"source": src, "target": dst})
    # node-tags: a tag promoted to a node links every note carrying it to that node
    # (an existing note with the same title, otherwise a synthetic tag node).
    for tagname, tcat in node_tag_cats.items():
        target = title_to_file.get(tagname)
        if target is None:
            target = "tag:" + tagname
            nodes[target] = {"id": target, "label": tagname, "group": tcat or tagname,
                             "val": 16, "tags": []}
        elif target in nodes:
            nodes[target]["val"] = 16            # Level 2: an existing record used as a node-tag
        for r in base:
            if tagname in (r["tags"] or []):
                links.append({"source": r["file"], "target": target})
    # sizes: category node = 54 (largest), HUB_TAG/hub note = 16 (medium), everything else = 2.
    return {"nodes": list(nodes.values()), "links": links}


# ---------- search ----------
@app.get("/api/search")
def api_search(q: str, k: int = 10):
    return server.search(q, k=k)


# ---------- tags ----------
@app.get("/api/tags")
def tags_list():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT t, count(DISTINCT file) AS c FROM chunks, unnest(tags) AS t "
                "GROUP BY t ORDER BY c DESC")
    out = [{"tag": r[0], "count": r[1]} for r in cur.fetchall()]
    conn.close()
    return out


@app.get("/api/tag")
def tag_notes(name: str):
    conn = connect(); cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT file, title FROM (SELECT DISTINCT ON (file) file, title FROM chunks "
                "WHERE %s = ANY(tags) ORDER BY file, id) s ORDER BY title", (name,))
    out = [{"id": r["file"], "label": r["title"]} for r in cur.fetchall()]
    conn.close()
    return out


# ---------- node-tags (tags promoted to graph nodes) ----------
@app.get("/api/node-tags")
def node_tags_list():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT tag, category FROM node_tags ORDER BY tag")
    out = [{"tag": r[0], "category": r[1]} for r in cur.fetchall()]
    conn.close()
    return out


class TagIn(BaseModel):
    tag: str
    category: str | None = None


@app.post("/api/node-tags")
def node_tag_add(t: TagIn):
    name = t.tag.strip()
    if not name:
        raise HTTPException(400, "tag required")
    conn = connect()
    conn.execute("INSERT INTO node_tags (tag, category) VALUES (%s, %s) "
                 "ON CONFLICT (tag) DO UPDATE SET category = EXCLUDED.category", (name, t.category))
    conn.close()
    return {"ok": True}


@app.delete("/api/node-tags/{tag}")
def node_tag_del(tag: str):
    conn = connect()
    conn.execute("DELETE FROM node_tags WHERE tag = %s", (tag,))
    conn.close()
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/static/index.html")


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
