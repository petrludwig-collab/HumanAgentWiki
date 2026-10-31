"""Unit tests for the pure helpers (no database or model needed).

Run:  pip install -r requirements-dev.txt  &&  pytest
"""
import os
import pytest

import common
import index
import server
import web


# ---------- index.py ----------
def test_parse_frontmatter_basic():
    fm, body = index.parse_frontmatter("---\ntitle: Hello\ncategory: Books\n---\n\nBody text")
    assert fm["title"] == "Hello"
    assert fm["category"] == "Books"
    assert body.strip() == "Body text"


def test_parse_frontmatter_strips_quotes():
    fm, _ = index.parse_frontmatter('---\ntitle: "Quoted"\n---\nx')
    assert fm["title"] == "Quoted"


def test_parse_frontmatter_none():
    fm, body = index.parse_frontmatter("No frontmatter here")
    assert fm == {}
    assert body == "No frontmatter here"


def test_split_blocks_on_headings():
    blocks = index.split_blocks("intro\n## Alpha\naaa\n### Beta\nbbb")
    headers = [h for h, _ in blocks]
    assert "Alpha" in headers and "Beta" in headers


def test_category_of():
    assert index.category_of(os.path.join("Books", "x.md")) == "Books"
    assert index.category_of("root.md") == (os.path.basename(index.NOTES_DIR.rstrip(os.sep)) or "notes")


def test_link_regex():
    assert index.LINK_RE.findall("see [[Alpha]] and [[Beta]]") == ["Alpha", "Beta"]


def test_token_windows_respect_limit_and_overlap():
    windows = list(index.token_windows(list(range(10)), max_tokens=4, overlap=1))
    assert windows == [[0, 1, 2, 3], [3, 4, 5, 6], [6, 7, 8, 9]]
    assert all(len(window) <= 4 for window in windows)


def test_token_windows_clamps_overlap_for_small_windows():
    assert list(index.token_windows(list(range(3)), max_tokens=1, overlap=48)) == [[0], [1], [2]]


def test_token_windows_reject_invalid_size():
    with pytest.raises(ValueError):
        list(index.token_windows([1, 2], max_tokens=0, overlap=0))


class _WhitespaceTokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False, **kwargs):
        tokens = text.split()
        result = {"input_ids": tokens}
        if return_offsets_mapping:
            offsets, cursor = [], 0
            for token in tokens:
                start = text.index(token, cursor)
                offsets.append((start, start + len(token)))
                cursor = start + len(token)
            result["offset_mapping"] = offsets
        return result

    def decode(self, token_ids, skip_special_tokens=True):
        return " ".join(token_ids)


def test_split_long_chunks_keeps_every_embedding_within_model_limit():
    tokenizer = _WhitespaceTokenizer()
    chunk = {
        "title": "Long section",
        "text": "## Long section\n\noriginal text",
        "emb_prefix": "File title - Long section\n",
        "emb_body": " ".join(f"word{i}" for i in range(1100)),
        "text_prefix": "## Long section\n",
        "emb_text": "File title - Long section\n" + " ".join(f"word{i}" for i in range(1100)),
    }

    parts = index.split_long_chunks([chunk], tokenizer, model_limit=512)

    assert len(parts) == 3
    assert [part["title"] for part in parts] == ["Long section", "Long section", "Long section"]
    assert all(
        len(tokenizer(part["emb_text"], add_special_tokens=False)["input_ids"]) + 2 <= 512
        for part in parts
    )
    assert parts[0]["text"].startswith("## Long section\n")
    assert all("word0" not in part["text"] for part in parts[1:])
    assert " ".join(part["text"] for part in parts).count("word1099") == 1


def test_build_chunks_splits_on_line_boundaries_with_prefix_and_overlap():
    tokenizer = _WhitespaceTokenizer()
    lines = [" ".join(f"l{n}w{i}" for i in range(30)) for n in range(40)]  # 40 lines x 30 tokens
    body = "\n".join(lines)
    chunk = {"title": "Sec", "text": "## Sec\n" + body, "emb_prefix": "Note - Parent > Sec\n",
             "emb_body": body, "text_prefix": "## Sec\n", "emb_text": "Note - Parent > Sec\n" + body}

    parts = index.build_chunks([chunk], tokenizer, model_limit=512, chunk_tokens=200, overlap=20)

    assert len(parts) > 1
    for part in parts:
        assert len(part["emb_text"].split()) <= 200
        assert part["emb_text"].startswith("Note - Parent > Sec")   # context kept in every part
        assert part["text"].split("\n")[-1].endswith("w29")          # cut only at line ends
    assert parts[0]["text"].startswith("## Sec\n")
    assert "l0w29" in parts[1]["emb_text"].split("\n", 2)[1] or "w29" in parts[1]["emb_text"]
    stored = "\n".join(p["text"] for p in parts)
    assert all(stored.count(f"l{n}w0 ") == 1 for n in range(40))   # stored text never overlaps


