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
from common import connect, embed, embedding_token_limit, get_model, NOTES_DIR, EMBED_MAX_TOKENS

HEADER_RE = re.compile(r'^(#{2,3})\s+(.*)$')
LINK_RE   = re.compile(r'\[\[([^\]]+?)\]\]')
SKIP_DIRS = ('/.git/', '/.obsidian/', '/node_modules/')


def link_targets(text):
    """[[wikilink]] targets with the |alias and #heading stripped: [[a|b]] -> 'a', [[a#h]] -> 'a'.
    Without this the graph stored 'a|b' as the whole target and never matched it, producing
    false 'unresolved' nodes even when the target note exists. Also de-duplicates."""
    seen, out = set(), []
    for m in LINK_RE.findall(text):
        t = m.split('|', 1)[0].split('#', 1)[0].strip()
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out


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
# Chunk budget in TOKENS of the embedding model's tokenizer (not characters), kept below
# EMBED_MAX_TOKENS so a chunk never reaches the hard model cap where sentence-transformers
# silently truncates. Overlap is only added to the embedded text, never to stored text.
EMBED_CHUNK_TOKENS = int(os.environ.get("EMBED_CHUNK_TOKENS", "480"))
EMBED_OVERLAP_TOKENS = int(os.environ.get("EMBED_OVERLAP_TOKENS", "48"))
# Pure reference-list sections ("## Zdroje" full of URLs). They stay in the index (full
# text, keyword search, brain_get) but are embedded only as a compact URL-free list and
# are left out of the vector ranking in server.search, so citation lists no longer
# outrank the content they support.
REFERENCE_SECTIONS = frozenset(s.strip().lower() for s in os.environ.get(
    "REFERENCE_SECTIONS", "zdroje,sources,references,reference,literatura,odkazy,bibliography"
).split(",") if s.strip())
URL_RE = re.compile(r'<?https?://\S+>?')
# Bump whenever the chunking / embedding input changes: it is mixed into the stored file
# hash, so the next ordinary (incremental) `index` run re-embeds every note.
CHUNKER_VERSION = "2-token-aware"


def is_reference_section(heading):
    return bool(heading) and heading.strip().lower() in REFERENCE_SECTIONS


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


def split_sections(body):
    """Split body on ## / ### headings into (heading_path, heading|None, content); the
    path lists the enclosing headings, e.g. ['Setup', 'Linux'] for a ### under a ##."""
    sections, stack, cur_h, buf = [], [], None, []
    def flush():
        c = '\n'.join(buf).strip()
        if c or cur_h:
            sections.append(([h for _, h in stack], cur_h, c))
    for ln in body.split('\n'):
        m = HEADER_RE.match(ln)
        if m:
            flush()
            level, cur_h, buf = len(m.group(1)), m.group(2).strip(), []
            stack = [s for s in stack if s[0] < level] + [(level, cur_h)]
        else:
            buf.append(ln)
    flush()
    return sections


def split_blocks(body):
    """Split body into (heading|None, content) blocks on ## / ### headings."""
    return [(h, c) for _, h, c in split_sections(body)]


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
    for path_h, header, content in split_sections(body):
        full = (header + '\n' + content).strip() if header else content.strip()
        if len(full) < MIN_CHUNK_CHARS:
            continue
        title = header.strip() if header else f_title
        links = link_targets(full)
        text_prefix = (header + "\n") if header else ""
        if header:  # note title + heading path, so every (split) chunk keeps its context
            emb_prefix, emb_body = f"{f_title} - {' > '.join(path_h)}\n", content
        else:       # lead block: note title as prefix; drop an H1 that only repeats it
            emb_prefix, lines = f"{f_title}\n", content.strip().split('\n')
            if lines and lines[0].startswith('# ') and lines[0][2:].strip() == f_title:
                text_prefix, lines = lines[0] + "\n", lines[1:]
            emb_body = '\n'.join(lines).strip()
        emb_text = f"{emb_prefix}{emb_body}".strip()
        out.append(dict(file=rel, category=category, node_type=node_type, title=title[:200],
                        links=links, tags=tags, text=full, meta=json.dumps(fm, ensure_ascii=False),
                        emb_prefix=emb_prefix, emb_body=emb_body, text_prefix=text_prefix,
                        emb_text=emb_text, reference=is_reference_section(header)))
    if not out:  # short note (title + a couple of links): still emit one node so it
        text = (f_title + "\n" + body).strip() or f_title   # appears and links to it resolve
        out.append(dict(file=rel, category=category, node_type=node_type, title=f_title[:200],
                        links=link_targets(body), tags=tags, text=text,
                        meta=json.dumps(fm, ensure_ascii=False),
                        emb_prefix="", emb_body=text, text_prefix="", emb_text=text,
                        reference=False))
    return out


