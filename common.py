"""Shared helpers: database connection + embedding model.

Everything is configured via environment variables (see .env.example) — there are
no hardcoded paths or personal data anywhere in this project.

  NOTES_DIR     folder with your Markdown notes        (default: ./notes)
  DATABASE_URL  libpq connection string                (default: dbname=humanagentwiki)
  EMBED_MODEL   sentence-transformers model            (default: BAAI/bge-m3 — multilingual)
  EMBED_DIM     embedding dimension, must match model  (default: 1024)
  EMBED_MAX_TOKENS maximum tokens per embedding input  (default: 1024)
  MCP_HOST      MCP server bind host                   (default: 127.0.0.1)
  MCP_PORT      MCP server port                        (default: 8802)
  WEB_HOST      web UI bind host                       (default: 127.0.0.1)
  WEB_PORT      web UI port                            (default: 8808)
"""
import os
import functools

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
import psycopg
from pgvector.psycopg import register_vector

NOTES_DIR = os.path.abspath(os.path.expanduser(os.environ.get("NOTES_DIR", "./notes")))
DSN       = os.environ.get("DATABASE_URL", "dbname=humanagentwiki")
EMB_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
EMB_DIM   = int(os.environ.get("EMBED_DIM", "1024"))
EMBED_MAX_TOKENS = int(os.environ.get("EMBED_MAX_TOKENS", "1024"))
MCP_HOST  = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT  = int(os.environ.get("MCP_PORT", "8802"))
WEB_HOST  = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT  = int(os.environ.get("WEB_PORT", "8808"))


def connect():
    conn = psycopg.connect(DSN, autocommit=True)
    register_vector(conn)
    return conn


@functools.lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer
    import torch
    dev = ("cuda" if torch.cuda.is_available()
           else "mps" if torch.backends.mps.is_available() else "cpu")
    model = SentenceTransformer(EMB_MODEL, device=dev)
    configure_embedding_length(model)
    return model


def _model_token_limit(model):
    """Return the smallest finite sequence limit advertised by a model."""
    candidates = [model.max_seq_length, model.tokenizer.model_max_length]
    config = getattr(getattr(model[0], "auto_model", None), "config", None)
    if config is not None:
        candidates.append(getattr(config, "max_position_embeddings", None))
    limits = [int(value) for value in candidates
              if value is not None and 0 < int(value) < 1_000_000]
    if not limits:
        raise ValueError("configured embedding model has no finite token limit")
    return min(limits)


def configure_embedding_length(model, requested_limit=EMBED_MAX_TOKENS):
    """Apply a configurable operational cap without exceeding model capability."""
    if requested_limit < 1:
        raise ValueError("EMBED_MAX_TOKENS must be positive")
    model.max_seq_length = min(_model_token_limit(model), requested_limit)
    return model.max_seq_length


def embedding_token_limit(model=None):
    """Return the usable sequence limit for the currently configured model."""
    return _model_token_limit(model or get_model())


def embed(texts, batch_size=16):
    """Return a list of normalized embedding vectors."""
    if isinstance(texts, str):
        texts = [texts]
    return get_model().encode(texts, batch_size=batch_size,
                              normalize_embeddings=True, show_progress_bar=False).tolist()
