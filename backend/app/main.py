"""Lore Forge API.

0.1.x is L0 + L1 of the phasing in `docs/design.md`:
  L0  intake + parse  -> clean chaptered text you can read
  L1  chunk + embed + index -> ask a question, get cited passages (no generation)

Extraction (L2), the lorebook (L3) and V3 cards (L4) land as new job handlers and new
folders under the existing `st-import/` + `campaign/` contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import builds, db, extract, index, jobs, logs, ollama, parse, rules_store, systext
from .config import (
    CHUNK_CHARS,
    CHUNK_OVERLAP,
    DB_DIR,
    EMBED_BATCH,
    EMBED_MODEL,
    FRONTEND_DIR,
    LOG_DIR,
    LORE_BUILDS_ROOT,
    MAX_UPLOAD_BYTES,
    OLLAMA_MODEL,
    OLLAMA_URL,
    VERSION,
)

app = FastAPI(title="Lore Forge", version=VERSION)


@app.middleware("http")
async def _log_requests(request: Any, call_next: Any):
    """Every inbound request, at verbose — the firehose for tracing a flow end-to-end."""
    if request.url.path.startswith("/static"):
        return await call_next(request)
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - t0) * 1000)
    logs.verbose("api", f"{request.method} {request.url.path} → {response.status_code}", ms=ms)
    return response


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _book(book_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no book with id {book_id}")
    return dict(row)


def _update_book(book_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE books SET {sets} WHERE id = ?", (*fields.values(), book_id))


def _chapters(book_id: int, with_text: bool = False) -> list[dict[str, Any]]:
    cols = "id, book_id, position, title, source_ref, word_count" + (", text" if with_text else "")
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM chapters WHERE book_id = ? ORDER BY position", (book_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _sync_manifest(book_id: int) -> None:
    book = _book(book_id)
    builds.write_manifest(book, _chapters(book_id))


# --------------------------------------------------------------------------- #
# job handlers
# --------------------------------------------------------------------------- #

class ParseHandler:
    """L0. One tick does the whole parse — it is local CPU work measured in seconds,
    so splitting it into stages would add resume complexity for no benefit."""

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        slug = book["slug"]
        src = builds.safe_path(slug, "sources", book["source_file"])
        if src is None or not src.is_file():
            _update_book(book["id"], parse_status="error", parse_message="source file missing")
            return jobs.ERROR, "source file missing on disk"

        jobs.set_stage(job["id"], "parsing", f"parsing {book['source_kind']}…", 0.1)
        try:
            # Parsing is blocking CPU work; keep the event loop (and the UI) alive.
            res = await asyncio.to_thread(parse.parse, src, book["source_kind"])
        except Exception as exc:  # noqa: BLE001
            _update_book(book["id"], parse_status="error", parse_message=str(exc)[:500])
            return jobs.ERROR, f"parse failed: {exc}"

        if not res.chapters:
            msg = "; ".join(res.warnings) or "no text could be extracted"
            _update_book(book["id"], parse_status="error", parse_message=msg[:500])
            return jobs.ERROR, msg

        jobs.set_stage(job["id"], "writing", f"writing {len(res.chapters)} chapter(s)…", 0.7)
        with db.connect() as conn:
            conn.execute("DELETE FROM chapters WHERE book_id = ?", (book["id"],))
            conn.executemany(
                "INSERT INTO chapters (book_id, position, title, source_ref, text, word_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(book["id"], i, c.title, c.source_ref, c.text, c.word_count)
                 for i, c in enumerate(res.chapters, start=1)],
            )
        for i, c in enumerate(res.chapters, start=1):
            builds.write_chapter_text(slug, i, c.title, c.text)

        # A parse invalidates any existing index — the chunks point at chapter rows
        # that no longer exist. Say so rather than leaving a stale index queryable.
        fields: dict[str, Any] = {
            "parse_status": "done",
            "parse_message": "; ".join(res.warnings)[:500],
            "chapter_count": len(res.chapters),
            "word_count": res.word_count,
        }
        if book["index_status"] == "done":
            fields.update(index_status="none", index_message="reindex needed after reparse",
                          chunk_count=0, embed_model="", embed_dims=0)
        if res.title and not book["title"].strip():
            fields["title"] = res.title
        if res.author and not book["author"]:
            fields["author"] = res.author
        _update_book(book["id"], **fields)

        updated = _book(book["id"])
        builds.write_report(slug, "review", "parse-report.json", parse.report(res, updated))
        _sync_manifest(book["id"])

        jobs.set_result(job["id"], {"chapters": len(res.chapters), "words": res.word_count,
                                    "method": res.method, "warnings": res.warnings})
        note = f" ({len(res.warnings)} warning(s))" if res.warnings else ""
        return jobs.DONE, f"{len(res.chapters)} chapters, {res.word_count:,} words{note}"


class IndexHandler:
    """L1. Chunks once, then embeds ONE batch per tick so progress persists and a
    restart mid-index resumes from the last completed batch."""

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        params = jobs.params_of(job)
        state = jobs.state_of(job)
        model = params.get("model") or EMBED_MODEL
        size = int(params.get("chunk_chars") or CHUNK_CHARS)
        overlap = int(params.get("chunk_overlap") or CHUNK_OVERLAP)
        batch = int(params.get("batch") or EMBED_BATCH)

        if book["parse_status"] != "done":
            return jobs.ERROR, "parse the book before indexing it"

        if not state.get("chunked"):
            jobs.set_stage(job["id"], "chunking", "splitting into chunks…", 0.02)
            total = await asyncio.to_thread(index.build_chunks, book["id"], size, overlap)
            if total == 0:
                _update_book(book["id"], index_status="error", index_message="no chunks produced")
                return jobs.ERROR, "no chunks produced from this book"
            state.update(chunked=True, total=total)
            jobs.set_state(job["id"], state)
            _update_book(book["id"], index_status="pending", index_message="",
                         chunk_count=total, chunk_chars=size, chunk_overlap=overlap,
                         embed_model=model)
            return jobs.RUNNING, f"chunked into {total} pieces"

        total = int(state.get("total") or index.total_count(book["id"]))
        try:
            done_now, dims = await index.embed_pending(book["id"], model, batch)
        except Exception as exc:  # noqa: BLE001
            # describe_error, not str(exc): httpx timeouts stringify to '', which would
            # park the book in 'error' with a blank reason — the same blind spot the
            # status panel had when Ollama first went down mid-session.
            detail = ollama.describe_error(exc)
            _update_book(book["id"], index_status="error", index_message=detail[:500])
            return jobs.ERROR, f"embedding failed: {detail}"

        if dims and not state.get("dims"):
            state["dims"] = dims
            jobs.set_state(job["id"], state)
            _update_book(book["id"], embed_dims=dims)

        remaining = index.pending_count(book["id"])
        embedded = total - remaining
        if remaining:
            jobs.set_stage(job["id"], "embedding",
                           f"embedded {embedded}/{total} chunks…",
                           round(0.02 + 0.96 * (embedded / max(total, 1)), 3))
            return jobs.RUNNING, f"embedded {embedded}/{total}"

        _update_book(book["id"], index_status="done", index_message="",
                     chunk_count=total, embed_model=model,
                     embed_dims=int(state.get("dims") or 0))
        updated = _book(book["id"])
        builds.write_report(book["slug"], "index", "index-report.json", index.report(updated))
        _sync_manifest(book["id"])
        jobs.set_result(job["id"], {"chunks": total, "model": model,
                                    "dims": int(state.get("dims") or 0)})
        return jobs.DONE, f"{total} chunks embedded with {model}"


class ExtractRulesHandler:
    """L2 — pull the progression system out of the book.

    Map/reduce, with the split that the standing principle demands:

      engine  prefilters chunks worth reading      (systext — no model, ~65% avoided)
      model   reads ONE chunk, emits JSON facts    (map)
      engine  validates, merges, cites, persists   (reduce)

    A few chunks per tick, cursor in `state_json`, so a container restart resumes at the
    chunk it reached rather than re-reading the book — and so a single unparseable
    response is recorded and stepped over instead of failing a 200-chunk run.
    """

    #: Chunks per tick. Small because each is a full generation on a 12B model; the tick
    #: loop is what keeps progress visible and the job cancellable mid-run.
    BATCH = 3

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        params = jobs.params_of(job)
        state = jobs.state_of(job)
        model = params.get("model") or OLLAMA_MODEL

        if book["parse_status"] != "done":
            return jobs.ERROR, "parse the book before extracting from it"

        # --- stage 1: choose what to read (no model involved) -------------------
        if not state.get("selected"):
            all_chunks = index.list_chunks(book["id"])
            if not all_chunks:
                return jobs.ERROR, "no chunks — build the index first"
            limit = int(params.get("limit") or 0) or None
            picked = systext.select(all_chunks, limit=limit)
            if not picked:
                return jobs.ERROR, ("the prefilter found no passages that look like stated "
                                    "game mechanics — lower the threshold or check the parse")
            state.update(selected=[c["id"] for c in picked], cursor=0,
                         stats=systext.summarise(all_chunks), failures=[], rules_seen=0)
            jobs.set_state(job["id"], state)
            jobs.set_stage(job["id"], "extracting",
                           f"{len(picked)} of {len(all_chunks)} passages selected", 0.02)
            return jobs.RUNNING, f"{len(picked)} passages selected for extraction"

        # --- stage 2: read them, a few per tick ---------------------------------
        selected: list[int] = state["selected"]
        cursor = int(state.get("cursor", 0))
        batch_ids = selected[cursor:cursor + self.BATCH]

        if batch_ids:
            chunks = {c["id"]: c for c in index.list_chunks(book["id"], batch_ids)}
            harvested: list[dict[str, Any]] = []
            for cid in batch_ids:
                chunk = chunks.get(cid)
                if chunk is None:
                    continue          # chunk vanished under a reindex; skip, don't die
                try:
                    reply = await ollama.generate(
                        extract.build_map_prompt(chunk),
                        system=extract.SYSTEM_PROMPT,
                        model=model,
                        # Deterministic-ish: extraction is not a creative task, and a
                        # hot model invents mechanics that are not in the passage.
                        options={"temperature": 0.1},
                    )
                except Exception as exc:  # noqa: BLE001
                    detail = ollama.describe_error(exc)
                    _update_book(book["id"], index_message="")
                    return jobs.ERROR, f"extraction failed talking to Ollama: {detail}"

                rules, err = extract.parse_model_rules(reply, chunk)
                if err:
                    state["failures"].append({"chunk_id": cid, "error": err})
                    logs.warn("process", f"unparseable extraction for chunk {cid}: {err}",
                              book_id=book["id"])
                harvested.extend(rules)

            if harvested:
                merged = extract.merge_rules(harvested)
                ins, upd = rules_store.upsert(book["id"], merged)
                state["rules_seen"] = int(state.get("rules_seen", 0)) + ins

            cursor += len(batch_ids)
            state["cursor"] = cursor
            jobs.set_state(job["id"], state)
            done_frac = cursor / max(len(selected), 1)
            counts = rules_store.counts(book["id"])
            jobs.set_stage(job["id"], "extracting",
                           f"read {cursor}/{len(selected)} passages · {counts['total']} rules",
                           round(0.02 + 0.93 * done_frac, 3))
            return jobs.RUNNING, f"read {cursor}/{len(selected)} · {counts['total']} rules"

        # --- stage 3: emit the artefact -----------------------------------------
        jobs.set_stage(job["id"], "writing", "writing rules/system.json…", 0.97)
        kept = [r for r in rules_store.list_rules(book["id"]) if r["status"] != "discarded"]
        stats = dict(state.get("stats") or {})
        stats.update(passages_read=len(selected),
                     unparseable=len(state.get("failures") or []),
                     model=model)
        doc = extract.build_document(book, [
            {"id": r["rule_key"], "kind": r["kind"], "name": r["name"],
             "statement": r["statement"], "formula": r["formula"],
             "confidence": r["confidence"], "evidence_excerpt": r["evidence_excerpt"],
             "citations": r["citations"]} for r in kept
        ], model, stats)

        builds.write_report(book["slug"], "campaign/rules", "system.json", doc)
        builds.write_report(book["slug"], "review", "extraction-report.json", {
            "book": {"title": book["title"], "slug": book["slug"]},
            "model": model,
            "prefilter": state.get("stats"),
            "passages_read": len(selected),
            "rules_found": len(kept),
            "unparseable_responses": state.get("failures") or [],
        })
        jobs.set_result(job["id"], {"rules": len(kept), "passages": len(selected),
                                    "unparseable": len(state.get("failures") or [])})
        bad = len(state.get("failures") or [])
        note = f", {bad} unparseable" if bad else ""
        return jobs.DONE, f"{len(kept)} rules from {len(selected)} passages{note}"


jobs.register("parse", ParseHandler())
jobs.register("index", IndexHandler())
jobs.register("extract_rules", ExtractRulesHandler())


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def _startup() -> None:
    logs.info("boot", f"Lore Forge {VERSION} starting")
    logs.info("boot", "config", ollama_url=OLLAMA_URL, embed_model=EMBED_MODEL,
              generate_model=OLLAMA_MODEL, lore_builds=str(LORE_BUILDS_ROOT),
              db_dir=str(DB_DIR), log_dir=str(LOG_DIR), frontend=str(FRONTEND_DIR))
    try:
        db.init_db()
        logs.info("boot", "database ready", path=str(db.DB_PATH))
    except Exception as exc:  # noqa: BLE001
        logs.error("boot", f"database init failed: {exc}")
        raise

    mounted = LORE_BUILDS_ROOT.is_dir()
    if not mounted:
        try:
            LORE_BUILDS_ROOT.mkdir(parents=True, exist_ok=True)
            mounted = True
        except OSError:
            pass
    writable, err = builds.probe_writable(LORE_BUILDS_ROOT)
    (logs.info if (mounted and writable) else logs.error)(
        "boot", "lore-builds mount check", path=str(LORE_BUILDS_ROOT),
        mounted=mounted, writable=writable, error=err)
    for label, d in (("db", DB_DIR), ("logs", LOG_DIR)):
        if not d.is_dir():
            logs.warn("boot", f"{label} directory not mounted", path=str(d))

    st = await ollama.status()
    if not st["reachable"]:
        logs.error("boot", "Ollama unreachable at startup", url=OLLAMA_URL, error=st["error"])
    else:
        logs.info("boot", f"Ollama reachable — {len(st['models'])} model(s)",
                  embed_model=EMBED_MODEL, embed_present=st["embed_model_present"])
        if not st["embed_model_present"]:
            logs.error("boot", f"embedding model '{EMBED_MODEL}' is NOT pulled — indexing "
                               f"will fail. Pull it on {OLLAMA_URL}.")

    # Anything left 'running' from a previous container is re-ticked from its row.
    with db.connect() as conn:
        stuck = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'running'").fetchone()["n"]
    if stuck:
        logs.info("boot", f"{stuck} job(s) will resume from their stored stage")

    asyncio.create_task(jobs.run_worker())


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/api/status")
async def status() -> dict:
    """Sidebar status: Ollama, the embedding model, and the output mount."""
    mounted = LORE_BUILDS_ROOT.is_dir()
    writable, err = builds.probe_writable(LORE_BUILDS_ROOT)
    with db.connect() as conn:
        books = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
    return {
        "version": VERSION,
        "ollama": await ollama.status(),
        "storage": {"root": str(LORE_BUILDS_ROOT), "mounted": mounted,
                    "writable": writable, "error": err},
        "defaults": {"chunk_chars": CHUNK_CHARS, "chunk_overlap": CHUNK_OVERLAP,
                     "embed_batch": EMBED_BATCH},
        "books": books,
    }


# --------------------------------------------------------------------------- #
# books — L0 intake
# --------------------------------------------------------------------------- #

@app.get("/api/books")
async def list_books() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM books ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/books/{book_id}")
async def get_book(book_id: int) -> dict:
    book = _book(book_id)
    book["chapters"] = _chapters(book_id)
    book["folder"] = str(builds.book_dir(book["slug"]))
    book["disk_bytes"] = builds.disk_usage(book["slug"])
    book["embedded_chunks"] = index.total_count(book_id) - index.pending_count(book_id)
    return book


@app.post("/api/books")
async def create_book(file: UploadFile = File(...), title: str = Form(""),
                      author: str = Form("")) -> dict:
    """Upload a book. Intake is upload-only by design at 0.1.x; a Calibre browser is
    a second source later, not a different pipeline."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "the uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is {len(data):,} bytes; the limit is {MAX_UPLOAD_BYTES:,}")

    kind = parse.sniff_kind(file.filename or "", data)
    if not kind:
        raise HTTPException(400, "unrecognised file type — EPUB, PDF or plain text only")

    name = (title or Path(file.filename or "book").stem).strip()
    slug = builds.slugify(name)
    with db.connect() as conn:
        taken = {r["slug"] for r in conn.execute("SELECT slug FROM books").fetchall()}
    if slug in taken:
        n = 2
        while f"{slug}-{n}" in taken:
            n += 1
        slug = f"{slug}-{n}"

    builds.ensure_book_dir(slug)
    safe_name = builds.slugify(Path(file.filename or "book").stem) + Path(file.filename or "").suffix
    dest = builds.safe_path(slug, "sources", safe_name)
    if dest is None:
        raise HTTPException(400, "invalid filename")
    try:
        dest.write_bytes(data)
    except OSError as exc:
        raise HTTPException(500, f"could not write the upload: {exc}") from exc

    sha = hashlib.sha256(data).hexdigest()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO books (title, slug, author, source_kind, source_file, source_bytes,"
            " source_sha) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, slug, author.strip(), kind, safe_name, len(data), sha),
        )
        book_id = cur.lastrowid

    logs.info("process", f"book uploaded: {name}", book_id=book_id, slug=slug,
              kind=kind, bytes=len(data))
    _sync_manifest(book_id)
    return _book(book_id)


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: int, purge_files: bool = False) -> dict:
    """Remove the book. Files are kept unless `purge_files=true` — a bad extraction
    run should be a deleted folder by choice, not by accident."""
    book = _book(book_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    removed = False
    if purge_files:
        import shutil  # noqa: PLC0415
        root = builds.book_dir(book["slug"])
        try:
            if root.is_dir():
                shutil.rmtree(root)
                removed = True
        except OSError as exc:
            logs.error("local", f"could not remove book folder: {exc}", slug=book["slug"])
    logs.info("process", f"book deleted: {book['title']}", book_id=book_id,
              files_removed=removed)
    return {"deleted": True, "files_removed": removed, "folder": str(builds.book_dir(book["slug"]))}


@app.post("/api/books/{book_id}/parse")
async def start_parse(book_id: int) -> dict:
    book = _book(book_id)
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'parse'"
            " AND status IN ('queued','running')", (book_id,)
        ).fetchone()
    if busy:
        raise HTTPException(409, f"a parse is already queued for this book (job {busy['id']})")
    _update_book(book_id, parse_status="pending", parse_message="")
    job = jobs.enqueue("parse", book_id, {"kind": book["source_kind"]})
    return job


