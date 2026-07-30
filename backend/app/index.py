"""L1 — chunk, embed, and retrieve with citations.

**L1 is the real risk checkpoint** (docs/design.md 6). If retrieval is bad, every
extraction pass built on top of it is bad — and you find that out here, before writing
a single generation prompt. So this module ends in a query that returns *cited
passages* and no generated text at all: what you read is what the index actually
holds, not a model's summary of it.

Chunking splits on paragraph boundaries first and only falls back to hard character
cuts, because a chunk that ends mid-sentence embeds badly and quotes worse.

The vector store is a float32 blob per row plus brute-force cosine in numpy. For one
book (a few thousand chunks) that is a sub-10 ms matrix multiply, and it has no
extension to install, no index to rebuild and no second query language. sqlite-vec is
the documented upgrade path if a merged multi-book corpus ever outgrows it.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import db, logs, ollama
from .config import CHUNK_CHARS, CHUNK_OVERLAP


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
               ) -> list[tuple[str, int, int]]:
    """Split into `(text, char_start, char_end)` windows over the chapter.

    Offsets are into the chapter string so a hit can be quoted back to its exact
    passage — a citation that can't be located isn't a citation.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [(text, 0, len(text))]

    out: list[tuple[str, int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Prefer a paragraph break, then a sentence end, then a space — but only
            # if it falls in the last third, otherwise we'd produce runt chunks.
            floor = start + (size * 2) // 3
            cut = text.rfind("\n\n", floor, end)
            if cut == -1:
                for punct in (". ", "! ", "? ", ".\n", "\n"):
                    cut = max(cut, text.rfind(punct, floor, end))
                if cut != -1:
                    cut += 1
            if cut == -1:
                cut = text.rfind(" ", floor, end)
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            out.append((piece, start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def build_chunks(book_id: int, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> int:
    """(Re)build the chunk rows for a book from its chapters. Embeddings are left
    NULL; `embed_pending` fills them. Returns the chunk count."""
    with db.connect() as conn:
        chapters = conn.execute(
            "SELECT id, position, text FROM chapters WHERE book_id = ? ORDER BY position",
            (book_id,),
        ).fetchall()
        conn.execute("DELETE FROM chunks WHERE book_id = ?", (book_id,))

        rows: list[tuple[Any, ...]] = []
        pos = 0
        for ch in chapters:
            for piece, cs, ce in chunk_text(ch["text"], size, overlap):
                rows.append((book_id, ch["id"], pos, piece, cs, ce))
                pos += 1
        if rows:
            conn.executemany(
                "INSERT INTO chunks (book_id, chapter_id, position, text, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
    invalidate(book_id)
    logs.info("local", f"chunked into {len(rows)} piece(s)", book_id=book_id,
              chapters=len(chapters), size=size, overlap=overlap)
    return len(rows)


def list_chunks(book_id: int, chunk_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Chunks with their chapter context, for extraction (L2).

    Note this does NOT require embeddings — extraction reads the text and the prefilter
    picks the targets, so L2 can run on a parsed book whose index was never built.
    """
    sql = ("SELECT c.id, c.position, c.text, c.char_start, c.char_end,"
           "       ch.position AS chapter_position, ch.title AS chapter_title, ch.source_ref"
           "  FROM chunks c JOIN chapters ch ON ch.id = c.chapter_id"
           " WHERE c.book_id = ?")
    args: list[Any] = [book_id]
    if chunk_ids:
        sql += f" AND c.id IN ({','.join('?' * len(chunk_ids))})"
        args.extend(chunk_ids)
    sql += " ORDER BY c.position"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def pending_count(book_id: int) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE book_id = ? AND embedding IS NULL",
            (book_id,),
        ).fetchone()
    return int(row["n"])


def total_count(book_id: int) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE book_id = ?", (book_id,)).fetchone()
    return int(row["n"])


async def embed_pending(book_id: int, model: str, batch: int) -> tuple[int, int]:
    """Embed one batch of not-yet-embedded chunks.

    Deliberately does ONE batch per call: the job worker ticks it repeatedly, so
    progress is persisted between batches and a container restart mid-index resumes
    from the last completed batch instead of re-embedding the whole book.

    Returns `(embedded_now, dims)`.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, text FROM chunks WHERE book_id = ? AND embedding IS NULL"
            " ORDER BY position LIMIT ?",
            (book_id, batch),
        ).fetchall()
    if not rows:
        return 0, 0

    vectors = await ollama.embed([r["text"] for r in rows], model=model)
    dims = len(vectors[0])

    with db.connect() as conn:
        conn.executemany(
            "UPDATE chunks SET embedding = ? WHERE id = ?",
            [(np.asarray(v, dtype=np.float32).tobytes(), r["id"])
             for r, v in zip(rows, vectors)],
        )
    return len(rows), dims


# Loaded matrices, keyed by book. A 400-chapter webnovel is roughly 3-4k chunks
# (~10 MB at 768 dims), so holding one book resident is cheap — but rebuilding it
# from SQLite on every keystroke-driven query is not. The cache key includes the
# embedded count and model, so a reindex invalidates it without any explicit
# bookkeeping: change the index, get a different key, load fresh.
_CACHE: dict[int, tuple[tuple[int, str], np.ndarray, list[dict[str, Any]]]] = {}


def _cache_key(book_id: int, model: str) -> tuple[int, str]:
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE book_id = ? AND embedding IS NOT NULL",
            (book_id,),
        ).fetchone()["n"]
    return int(n), model


def invalidate(book_id: int) -> None:
    _CACHE.pop(book_id, None)


def _matrix(book_id: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Load every embedded chunk as an L2-normalised matrix plus its metadata."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.position, c.text, c.char_start, c.char_end,"
            "       ch.position AS chapter_position, ch.title AS chapter_title,"
            "       ch.source_ref, c.embedding"
            "  FROM chunks c JOIN chapters ch ON ch.id = c.chapter_id"
            " WHERE c.book_id = ? AND c.embedding IS NOT NULL"
            " ORDER BY c.position",
            (book_id,),
        ).fetchall()
    if not rows:
        return np.zeros((0, 0), dtype=np.float32), []

    mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    meta = [
        {"chunk_id": r["id"], "position": r["position"], "text": r["text"],
         "char_start": r["char_start"], "char_end": r["char_end"],
         "chapter_position": r["chapter_position"], "chapter_title": r["chapter_title"],
         "source_ref": r["source_ref"]}
        for r in rows
    ]
    return mat / norms, meta


async def query(book: dict[str, Any], question: str, k: int = 6) -> dict[str, Any]:
    """Retrieve the k most similar passages, each with a citation.

    No generation. The point of L1 is to see the raw retrieval, because a model
    summarising bad passages hides exactly the failure you're testing for.
    """
    book_id = book["id"]
    indexed_model = book.get("embed_model") or ""
    if not indexed_model:
        raise ValueError("this book has no index yet — run Build index first")

    key = _cache_key(book_id, indexed_model)
    cached = _CACHE.get(book_id)
    if cached and cached[0] == key:
        _, mat, meta = cached
    else:
        mat, meta = _matrix(book_id)
        _CACHE[book_id] = (key, mat, meta)
    if mat.shape[0] == 0:
        raise ValueError("this book has no embedded chunks — run Build index first")

    qvec = np.asarray(await ollama.embed_one(question, model=indexed_model), dtype=np.float32)
    if qvec.shape[0] != mat.shape[1]:
        # Belt and braces: the model was swapped under an existing index. Refuse
        # rather than return confident nonsense.
        raise ValueError(
            f"embedding dimension mismatch ({qvec.shape[0]} vs {mat.shape[1]}) — the "
            f"index was built with '{indexed_model}'. Reindex to change model."
        )
    qnorm = np.linalg.norm(qvec) or 1.0
    scores = mat @ (qvec / qnorm)

    k = max(1, min(int(k), len(meta)))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]

    hits = []
    for i in top:
        m = dict(meta[int(i)])
        m["score"] = round(float(scores[int(i)]), 4)
        m["citation"] = _citation(m)
        hits.append(m)

    logs.info("process", f"query returned {len(hits)} passage(s)", book_id=book_id,
              model=indexed_model, top_score=hits[0]["score"] if hits else 0)
    return {"question": question, "model": indexed_model, "hits": hits,
            "searched": int(mat.shape[0])}


def _citation(m: dict[str, Any]) -> str:
    """Human-readable provenance: chapter, then whatever the source format can offer
    (an EPUB href, a PDF page range), then the character offsets."""
    parts = [f"ch. {m['chapter_position']}"]
    if m.get("chapter_title"):
        parts.append(f"“{m['chapter_title']}”")
    if m.get("source_ref"):
        parts.append(str(m["source_ref"]))
    parts.append(f"chars {m['char_start']}–{m['char_end']}")
    return " · ".join(parts)


def report(book: dict[str, Any]) -> dict[str, Any]:
    """Written to `index/index-report.json` — what was indexed, with what, how big."""
    book_id = book["id"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(AVG(LENGTH(text)), 0) AS avg_chars,"
            "       COALESCE(MIN(LENGTH(text)), 0) AS min_chars,"
            "       COALESCE(MAX(LENGTH(text)), 0) AS max_chars"
            "  FROM chunks WHERE book_id = ? AND embedding IS NOT NULL",
            (book_id,),
        ).fetchone()
        per_chapter = conn.execute(
            "SELECT ch.position, ch.title, COUNT(c.id) AS chunks"
            "  FROM chapters ch LEFT JOIN chunks c ON c.chapter_id = ch.id"
            " WHERE ch.book_id = ? GROUP BY ch.id ORDER BY ch.position",
            (book_id,),
        ).fetchall()
    return {
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "embed_model": book.get("embed_model", ""),
        "dims": book.get("embed_dims", 0),
        "chunk_chars": book.get("chunk_chars", 0),
        "chunk_overlap": book.get("chunk_overlap", 0),
        "chunks_embedded": int(row["n"]),
        "chunk_size_chars": {"avg": round(float(row["avg_chars"]), 1),
                             "min": int(row["min_chars"]), "max": int(row["max_chars"])},
        "per_chapter": [{"position": r["position"], "title": r["title"], "chunks": r["chunks"]}
                        for r in per_chapter],
    }