def test_reference_section_is_kept_whole_but_embedded_without_urls():
    tokenizer = _WhitespaceTokenizer()
    body = "\n".join(f"[{i}] Source title {i} — https://example.com/{'x' * 50}/{i}" for i in range(300))
    chunk = {"title": "Zdroje", "text": "## Zdroje\n" + body, "emb_prefix": "Note - Zdroje\n",
             "emb_body": body, "text_prefix": "## Zdroje\n", "emb_text": "Note - Zdroje\n" + body,
             "reference": True}

    parts = index.build_chunks([chunk], tokenizer, model_limit=512)

    assert len(parts) == 1 and parts[0]["text"] == chunk["text"]
    assert "https://" not in parts[0]["emb_text"]
    assert len(parts[0]["emb_text"].split()) + 2 <= 512


def test_process_file_heading_path_and_reference_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "NOTES_DIR", str(tmp_path))
    note = tmp_path / "n.md"
    note.write_text("---\ntitle: Note\n---\n# Note\nLead paragraph text long enough.\n"
                    "## Parent\nParent body text long enough.\n### Child\nChild body text long enough.\n"
                    "## Zdroje\n[1] A source — https://example.com/a\n", encoding="utf-8")
    chunks = {c["title"]: c for c in index.process_file(str(note))}
    assert chunks["Note"]["emb_text"].startswith("Note\nLead paragraph")
    assert chunks["Note"]["text"].startswith("# Note")
    assert chunks["Child"]["emb_prefix"] == "Note - Parent > Child\n"
    assert chunks["Zdroje"]["reference"] and not chunks["Parent"]["reference"]


def test_file_hash_depends_on_chunker_version(tmp_path, monkeypatch):
    note = tmp_path / "n.md"
    note.write_text("x", encoding="utf-8")
    before = index.file_hash(str(note))
    monkeypatch.setattr(index, "CHUNKER_VERSION", "next")
    assert index.file_hash(str(note)) != before


def test_embedding_token_limit_uses_the_smallest_model_constraint():
    class _Config:
        max_position_embeddings = 4096

    class _Module:
        class auto_model:
            config = _Config()

    class _Model:
        max_seq_length = 8192
        tokenizer = type("Tokenizer", (), {"model_max_length": 1024})()

        def __getitem__(self, index):
            assert index == 0
            return _Module()

    assert common.embedding_token_limit(_Model()) == 1024


def test_configure_embedding_length_applies_operational_cap():
    class _Config:
        max_position_embeddings = 8192

    class _Module:
        class auto_model:
            config = _Config()

    class _Model:
        max_seq_length = 8192
        tokenizer = type("Tokenizer", (), {"model_max_length": 8192})()

        def __getitem__(self, index):
            return _Module()

    model = _Model()
    assert common.configure_embedding_length(model, requested_limit=1024) == 1024
    assert common.embedding_token_limit(model) == 1024


# ---------- web.py ----------
def test_slugify():
    assert web.slugify("Hello, World!") == "hello-world"
    assert web.slugify("   ") == "note"


@pytest.mark.parametrize("bad", ["../evil.md", "/abs/x.md", "notes.txt", "a/../../x.md"])
def test_safe_md_path_rejects(bad):
    with pytest.raises(Exception):
        web.safe_md_path(bad)


def test_safe_md_path_accepts_relative_md():
    p = web.safe_md_path("Books/note.md")
    assert p.endswith(os.path.join("Books", "note.md"))


# ---------- server.py (brain_write helpers) ----------

def test_server_slugify_and_safe_folder():
    assert server._slugify("My First Note!") == "my-first-note"
    assert server._slugify("   ") == "note"
    assert server._safe_folder("../../etc") == "etc"      # traversal collapses to a plain name
    assert server._safe_folder("") == "uncategorized"


@pytest.mark.parametrize("bad", ["../evil.md", "/etc/x.md", "a/../../evil.md", "note.txt"])
def test_server_safe_note_path_rejects(bad):
    with pytest.raises(ValueError):
        server._safe_note_path(bad)


def test_server_safe_note_path_accepts_relative_md():
    p = server._safe_note_path("Ideas/x.md")
    assert p.endswith(os.path.join("Ideas", "x.md"))