@app.get("/api/books/{book_id}/chapters")
async def get_chapters(book_id: int) -> list[dict]:
    _book(book_id)
    return _chapters(book_id)


@app.get("/api/books/{book_id}/chapters/{position}")
async def get_chapter(book_id: int, position: int) -> dict:
    _book(book_id)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM chapters WHERE book_id = ? AND position = ?", (book_id, position)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"no chapter {position} in book {book_id}")
    return dict(row)


@app.get("/api/books/{book_id}/report")
async def get_report(book_id: int, which: str = "parse") -> dict:
    """Read back a written report — proof that lives on disk, not just in the UI."""
    book = _book(book_id)
    folder, name = ("review", "parse-report.json") if which == "parse" else ("index", "index-report.json")
    path = builds.safe_path(book["slug"], folder, name)
    if path is None or not path.is_file():
        raise HTTPException(404, f"no {which} report yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"could not read the report: {exc}") from exc


# --------------------------------------------------------------------------- #
# index + query — L1
# --------------------------------------------------------------------------- #

class IndexRequest(BaseModel):
    model: str = ""
    chunk_chars: int = Field(default=0, ge=0, le=8000)
    chunk_overlap: int = Field(default=0, ge=0, le=2000)
    batch: int = Field(default=0, ge=0, le=128)


@app.post("/api/books/{book_id}/index")
async def start_index(book_id: int, req: IndexRequest) -> dict:
    book = _book(book_id)
    if book["parse_status"] != "done":
        raise HTTPException(409, "parse the book before indexing it")
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'index'"
            " AND status IN ('queued','running')", (book_id,)
        ).fetchone()
    if busy:
        raise HTTPException(409, f"an index build is already queued (job {busy['id']})")

    st = await ollama.status()
    model = req.model or EMBED_MODEL
    if st["reachable"]:
        names = {m["name"] for m in st["models"]}
        if model not in names and f"{model}:latest" not in names:
            raise HTTPException(400, f"'{model}' is not pulled on {OLLAMA_URL} — pull it first")
    _update_book(book_id, index_status="pending", index_message="")
    return jobs.enqueue("index", book_id, {
        "model": model,
        "chunk_chars": req.chunk_chars or CHUNK_CHARS,
        "chunk_overlap": req.chunk_overlap or CHUNK_OVERLAP,
        "batch": req.batch or EMBED_BATCH,
    })


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    k: int = Field(default=6, ge=1, le=25)