def token_windows(token_ids, max_tokens, overlap=EMBED_OVERLAP_TOKENS):
    """Yield overlapping windows that never exceed the embedding token limit."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    overlap = max(0, min(overlap, max_tokens - 1))
    for start in range(0, len(token_ids), max_tokens - overlap):
        window = token_ids[start:start + max_tokens]
        if not window:
            break
        yield window
        if start + max_tokens >= len(token_ids):
            break


def _tok(tokenizer, text, **kw):
    try:  # verbose=False: we measure on purpose, no "longer than maximum" warning
        return tokenizer(text, add_special_tokens=False, verbose=False, **kw)
    except TypeError:
        return tokenizer(text, add_special_tokens=False, **kw)


def _count(tokenizer, text):
    return len(_tok(tokenizer, text)['input_ids'])


def _hard_split(tokenizer, line, budget):
    """Contiguous, source-accurate slices of one over-long line, each <= budget tokens."""
    offsets = _tok(tokenizer, line, return_offsets_mapping=True).get('offset_mapping')
    if offsets is None:
        raise ValueError("embedding tokenizer must provide offset mappings for long lines")
    pieces, start, char_start = [], 0, 0
    while start < len(offsets):
        end = min(start + budget, len(offsets))
        while True:
            char_end = offsets[end - 1][1] if end < len(offsets) else len(line)
            piece = line[char_start:char_end]
            if end - start <= 1 or _count(tokenizer, piece) <= budget:
                break
            end -= 1
        pieces.append(piece)
        start, char_start = end, char_end
    return pieces


def _segments(tokenizer, body, budget):
    """Greedily pack whole lines into contiguous, non-overlapping segments of <= budget
    tokens; only a single line longer than the budget is cut on token boundaries."""
    units = []
    for ln in body.split('\n'):
        if _count(tokenizer, ln) > budget:
            pieces = _hard_split(tokenizer, ln, budget)
            units += [('\n', pieces[0])] + [('', p) for p in pieces[1:]]
        else:
            units.append(('\n', ln))
    segs, cur = [], None
    for sep, unit in units:
        cand = unit if cur is None else cur + sep + unit
        if cur is not None and _count(tokenizer, cand) > budget:
            segs.append(cur); cur = unit
        else:
            cur = cand
    if cur is not None:
        segs.append(cur)
    return [s for s in segs if s.strip()]


def build_chunks(chunks, tokenizer, model_limit, chunk_tokens=None, overlap=None):
    """Make every embedding input fit the token budget (min(EMBED_CHUNK_TOKENS, model
    limit - special tokens)), so the model never truncates. Long sections are split on
    line boundaries; each part keeps the note title + heading path as prefix and gets
    `overlap` tokens of the previous part (embedding only - stored text never overlaps)."""
    chunk_tokens = EMBED_CHUNK_TOKENS if chunk_tokens is None else chunk_tokens
    overlap = EMBED_OVERLAP_TOKENS if overlap is None else overlap
    payload = min(chunk_tokens, model_limit - tokenizer.num_special_tokens_to_add(pair=False))
    if payload < 1:
        raise ValueError("embedding model leaves no room for payload tokens")
    out = []
    for chunk in chunks:
        prefix = chunk.get('emb_prefix', '')
        body = chunk.get('emb_body', chunk['emb_text'])
        prefix_len = _count(tokenizer, prefix)
        if prefix_len > payload // 2:
            prefix, prefix_len = "", 0
        if chunk.get('reference'):  # stored whole; embed a compact URL-free summary
            lines = (URL_RE.sub('', ln).rstrip(' -\u2013\u2014:|') for ln in body.split('\n'))
            segs = _segments(tokenizer, '\n'.join(ln for ln in lines if ln.strip()),
                             payload - prefix_len)
            part = dict(chunk)
            part['emb_text'] = f"{prefix}{segs[0] if segs else ''}".strip() or chunk['title']
            out.append(part)
            continue
        if _count(tokenizer, chunk['emb_text']) <= payload:
            out.append(chunk)
            continue
        ov = max(0, min(overlap, (payload - prefix_len) // 4))
        prev = None
        for number, seg in enumerate(_segments(tokenizer, body, payload - prefix_len - ov)):
            emb = f"{prefix}{seg}"
            if prev is not None and ov:
                ids = _tok(tokenizer, prev)['input_ids'][-ov:]
                while ids:
                    tail = tokenizer.decode(ids, skip_special_tokens=True).strip()
                    emb = f"{prefix}{tail}\n{seg}"
                    if _count(tokenizer, emb) <= payload:
                        break
                    ids, emb = ids[1:], f"{prefix}{seg}"
            part = dict(chunk)
            part['text'] = f"{chunk.get('text_prefix', '') if number == 0 else ''}{seg}".strip()
            part['links'] = link_targets(part['text'])
            part['emb_text'] = emb.strip()
            out.append(part)
            prev = seg
    return out


split_long_chunks = build_chunks  # backwards-compatible name


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
    """Content hash + chunker settings: changing how notes are chunked/embedded makes
    every file count as changed, so an incremental run rebuilds the whole index."""
    h = hashlib.sha256(f"chunker={CHUNKER_VERSION};tokens={EMBED_CHUNK_TOKENS};"
                       f"overlap={EMBED_OVERLAP_TOKENS};max={EMBED_MAX_TOKENS}\n".encode())
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
    vecs = []
    if chunks:
        model = get_model()
        chunks = build_chunks(chunks, model.tokenizer, embedding_token_limit(model))
        vecs = embed([c['emb_text'] for c in chunks], batch_size=8)
    # Swap old -> new rows only after the (slow, CPU) embedding finished, in one
    # transaction, so searches never see a half-empty index during a re-embed.
    with cur.connection.transaction():
        if rels:
            cur.execute("DELETE FROM chunks WHERE file = ANY(%s)", (rels,))
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
