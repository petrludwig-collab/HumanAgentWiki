#!/usr/bin/env python3
"""Health checks for a HumanAgentWiki instance (DB, embeddings, indexes, incremental sync).
   python cli.py selftest   (or: python selftest.py)   exit 0 = all OK, 1 = a check failed.
"""
import os
import datetime
from common import connect, EMB_DIM, NOTES_DIR
import index

RESULTS = []
def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"exception: {e}"
    RESULTS.append((name, ok, detail))


def c_connect():
    conn = connect(); conn.cursor().execute("SELECT 1"); conn.close()
    return True, "OK"

def c_extensions():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT extname FROM pg_extension"); ext = {r[0] for r in cur.fetchall()}; conn.close()
    miss = {"vector", "pg_trgm", "unaccent"} - ext
    return (not miss), ("present" if not miss else f"MISSING: {miss}")

def c_rowcount():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT count(*) FROM chunks"); n = cur.fetchone()[0]; conn.close()
    return (n > 0), f"{n} chunks (run `cli.py index` if 0)"

def c_no_null_embedding():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL"); n = cur.fetchone()[0]; conn.close()
    return (n == 0), f"{n} rows without embedding"

def c_embedding_dim():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT vector_dims(embedding) FROM chunks WHERE embedding IS NOT NULL LIMIT 1")
    row = cur.fetchone(); conn.close()
    if not row:
        return True, "no rows yet"
    return (row[0] == EMB_DIM), f"dim={row[0]} (expected {EMB_DIM})"

def c_tsv():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT count(*) FROM chunks WHERE tsv IS NULL"); n = cur.fetchone()[0]; conn.close()
    return (n == 0), f"{n} rows without full-text"

def c_indexes():
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='chunks'")
    idx = {r[0] for r in cur.fetchall()}; conn.close()
    miss = {"chunks_hnsw", "chunks_tsv", "chunks_links", "chunks_trgm"} - idx
    return (not miss), ("present" if not miss else f"MISSING: {miss}")

def c_registry_in_sync():
    """After indexing, the file registry must match disk exactly (drift = 0)."""
    disk = {os.path.relpath(p, NOTES_DIR): index.file_hash(p) for p in index.list_files()}
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT file, hash FROM files"); db = dict(cur.fetchall()); conn.close()
    changed = [f for f in disk if disk[f] != db.get(f)]
    stale = [f for f in db if f not in disk]
    drift = len(changed) + len(stale)
    return (drift == 0), (f"in sync ({len(disk)} files)" if drift == 0
                          else f"DRIFT: {len(changed)} changed/new, {len(stale)} stale - run `cli.py index`")

def c_chunks_have_registry():
    conn = connect(); cur = conn.cursor()
    cur.execute("""SELECT count(DISTINCT c.file) FROM chunks c
                   WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.file = c.file)""")
    orphan = cur.fetchone()[0]; conn.close()
    return (orphan == 0), f"{orphan} files with chunks but no registry row"


CHECKS = [
    ("DB connection", c_connect),
    ("Extensions (vector/pg_trgm/unaccent)", c_extensions),
    ("Chunk count > 0", c_rowcount),
    ("No NULL embeddings", c_no_null_embedding),
    (f"Embedding dim == {EMB_DIM}", c_embedding_dim),
    ("Full-text (tsv) populated", c_tsv),
    ("Indexes (HNSW + GIN)", c_indexes),
    ("File registry in sync with disk", c_registry_in_sync),
    ("Chunks have a registry row", c_chunks_have_registry),
]


def run():
    for name, fn in CHECKS:
        check(name, fn)
    failed = [r for r in RESULTS if not r[1]]
    print(f"=== HumanAgentWiki selftest {datetime.datetime.now():%Y-%m-%d %H:%M} ===")
    for name, ok, detail in RESULTS:
        print(f"  {'OK ' if ok else 'FAIL'} {name}: {detail}")
    print(f"--- {len(RESULTS) - len(failed)}/{len(RESULTS)} OK ---")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