@app.post("/api/books/{book_id}/query")
async def query_book(book_id: int, req: QueryRequest) -> dict:
    """Cited retrieval, no generation. This endpoint IS the L1 proof."""
    book = _book(book_id)
    try:
        return await index.query(book, req.question, req.k)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logs.error("process", f"query failed: {exc}", book_id=book_id)
        raise HTTPException(502, f"query failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# extraction — L2
# --------------------------------------------------------------------------- #

@app.get("/api/books/{book_id}/extract/preview")
async def extract_preview(book_id: int, limit: int = 15) -> dict:
    """What the prefilter would send to the model, and why.

    Costs nothing and needs no Ollama. A prefilter you can't inspect is one you can't
    trust when a rule turns up missing — so this exists before the run, not after.
    """
    _book(book_id)
    chunks = index.list_chunks(book_id)
    if not chunks:
        raise HTTPException(409, "no chunks — parse and index the book first")
    picked = systext.select(chunks)
    return {
        "stats": systext.summarise(chunks),
        "top": [
            {"chunk_id": c["id"], "chapter": c["chapter_position"],
             "chapter_title": c["chapter_title"], "score": c["_score"],
             "reasons": c["_reasons"], "preview": c["text"][:240]}
            for c in picked[:limit]
        ],
    }


class ExtractRequest(BaseModel):
    model: str = ""
    limit: int = Field(default=0, ge=0, le=2000)   # 0 = every selected passage


@app.post("/api/books/{book_id}/extract/rules")
async def start_extract_rules(book_id: int, req: ExtractRequest) -> dict:
    book = _book(book_id)
    if book["parse_status"] != "done":
        raise HTTPException(409, "parse the book before extracting from it")
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'extract_rules'"
            " AND status IN ('queued','running')", (book_id,)).fetchone()
    if busy:
        raise HTTPException(409, f"an extraction is already queued (job {busy['id']})")

    st = await ollama.status()
    if not st["reachable"]:
        raise HTTPException(503, f"Ollama is unreachable — {st['error']}")
    model = req.model or OLLAMA_MODEL
    names = {m["name"] for m in st["models"]}
    if model not in names and f"{model}:latest" not in names:
        raise HTTPException(400, f"'{model}' is not pulled on {OLLAMA_URL} — pull it first")

    return jobs.enqueue("extract_rules", book_id, {"model": model, "limit": req.limit})


