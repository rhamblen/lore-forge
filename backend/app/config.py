"""Environment and paths.

One module so every other module reads config from the same place, and so the two
layouts (container `/app/app/...` vs repo `backend/app/...`) resolve identically.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- external services -----------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.32:11434").rstrip("/")
# Embeddings (L1). The dimension is NOT configured — it is discovered from the model
# and recorded per book, because getting it wrong silently poisons retrieval.
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
# Generation (L2+). Unused at 0.1.x; declared now so the env contract is stable.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:12b")

# --- storage ---------------------------------------------------------------
LORE_BUILDS_ROOT = Path(os.getenv("LORE_BUILDS_ROOT", "/lore-builds"))
DB_DIR = Path(os.getenv("DB_DIR", "/data/db"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))

# Ownership for folders we create on the shared mount (Unraid nobody:users).
BUILD_UID = int(os.getenv("PUID", "99"))
BUILD_GID = int(os.getenv("PGID", "100"))

# --- indexing defaults -----------------------------------------------------
# Characters, not tokens: a chunker that needs a tokeniser needs a model download,
# and at book scale ~4 chars/token is close enough to size a window. Overlap exists
# so a fact stated across a paragraph break is retrievable from either side.
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1600"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
# How many chunks go to Ollama per HTTP call. Batching is a big win; too large a
# batch on a small GPU stalls. A 400-chapter webnovel is ~3-4k chunks, so at 32 the
# whole book is ~100 round trips rather than ~250.
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "32"))

# Upload guard. A doorstop novel EPUB is a few MB; 256 MB covers scanned PDFs.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))


def _resolve(name: str, must_be_dir: bool = True) -> Path:
    """Resolve a sibling asset in both layouts.

    Container: /app/app/config.py   -> /app/<name>
    Repo:      backend/app/config.py -> <repo-root>/<name>
    """
    here = Path(__file__).resolve()
    cands = [here.parent.parent / name, here.parent.parent.parent / name]
    for c in cands:
        if c.is_dir() if must_be_dir else c.is_file():
            return c
    return cands[0]


FRONTEND_DIR = _resolve("frontend")
_version_file = _resolve("VERSION", must_be_dir=False)
VERSION = _version_file.read_text().strip() if _version_file.is_file() else "0.0.0"


def _build_stamp() -> str:
    """A short hash that changes whenever the code changes.

    The version alone cannot answer "am I running your latest?" — during a run of
    local iteration the version deliberately stays put while the code moves underneath
    it. This hashes the size and mtime of every backend module and frontend asset, so
    any edit produces a different stamp.

    Deliberately cheap and dependency-free: no git required (the container has no repo),
    computed once at import.
    """
    import hashlib  # noqa: PLC0415 — startup-only

    h = hashlib.sha1(VERSION.encode())
    here = Path(__file__).resolve().parent
    targets = sorted(here.glob("*.py"))
    if FRONTEND_DIR.is_dir():
        targets += sorted(p for p in FRONTEND_DIR.iterdir() if p.is_file())
    for path in targets:
        try:
            st = path.stat()
            h.update(f"{path.name}:{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            continue
    return h.hexdigest()[:8]


BUILD = _build_stamp()
