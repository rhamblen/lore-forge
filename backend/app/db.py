"""SQLite store for books, their parsed chapters, and the retrieval index.

Two rules from `docs/design.md` govern every table here:

**Database = truth. LLM = storyteller.** The extracted text, its chapter structure and
its citations live in rows. A model is only ever asked to *read* them.

**No schema Persona Forge can't absorb** (merge-first rule 2). Every table below is
named as a PF table would be — `books`, `chapters`, `chunks`, `jobs` — and the `jobs`
table is byte-compatible with PF's own, except that `project_id` is `book_id`. At merge
time (L6) these become PF tables unchanged; the importer is a copy, not a rewrite.
"""

from __future__ import annotations

import sqlite3

from .config import DB_DIR

DB_PATH = DB_DIR / "lore_forge.sqlite3"

SCHEMA = """
-- One row per uploaded source. `slug` is the folder name under the lore-builds
-- root, so it is UNIQUE for the same reason PF's project slug is.
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    author      TEXT NOT NULL DEFAULT '',
    -- 'epub' | 'pdf' | 'txt', sniffed from the upload, not trusted from the name
    source_kind TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',     -- basename inside <slug>/sources/
    source_bytes INTEGER NOT NULL DEFAULT 0,
    -- sha256 of the original upload. The manifest records it so a re-upload of the
    -- same file is recognisable and an index can be tied to exact source bytes.
    source_sha  TEXT NOT NULL DEFAULT '',

    parse_status  TEXT NOT NULL DEFAULT 'none',   -- none | pending | done | error
    parse_message TEXT NOT NULL DEFAULT '',
    chapter_count INTEGER NOT NULL DEFAULT 0,
    word_count    INTEGER NOT NULL DEFAULT 0,

    index_status  TEXT NOT NULL DEFAULT 'none',   -- none | pending | done | error
    index_message TEXT NOT NULL DEFAULT '',
    -- The embedding model and its dimension are recorded per book because mixing
    -- dimensions in one index returns confident nonsense rather than an error. A
    -- query against a different model is refused, not coerced.
    embed_model   TEXT NOT NULL DEFAULT '',
    embed_dims    INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    chunk_chars   INTEGER NOT NULL DEFAULT 0,
    chunk_overlap INTEGER NOT NULL DEFAULT 0,

    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Chaptered text — the L0 deliverable. Text lives in the row AND on disk under
-- <slug>/sources/text/: the row is what the app reads, the file is what a human
-- reads to check the parse without a database client.
CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,               -- 1-based reading order
    title       TEXT NOT NULL DEFAULT '',
    -- Where it came from inside the container file (EPUB spine href / PDF page
    -- range). Kept because a citation that can't be traced back to the source is
    -- not a citation.
    source_ref  TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    word_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, position);

-- The retrieval index — the L1 deliverable. `embedding` is a raw float32 blob
-- (numpy tobytes), which is both compact and directly loadable into a matrix for
-- brute-force cosine. At book scale that is milliseconds; sqlite-vec is the upgrade
-- path if a merged multi-book corpus ever outgrows it (docs/design.md 7).
--
-- `char_start`/`char_end` are offsets into the CHAPTER text, not the book: that is
-- what makes a citation quotable back to its exact passage.
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id  INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,               -- 0-based, book-wide reading order
    text        TEXT NOT NULL,
    char_start  INTEGER NOT NULL DEFAULT 0,
    char_end    INTEGER NOT NULL DEFAULT 0,
    embedding   BLOB,                           -- float32[dims]; NULL until embedded
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_book ON chunks(book_id, position);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON chunks(chapter_id);

-- The job engine's table. Identical to Persona Forge's `jobs` except project_id ->
-- book_id, so the engine code is a straight port and the merge is a rename.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER REFERENCES books(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',    -- queued | running | done | error | canceled
    stage       TEXT NOT NULL DEFAULT '',          -- handler-defined current stage label
    params_json TEXT NOT NULL DEFAULT '{}',        -- inputs
    state_json  TEXT NOT NULL DEFAULT '{}',        -- handler scratch — resume-safe
    message     TEXT NOT NULL DEFAULT '',          -- human-readable status / last error
    progress    REAL NOT NULL DEFAULT 0,           -- 0..1
    result_json TEXT NOT NULL DEFAULT '{}',        -- outputs
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_book ON jobs(book_id, id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A parse job holding a write txn must not block the UI's reads.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
