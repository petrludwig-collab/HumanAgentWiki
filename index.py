"""Indexer: Markdown notes -> chunks -> embeddings -> pgvector.

INCREMENTAL by default: only files whose content changed (detected via a sha256
hash stored in the `files` table) are re-embedded. Unchanged files are skipped,
deleted files are removed from the index.

  python cli.py index           # incremental (fast - only changed files)
  python cli.py index --full    # full rebuild (re-embed everything)

A note's top-level folder under NOTES_DIR is its category (e.g. notes/Books/x.md
-> category "Books"); a `category:` field in YAML frontmatter overrides that.
Sections are split on `##` / `###` headings; `[[wikilinks]]` build the graph.
"""
import os
import re
import sys
import glob
import json
import time
import hashlib
from common import connect, embed, NOTES_DIR

HEADER_RE = re.compile(r'^(#{2,3})\s+(.*)$')
LINK_RE   = re.compile(r'\[\[([^\]]+?)\]\]')
SKIP_DIRS = ('/.git/', '/.obsidian/', '/node_modules/')
# Settings that must stay consistent across EVERY `index` run — even when invoked by hand
# without the usual env vars (the classic footgun: an incremental reindex with no
# CATEGORY_LABELS silently re-labels notes, or a --full run with the wrong INCLUDE_DIRS
# drops whole folders). Read them from a per-vault JSON config (`.haw.json` in NOTES_DIR,
# version-controlled with the notes). Explicit env vars still win. Keys: include_dirs (list),
# category_labels ({raw: "Display"}).
def _file_config():
    try:
        with open(os.path.join(NOTES_DIR, ".haw.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
_FCFG = _file_config()

# Optional allowlist of top-level folders to index, e.g. INCLUDE_DIRS='books,notes'.
# Empty = index everything under NOTES_DIR. Lets you grow a vault one section at a time.
_inc = os.environ.get("INCLUDE_DIRS")
if _inc is None:
    _inc = ",".join(_FCFG.get("include_dirs", []))
INCLUDE_DIRS = [d.strip() for d in _inc.split(",") if d.strip()]
MIN_CHUNK_CHARS = 25  # skip trivially short blocks (stray lines, empty sections)


def parse_frontmatter(text):
    """Minimal YAML-ish frontmatter parser (key: value lines). Returns (dict, body)."""
    fm = {}
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            return fm, text[end + 4:]
    return fm, text


def split_blocks(body):
    """Split body into (heading|None, content) blocks on ## / ### headings."""
    blocks, cur_h, buf = [], None, []
    def flush():
        c = '\n'.join(buf).strip()
        if c or cur_h:
            blocks.append((cur_h, c))
    for ln in body.split('\n'):
        m = HEADER_RE.match(ln)
        if m:
            flush(); cur_h = m.group(2).strip(); buf = []
        else:
            buf.append(ln)
    flush()
    return blocks


# Optional display names for categories, e.g. CATEGORY_LABELS='{"wiki":"Personal wiki"}'.
# Without it, the raw project/folder name is used with its first letter capitalized.
try:
    _lbl = os.environ.get("CATEGORY_LABELS")
    CATEGORY_LABELS = json.loads(_lbl) if _lbl is not None else _FCFG.get("category_labels", {})
except Exception:
    CATEGORY_LABELS = {}


def category_of(rel):
    """Top-level folder under NOTES_DIR = category; a file in the root uses the notes
    directory's own name."""
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else (os.path.basename(NOTES_DIR.rstrip(os.sep)) or "notes")


def category_label(raw):
    """Display name for a category: CATEGORY_LABELS override, else first letter capitalized."""
    return CATEGORY_LABELS.get(raw) or CATEGORY_LABELS.get(str(raw).lower()) or (raw[:1].upper() + raw[1:])


def process_file(path):
    rel = os.path.relpath(path, NOTES_DIR)
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    fm, body = parse_frontmatter(raw)
    f_title  = fm.get('title', os.path.splitext(os.path.basename(rel))[0])
    raw = fm.get('category') or fm.get('project') or category_of(rel)
    if str(raw).strip().lower() == 'uncategorized':   # placeholder -> fall back to project/folder
        raw = fm.get('project') or category_of(rel)
    category = category_label(raw)
    node_type = 'hub' if fm.get('type') == 'hub' else 'note'
    tags = [t.strip().strip('"').strip("'") for t in re.sub(r'[\[\]]', '', fm.get('tags', '')).split(',') if t.strip()]
    out = []
    for header, content in split_blocks(body):
        full = (header + '\n' + content).strip() if header else content.strip()
        if len(full) < MIN_CHUNK_CHARS:
            continue
        title = header.strip() if header else f_title
        links = LINK_RE.findall(full)
        emb_text = f"{f_title} - {title}\n{content}".strip() if title != f_title else full
        out.append(dict(file=rel, category=category, node_type=node_type, title=title[:200],
                        links=links, tags=tags, text=full, meta=json.dumps(fm, ensure_ascii=False),
                        emb_text=emb_text))
    if not out:  # short note (title + a couple of links): still emit one node so it
        text = (f_title + "\n" + body).strip() or f_title   # appears and links to it resolve
        out.append(dict(file=rel, category=category, node_type=node_type, title=f_title[:200],
                        links=LINK_RE.findall(body), tags=tags, text=text,
                        meta=json.dumps(fm, ensure_ascii=False), emb_text=text))
    return out


INSERT_SQL = """
    INSERT INTO chunks (file, category, node_type, title, links, tags, text, meta, embedding, tsv)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, to_tsvector('simple', unaccent(%s)))
"""


def list_files():
    files = [p for p in glob.glob(f"{NOTES_DIR}/**/*.md", recursive=True)
             if not any(s in p + '/' for s in SKIP_DIRS)]
    if INCLUDE_DIRS:
        files = [p for p in files
                 if os.path.relpath(p, NOTES_DIR).split(os.sep)[0] in INCLUDE_DIRS]
    return files


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def reindex_files(cur, abs_files):
    """Delete old chunks for these files and insert freshly embedded ones."""
    rels = [os.path.relpath(p, NOTES_DIR) for p in abs_files]
    chunks = []
    for p in abs_files:
        try:
            chunks += process_file(p)
        except Exception as e:
            print("  error", p, e)
    if rels:
        cur.execute("DELETE FROM chunks WHERE file = ANY(%s)", (rels,))
    if chunks:
        vecs = embed([c['emb_text'] for c in chunks], batch_size=8)
        for c, v in zip(chunks, vecs):
            cur.execute(INSERT_SQL, (c['file'], c['category'], c['node_type'], c['title'],
                                     c['links'], c['tags'], c['text'], c['meta'], v, c['text']))
    return len(chunks)


def run(full=False):
    conn = connect(); cur = conn.cursor()
    abs_files = list_files()
    disk = {os.path.relpath(p, NOTES_DIR): p for p in abs_files}
    disk_hash = {rel: file_hash(p) for rel, p in disk.items()}

    if full:
        print("Full rebuild...")
        cur.execute("TRUNCATE chunks RESTART IDENTITY")
        cur.execute("TRUNCATE files")
        changed = sorted(disk)
    else:
        cur.execute("SELECT file, hash FROM files")
        db_hash = dict(cur.fetchall())
        changed = [r for r in disk if disk_hash[r] != db_hash.get(r)]
        removed = [r for r in db_hash if r not in disk]
        if removed:
            cur.execute("DELETE FROM chunks WHERE file = ANY(%s)", (removed,))
            cur.execute("DELETE FROM files  WHERE file = ANY(%s)", (removed,))
            print(f"Removed {len(removed)} deleted file(s).")
        print(f"Files: {len(disk)} | changed/new: {len(changed)} | "
              f"unchanged: {len(disk) - len(changed)} (skipped)")

    if not changed:
        print("Nothing to do."); conn.close(); return

    t0 = time.time()
    print(f"Embedding {len(changed)} file(s)...")
    n = reindex_files(cur, [disk[r] for r in changed])
    for r in changed:
        cur.execute("""INSERT INTO files (file, hash, updated_at) VALUES (%s, %s, now())
                       ON CONFLICT (file) DO UPDATE SET hash = EXCLUDED.hash, updated_at = now()""",
                    (r, disk_hash[r]))
    cur.execute("SELECT count(*) FROM chunks")
    print(f"  done in {time.time() - t0:.1f}s - {n} chunks written, {cur.fetchone()[0]} total in index.")
    conn.close()


if __name__ == "__main__":
    run(full="--full" in sys.argv)