@app.get("/api/books/{book_id}/rules")
async def get_rules(book_id: int, status: str | None = None) -> dict:
    _book(book_id)
    return {"counts": rules_store.counts(book_id),
            "rules": rules_store.list_rules(book_id, status)}


class RuleEdit(BaseModel):
    name: str | None = None
    statement: str | None = None
    formula: str | None = None
    kind: str | None = None
    confidence: str | None = None


@app.patch("/api/rules/{rule_id}")
async def edit_rule(rule_id: int, req: RuleEdit) -> dict:
    """Human edit. Marks the row `edited`, which protects it from later extraction runs
    overwriting the text — curation outranks extraction."""
    row = rules_store.edit(rule_id, **req.model_dump())
    if row is None:
        raise HTTPException(404, f"no rule {rule_id}, or nothing to change")
    return row


@app.post("/api/rules/{rule_id}/{action}")
async def curate_rule(rule_id: int, action: str) -> dict:
    if action not in ("keep", "discard", "reset"):
        raise HTTPException(400, "action must be keep, discard or reset")
    status = {"keep": "kept", "discard": "discarded", "reset": "proposed"}[action]
    row = rules_store.set_status(rule_id, status)
    if row is None:
        raise HTTPException(404, f"no rule {rule_id}")
    return row


