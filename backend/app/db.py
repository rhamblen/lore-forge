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

-- L2: extracted progression rules, one row per distinct mechanic after merging.
--
-- Rows, not just the emitted `campaign/rules/system.json`, because the curation step
-- (keep / discard / edit) needs stable ids, and because a re-run must be able to merge
-- into what is already there rather than starting over. The JSON file is the *export*;
-- this table is the truth, in keeping with the standing principle.
--
-- `citations_json` is a list of {chapter, source_ref, chunk_id, char_start, char_end}.
-- It is denormalised on purpose: a citation must survive its chunk being deleted by a
-- reindex, or the rule silently loses its provenance.
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    rule_key    TEXT NOT NULL,                     -- kind:slug(name); the merge identity
    kind        TEXT NOT NULL DEFAULT 'mechanic',  -- xp|level|skill|class|currency|cap|penalty|mechanic
    name        TEXT NOT NULL,
    statement   TEXT NOT NULL DEFAULT '',          -- paraphrase, never verbatim source
    formula     TEXT NOT NULL DEFAULT '',
    confidence  TEXT NOT NULL DEFAULT 'implied',   -- stated | implied
    evidence_excerpt TEXT NOT NULL DEFAULT '',     -- short citation aid, hard-capped
    citations_json   TEXT NOT NULL DEFAULT '[]',
    -- Curation, the L2 deliverable alongside the dossiers: nothing is auto-published.
    status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | kept | discarded
    edited      INTEGER NOT NULL DEFAULT 0,        -- 1 once a human has changed the text
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_book_key ON rules(book_id, rule_key);
CREATE INDEX IF NOT EXISTS idx_rules_book ON rules(book_id, status);

-- L2/L3: world entities — places, factions, systems, artefacts, history, terminology.
-- These become the SillyTavern lorebook entries at L3.
--
-- `aliases_json` is load-bearing, not decoration: a lorebook entry fires only when one
-- of its keys appears in the chat, so a lost alias is a silently dead entry. Merging a
-- repeat mention UNIONS the aliases rather than replacing them.
CREATE TABLE IF NOT EXISTS lore_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    entry_key   TEXT NOT NULL,                     -- kind:slug(name); the merge identity
    kind        TEXT NOT NULL DEFAULT 'term',      -- location|faction|system|artefact|history|term
    name        TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    summary     TEXT NOT NULL DEFAULT '',          -- paraphrase, never verbatim source
    citations_json TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed | kept | discarded
    edited      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_book_key ON lore_entries(book_id, entry_key);
CREATE INDEX IF NOT EXISTS idx_entries_book ON lore_entries(book_id, status);

-- L2/L5: quests — the journey. A quest carries ITS OWN reward and penalty; those terms
-- are not system rules. Keeping them here rather than in `rules` is the direct fix for a
-- real extraction error, where one quest's failure penalty was recorded as a universal
-- law governing every quest.
CREATE TABLE IF NOT EXISTS quests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    quest_key    TEXT NOT NULL,                    -- slug(name); the merge identity
    name         TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    kind         TEXT NOT NULL DEFAULT 'unknown',  -- main|side|hidden|optional|tutorial|unknown
    objective    TEXT NOT NULL DEFAULT '',
    giver        TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    reward       TEXT NOT NULL DEFAULT '',
    penalty      TEXT NOT NULL DEFAULT '',
    deadline     TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT 'unknown',  -- accepted|completed|failed|declined|ongoing|unknown
    -- Position in the journey = where the book first mentions it.
    first_chapter INTEGER NOT NULL DEFAULT 0,
    citations_json TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'proposed', -- proposed | kept | discarded
    edited       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_quests_book_key ON quests(book_id, quest_key);
CREATE INDEX IF NOT EXISTS idx_quests_book ON quests(book_id, first_chapter);

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


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a no-op on
# an existing table, so a new column needs an explicit ALTER or an already-created
# database silently lacks it. Applied one at a time so a database that picked up some of
# them still lands complete — the same pattern Persona Forge uses.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # Rule aliases: a rule becomes a lorebook entry at L3, and an entry fires only on its
    # keys, so a mechanic the book abbreviates needs both spellings.
    "rules": [
        ("aliases_json", "TEXT NOT NULL DEFAULT '[]'"),
        # system = governs everything of its type; instance = one quest/item/contract.
        ("scope", "TEXT NOT NULL DEFAULT 'system'"),
        ("applies_to", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in _MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns:
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