@app.delete("/api/books/{book_id}/rules")
async def clear_rules(book_id: int, everything: bool = False) -> dict:
    """Clear extracted rules. Spares curated rows unless `everything=true`."""
    _book(book_id)
    removed = rules_store.clear(book_id, only_proposed=not everything)
    return {"removed": removed, "counts": rules_store.counts(book_id)}


# --------------------------------------------------------------------------- #
# jobs + logs
# --------------------------------------------------------------------------- #

@app.get("/api/jobs")
async def list_jobs(book_id: int | None = None, limit: int = 50) -> list[dict]:
    return jobs.list_jobs(book_id, limit)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    if job["book_id"]:
        field = "parse_status" if job["kind"] == "parse" else "index_status"
        try:
            book = _book(job["book_id"])
            if book[field] == "pending":
                _update_book(job["book_id"], **{field: "none"})
        except HTTPException:
            pass
    return job


@app.get("/api/logs")
async def get_logs(level: str | None = None, category: str | None = None,
                   since_id: int = 0, limit: int = 300, search: str | None = None,
                   persisted: bool = False) -> dict:
    items = logs.load_persisted(limit) if persisted else logs.read(level, category, since_id, limit, search)
    return {"items": items, "stats": logs.stats(),
            "levels": list(logs.LEVELS), "categories": list(logs.CATEGORIES)}


# --------------------------------------------------------------------------- #
# static frontend
# --------------------------------------------------------------------------- #

class NoCacheStaticFiles(StaticFiles):
    """Serve the frontend with `Cache-Control: no-cache` so a browser always
    revalidates. StaticFiles still sends ETag/Last-Modified, so an unchanged file is
    a cheap 304 — this only prevents the stale-app.js-after-deploy trap that cost
    Persona Forge a debugging session."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIR.is_dir():
    app.mount("/static", NoCacheStaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})
