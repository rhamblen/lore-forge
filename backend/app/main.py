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

from . import (
    builds, census, characters_store, db, entries_store, extract, index, jobs, logs,
    lorebook, ollama, parse, quests_store, rules_store, systext,
)
from .config import (
    BUILD,
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


async def _ensure_chunks(book_id: int) -> list[dict[str, Any]]:
    """Chunks for extraction, building them if the book has never been indexed.

    Extraction reads chunk *text*; only retrieval needs embeddings. Chunking is
    deterministic, free and takes milliseconds, so demanding a full embedding run before
    you can extract from a freshly parsed book is a pointless gate — and a confusing one,
    since the error said "build the index first" when the index was not the problem.
    """
    chunks = index.list_chunks(book_id)
    if chunks:
        return chunks
    made = await asyncio.to_thread(index.build_chunks, book_id)
    if made:
        logs.info("process", f"chunked {made} passage(s) for extraction (no index needed)",
                  book_id=book_id)
    return index.list_chunks(book_id)


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
            all_chunks = await _ensure_chunks(book["id"])
            if not all_chunks:
                return jobs.ERROR, "no chunks could be built — check the parse"
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


class ExtractWorldHandler(ExtractRulesHandler):
    """L2, world half — places, factions, systems, artefacts, history, terminology.

    Same map/reduce as the rules pass, with two differences that matter:

    - **No prefilter.** Rules cluster in system boxes and are findable lexically; world
      entities are named anywhere in the prose, so a keyword filter would quietly lose
      most of them. This pass reads every chunk, which is why it costs roughly three
      times the rules pass on the same book.
    - **Aliases are unioned on merge**, so "the Ashen Court" seen in chapter 2 and "the
      Court" in chapter 9 become one entry with both triggers.
    """

    BATCH = 3

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        params = jobs.params_of(job)
        state = jobs.state_of(job)
        model = params.get("model") or OLLAMA_MODEL

        if book["parse_status"] != "done":
            return jobs.ERROR, "parse the book before extracting from it"

        if not state.get("selected"):
            all_chunks = await _ensure_chunks(book["id"])
            if not all_chunks:
                return jobs.ERROR, "no chunks could be built — check the parse"
            limit = int(params.get("limit") or 0)
            ids = [c["id"] for c in all_chunks][:limit] if limit else [c["id"] for c in all_chunks]
            state.update(selected=ids, cursor=0, failures=[])
            jobs.set_state(job["id"], state)
            jobs.set_stage(job["id"], "extracting", f"{len(ids)} passages to read", 0.02)
            return jobs.RUNNING, f"{len(ids)} passages to read"

        selected: list[int] = state["selected"]
        cursor = int(state.get("cursor", 0))
        batch_ids = selected[cursor:cursor + self.BATCH]

        if batch_ids:
            chunks = {c["id"]: c for c in index.list_chunks(book["id"], batch_ids)}
            harvested: list[dict[str, Any]] = []
            for cid in batch_ids:
                chunk = chunks.get(cid)
                if chunk is None:
                    continue
                try:
                    reply = await ollama.generate(
                        extract.build_world_prompt(chunk),
                        system=extract.WORLD_SYSTEM_PROMPT,
                        model=model,
                        options={"temperature": 0.1},
                    )
                except Exception as exc:  # noqa: BLE001
                    return jobs.ERROR, f"extraction failed talking to Ollama: {ollama.describe_error(exc)}"

                ents, err = extract.parse_model_entities(reply, chunk)
                if err:
                    state["failures"].append({"chunk_id": cid, "error": err})
                    logs.warn("process", f"unparseable world extraction for chunk {cid}: {err}",
                              book_id=book["id"])
                harvested.extend(ents)

            if harvested:
                entries_store.upsert(book["id"], extract.merge_entities(harvested))

            cursor += len(batch_ids)
            state["cursor"] = cursor
            jobs.set_state(job["id"], state)
            counts = entries_store.counts(book["id"])
            jobs.set_stage(job["id"], "extracting",
                           f"read {cursor}/{len(selected)} · {counts['total']} entries",
                           round(0.02 + 0.96 * (cursor / max(len(selected), 1)), 3))
            return jobs.RUNNING, f"read {cursor}/{len(selected)} · {counts['total']} entries"

        counts = entries_store.counts(book["id"])
        bad = len(state.get("failures") or [])
        jobs.set_result(job["id"], {"entries": counts["total"], "passages": len(selected),
                                    "unparseable": bad})
        note = f", {bad} unparseable" if bad else ""
        return jobs.DONE, f"{counts['total']} entities from {len(selected)} passages{note}"


class ExtractQuestsHandler(ExtractWorldHandler):
    """L2/L5 — the quests, in the order the book meets them.

    Reads every chunk like the world pass (a quest can be named anywhere), but keeps its
    own artefact: a quest's reward and penalty are *that quest's* terms. Flattening them
    into system rules is the error this pass exists to prevent.
    """

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        params = jobs.params_of(job)
        state = jobs.state_of(job)
        model = params.get("model") or OLLAMA_MODEL

        if book["parse_status"] != "done":
            return jobs.ERROR, "parse the book before extracting from it"

        if not state.get("selected"):
            all_chunks = await _ensure_chunks(book["id"])
            if not all_chunks:
                return jobs.ERROR, "no chunks could be built — check the parse"
            limit = int(params.get("limit") or 0)
            ids = [c["id"] for c in all_chunks]
            if limit:
                ids = ids[:limit]
            state.update(selected=ids, cursor=0, failures=[])
            jobs.set_state(job["id"], state)
            jobs.set_stage(job["id"], "extracting", f"{len(ids)} passages to read", 0.02)
            return jobs.RUNNING, f"{len(ids)} passages to read"

        selected: list[int] = state["selected"]
        cursor = int(state.get("cursor", 0))
        batch_ids = selected[cursor:cursor + self.BATCH]

        if batch_ids:
            chunks = {c["id"]: c for c in index.list_chunks(book["id"], batch_ids)}
            harvested: list[dict[str, Any]] = []
            for cid in batch_ids:
                chunk = chunks.get(cid)
                if chunk is None:
                    continue
                try:
                    reply = await ollama.generate(
                        extract.build_quest_prompt(chunk),
                        system=extract.QUEST_SYSTEM_PROMPT,
                        model=model, options={"temperature": 0.1})
                except Exception as exc:  # noqa: BLE001
                    return jobs.ERROR, f"extraction failed talking to Ollama: {ollama.describe_error(exc)}"
                quests, err = extract.parse_model_quests(reply, chunk)
                if err:
                    state["failures"].append({"chunk_id": cid, "error": err})
                harvested.extend(quests)

            if harvested:
                quests_store.upsert(book["id"], extract.merge_quests(harvested))

            cursor += len(batch_ids)
            state["cursor"] = cursor
            jobs.set_state(job["id"], state)
            counts = quests_store.counts(book["id"])
            jobs.set_stage(job["id"], "extracting",
                           f"read {cursor}/{len(selected)} · {counts['total']} quests",
                           round(0.02 + 0.93 * (cursor / max(len(selected), 1)), 3))
            return jobs.RUNNING, f"read {cursor}/{len(selected)} · {counts['total']} quests"

        jobs.set_stage(job["id"], "writing", "writing story/quests.json…", 0.97)
        kept = [q for q in quests_store.list_quests(book["id"]) if q["status"] != "discarded"]
        doc = extract.build_journey(book, kept, model)
        builds.write_report(book["slug"], "campaign/story", "quests.json", doc)
        bad = len(state.get("failures") or [])
        jobs.set_result(job["id"], {"quests": len(kept), "passages": len(selected),
                                    "unparseable": bad})
        note = f", {bad} unparseable" if bad else ""
        return jobs.DONE, f"{len(kept)} quests from {len(selected)} passages{note}"


class CensusHandler:
    """L2 pass 1 — the character census.

    Cheap by construction. The lexical harvest runs over the whole book in well under a
    second with no model at all; the model is then spent only on batches of *candidates*
    — a few calls, not one per chunk — to decide which are people and which surface forms
    are the same person. Tiers are computed from the evidence afterwards.

    Pass 2 (the per-character sheets) is deliberately a separate job: the tier list has to
    be reviewable before anything is built on top of it.
    """

    #: Candidates per model call. Large enough that the model can see groupings across
    #: the batch, small enough that it accounts for every item without dropping some.
    BATCH = 25

    async def tick(self, job: dict[str, Any]) -> tuple[str, str]:
        book = _book(job["book_id"])
        params = jobs.params_of(job)
        state = jobs.state_of(job)
        model = params.get("model") or OLLAMA_MODEL

        if book["parse_status"] != "done":
            return jobs.ERROR, "parse the book before running the census"

        chapters = _chapters(book["id"], with_text=True)
        if not chapters:
            return jobs.ERROR, "no chapters — parse the book first"

        # --- stage 1: lexical harvest, no model --------------------------------
        if not state.get("candidates"):
            found = await asyncio.to_thread(census.harvest, chapters)
            if not found:
                return jobs.ERROR, "no name candidates found in this book"
            state.update(candidates=found, cursor=0, people=[], not_people=[], warnings=[])
            jobs.set_state(job["id"], state)
            jobs.set_stage(job["id"], "resolving",
                           f"{len(found)} candidates harvested", 0.05)
            return jobs.RUNNING, f"{len(found)} name candidates harvested"

        candidates: list[dict[str, Any]] = state["candidates"]
        cursor = int(state.get("cursor", 0))
        batch = candidates[cursor:cursor + self.BATCH]

        # --- stage 2: the model decides person / not-person, and groups aliases --
        if batch:
            for c in batch:
                c["snippets"] = census.context_snippets(chapters, c["name"], limit=1)
            try:
                reply = await ollama.generate(
                    extract.build_census_prompt(batch),
                    system=extract.CENSUS_SYSTEM_PROMPT,
                    model=model, options={"temperature": 0.1})
            except Exception as exc:  # noqa: BLE001
                return jobs.ERROR, f"census failed talking to Ollama: {ollama.describe_error(exc)}"

            people, not_people, warning = extract.parse_census(
                reply, [c["name"] for c in batch])
            if warning:
                state["warnings"].append({"batch": cursor, "warning": warning})
            state["people"] += people
            state["not_people"] += not_people

            cursor += len(batch)
            state["cursor"] = cursor
            jobs.set_state(job["id"], state)
            jobs.set_stage(job["id"], "resolving",
                           f"resolved {cursor}/{len(candidates)} candidates · "
                           f"{len(state['people'])} people",
                           round(0.05 + 0.9 * (cursor / max(len(candidates), 1)), 3))
            return jobs.RUNNING, f"resolved {cursor}/{len(candidates)}"

        # --- stage 2b: reconcile across batch boundaries ------------------------
        # Resolution runs in batches, so two forms of one character can land in
        # different batches and never be compared. The engine shortlists containment
        # pairs; the model confirms only the ones it is sure about.
        if not state.get("reconciled"):
            names = [p["name"] for p in state["people"]]
            pairs = census.containment_pairs(names)
            if pairs:
                stats = {c["name"]: c for c in candidates}
                for p in state["people"]:
                    stats.setdefault(p["name"], {})
                try:
                    reply = await ollama.generate(
                        extract.build_reconcile_prompt(pairs, stats),
                        system=extract.RECONCILE_SYSTEM_PROMPT,
                        model=model, options={"temperature": 0.1})
                    confirmed, _ = extract.parse_reconcile(reply, pairs)
                except Exception as exc:  # noqa: BLE001
                    logs.warn("process", f"reconcile pass failed, continuing unmerged: {exc}")
                    confirmed = []

                by_name = {p["name"]: p for p in state["people"]}
                for short, long in confirmed:
                    a, b = by_name.get(short), by_name.get(long)
                    if not a or not b or a is b:
                        continue
                    # The fuller form becomes the primary name; the other joins as an
                    # alias, carrying its own aliases with it.
                    b["aliases"] = list({*b.get("aliases", []), a["name"],
                                         *a.get("aliases", [])} - {b["name"]})
                    by_name.pop(short, None)
                state["people"] = list(by_name.values())
                if confirmed:
                    logs.info("process", f"reconciled {len(confirmed)} cross-batch alias pair(s)",
                              book_id=book["id"])
            state["reconciled"] = True
            jobs.set_state(job["id"], state)

        # --- stage 3: fold the counts onto each person, then tier ---------------
        jobs.set_stage(job["id"], "tiering", "computing tiers…", 0.97)
        by_name = {c["name"].lower(): c for c in candidates}
        merged: list[dict[str, Any]] = []
        for person in state["people"]:
            forms = [person["name"], *person.get("aliases", [])]
            stats = [by_name[f.lower()] for f in forms if f.lower() in by_name]
            if not stats:
                continue
            # A person's evidence is the SUM over their surface forms — "Diane" and
            # "Diane Fitzgerald" are counted separately by the scanner, and only the
            # combined figure reflects how present they actually are.
            chapters_union: set[int] = set()
            for s in stats:
                chapters_union |= set(s["chapters"])
            merged.append({
                "name": person["name"],
                "aliases": person.get("aliases", []),
                "note": person.get("note", ""),
                "mentions": sum(s["mentions"] for s in stats),
                "dialogue_hits": sum(s["dialogue_hits"] for s in stats),
                # The set, not only its size — a later merge must union chapters, and
                # counts cannot be unioned.
                "chapters": sorted(chapters_union),
                "chapter_count": len(chapters_union),
                "first_chapter": min(s["first_chapter"] for s in stats),
                "last_chapter": max(s["last_chapter"] for s in stats),
            })

        ins, upd = characters_store.upsert(book["id"], merged, len(chapters))
        counts = characters_store.counts(book["id"])
        builds.write_report(book["slug"], "review", "census-report.json", {
            "book": {"title": book["title"], "slug": book["slug"]},
            "model": model,
            "candidates_harvested": len(candidates),
            "people": len(merged),
            "pruned": len(state["not_people"]),
            "pruned_names": state["not_people"],
            "by_tier": {k: v for k, v in counts.items() if k != "total"},
            "warnings": state.get("warnings") or [],
        })
        jobs.set_result(job["id"], {"people": len(merged), "inserted": ins, "updated": upd,
                                    "pruned": len(state["not_people"]),
                                    "by_tier": counts})
        return jobs.DONE, (f"{len(merged)} characters "
                           f"({counts['primary']} primary, {counts['secondary']} secondary, "
                           f"{counts['filler']} filler); {len(state['not_people'])} pruned")


jobs.register("census", CensusHandler())
jobs.register("parse", ParseHandler())
jobs.register("index", IndexHandler())
jobs.register("extract_rules", ExtractRulesHandler())
jobs.register("extract_world", ExtractWorldHandler())
jobs.register("extract_quests", ExtractQuestsHandler())


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def _startup() -> None:
    logs.info("boot", f"Lore Forge {VERSION} (build {BUILD}) starting")
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


STARTED_AT = time.time()


@app.get("/api/health")
async def health() -> dict:
    """Version plus a build stamp.

    `version` answers "which release", `build` answers "is this your latest code" —
    which the version cannot, because it deliberately stays still during a run of local
    iteration. The frontend compares the build it loaded against this one and tells you
    to refresh when they diverge.
    """
    return {"status": "ok", "version": VERSION, "build": BUILD,
            "started_at": STARTED_AT,
            "uptime_seconds": round(time.time() - STARTED_AT, 1)}


@app.get("/api/status")
async def status() -> dict:
    """Sidebar status: Ollama, the embedding model, and the output mount."""
    mounted = LORE_BUILDS_ROOT.is_dir()
    writable, err = builds.probe_writable(LORE_BUILDS_ROOT)
    with db.connect() as conn:
        books = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
    return {
        "version": VERSION,
        "build": BUILD,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
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


@app.post("/api/books/{book_id}/extract/world")
async def start_extract_world(book_id: int, req: ExtractRequest) -> dict:
    book = _book(book_id)
    if book["parse_status"] != "done":
        raise HTTPException(409, "parse the book before extracting from it")
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'extract_world'"
            " AND status IN ('queued','running')", (book_id,)).fetchone()
    if busy:
        raise HTTPException(409, f"a world extraction is already queued (job {busy['id']})")
    st = await ollama.status()
    if not st["reachable"]:
        raise HTTPException(503, f"Ollama is unreachable — {st['error']}")
    model = req.model or OLLAMA_MODEL
    names = {m["name"] for m in st["models"]}
    if model not in names and f"{model}:latest" not in names:
        raise HTTPException(400, f"'{model}' is not pulled on {OLLAMA_URL} — pull it first")
    return jobs.enqueue("extract_world", book_id, {"model": model, "limit": req.limit})


@app.get("/api/books/{book_id}/entries")
async def get_entries(book_id: int, status: str | None = None) -> dict:
    _book(book_id)
    return {"counts": entries_store.counts(book_id),
            "entries": entries_store.list_entries(book_id, status)}


class EntryEdit(BaseModel):
    name: str | None = None
    summary: str | None = None
    kind: str | None = None
    aliases: list[str] | None = None


@app.patch("/api/entries/{entry_id}")
async def edit_entry(entry_id: int, req: EntryEdit) -> dict:
    row = entries_store.edit(entry_id, **req.model_dump())
    if row is None:
        raise HTTPException(404, f"no entry {entry_id}, or nothing to change")
    return row


@app.post("/api/entries/{entry_id}/{action}")
async def curate_entry(entry_id: int, action: str) -> dict:
    if action not in ("keep", "discard", "reset"):
        raise HTTPException(400, "action must be keep, discard or reset")
    status = {"keep": "kept", "discard": "discarded", "reset": "proposed"}[action]
    row = entries_store.set_status(entry_id, status)
    if row is None:
        raise HTTPException(404, f"no entry {entry_id}")
    return row


@app.delete("/api/books/{book_id}/entries")
async def clear_entries(book_id: int, everything: bool = False) -> dict:
    _book(book_id)
    removed = entries_store.clear(book_id, only_proposed=not everything)
    return {"removed": removed, "counts": entries_store.counts(book_id)}


# --------------------------------------------------------------------------- #
# lorebook — L3
# --------------------------------------------------------------------------- #

@app.post("/api/books/{book_id}/census")
async def start_census(book_id: int, req: ExtractRequest) -> dict:
    """Pass 1 of character extraction: who exists, what they are called, who matters."""
    book = _book(book_id)
    if book["parse_status"] != "done":
        raise HTTPException(409, "parse the book before running the census")
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'census'"
            " AND status IN ('queued','running')", (book_id,)).fetchone()
    if busy:
        raise HTTPException(409, f"a census is already queued (job {busy['id']})")
    st = await ollama.status()
    if not st["reachable"]:
        raise HTTPException(503, f"Ollama is unreachable — {st['error']}")
    model = req.model or OLLAMA_MODEL
    names = {m["name"] for m in st["models"]}
    if model not in names and f"{model}:latest" not in names:
        raise HTTPException(400, f"'{model}' is not pulled on {OLLAMA_URL} — pull it first")
    return jobs.enqueue("census", book_id, {"model": model})


@app.get("/api/books/{book_id}/characters")
async def get_characters(book_id: int, tier: str | None = None,
                         status: str | None = None) -> dict:
    _book(book_id)
    return {"counts": characters_store.counts(book_id),
            "characters": characters_store.list_characters(book_id, tier, status)}


@app.get("/api/books/{book_id}/characters/{char_id}/mentions")
async def character_mentions(book_id: int, char_id: int, limit: int = 12) -> dict:
    """Passages where this character's name or aliases appear.

    The point is judgement, not display: a relational reference like "Mom" shares no
    tokens with "Diane Fitzgerald", so no matching rule can ever propose the merge. Two
    lines of context settle it, and then you pair them by hand.
    """
    _book(book_id)
    matches = [c for c in characters_store.list_characters(book_id) if c["id"] == char_id]
    if not matches:
        raise HTTPException(404, f"no character {char_id}")
    character = matches[0]
    chapters = _chapters(book_id, with_text=True)
    names = [character["name"], *character["aliases"]]
    return {"character": {"id": char_id, "name": character["name"], "aliases": character["aliases"]},
            "mentions": census.find_mentions(chapters, names, limit=limit)}


@app.post("/api/books/{book_id}/characters/{keep_id}/merge/{absorb_id}")
async def merge_characters(book_id: int, keep_id: int, absorb_id: int) -> dict:
    """Fold one character into another. The absorbed name survives as an alias, so the
    lorebook keeps that trigger and a later census cannot undo the pairing."""
    _book(book_id)
    row = characters_store.merge(book_id, keep_id, absorb_id)
    if row is None:
        raise HTTPException(400, "could not merge — check both ids belong to this book "
                                 "and are different")
    logs.info("process", f"characters merged into {row['name']}", book_id=book_id,
              keep=keep_id, absorbed=absorb_id)
    return row


class CharacterEdit(BaseModel):
    name: str | None = None
    note: str | None = None
    aliases: list[str] | None = None


@app.patch("/api/characters/{char_id}")
async def edit_character(char_id: int, req: CharacterEdit) -> dict:
    row = characters_store.edit(char_id, **req.model_dump())
    if row is None:
        raise HTTPException(404, f"no character {char_id}, or nothing to change")
    return row


@app.post("/api/characters/{char_id}/tier/{tier}")
async def set_character_tier(char_id: int, tier: str) -> dict:
    """Override the computed tier. Locks it, so a re-census cannot undo the correction."""
    row = characters_store.set_tier(char_id, tier)
    if row is None:
        raise HTTPException(400, f"no character {char_id}, or '{tier}' is not a tier")
    return row


@app.post("/api/characters/{char_id}/{action}")
async def curate_character(char_id: int, action: str) -> dict:
    if action not in ("keep", "discard", "reset"):
        raise HTTPException(400, "action must be keep, discard or reset")
    status = {"keep": "kept", "discard": "discarded", "reset": "proposed"}[action]
    row = characters_store.set_status(char_id, status)
    if row is None:
        raise HTTPException(404, f"no character {char_id}")
    return row


@app.delete("/api/books/{book_id}/characters")
async def clear_characters(book_id: int, everything: bool = False) -> dict:
    _book(book_id)
    removed = characters_store.clear(book_id, only_proposed=not everything)
    return {"removed": removed, "counts": characters_store.counts(book_id)}


@app.post("/api/books/{book_id}/extract/quests")
async def start_extract_quests(book_id: int, req: ExtractRequest) -> dict:
    book = _book(book_id)
    if book["parse_status"] != "done":
        raise HTTPException(409, "parse the book before extracting from it")
    with db.connect() as conn:
        busy = conn.execute(
            "SELECT id FROM jobs WHERE book_id = ? AND kind = 'extract_quests'"
            " AND status IN ('queued','running')", (book_id,)).fetchone()
    if busy:
        raise HTTPException(409, f"a quest extraction is already queued (job {busy['id']})")
    st = await ollama.status()
    if not st["reachable"]:
        raise HTTPException(503, f"Ollama is unreachable — {st['error']}")
    model = req.model or OLLAMA_MODEL
    names = {m["name"] for m in st["models"]}
    if model not in names and f"{model}:latest" not in names:
        raise HTTPException(400, f"'{model}' is not pulled on {OLLAMA_URL} — pull it first")
    return jobs.enqueue("extract_quests", book_id, {"model": model, "limit": req.limit})


@app.get("/api/books/{book_id}/quests")
async def get_quests(book_id: int, status: str | None = None) -> dict:
    _book(book_id)
    return {"counts": quests_store.counts(book_id),
            "quests": quests_store.list_quests(book_id, status)}


class QuestEdit(BaseModel):
    name: str | None = None
    kind: str | None = None
    outcome: str | None = None
    objective: str | None = None
    giver: str | None = None
    requirements: str | None = None
    reward: str | None = None
    penalty: str | None = None
    deadline: str | None = None
    aliases: list[str] | None = None


@app.patch("/api/quests/{quest_id}")
async def edit_quest(quest_id: int, req: QuestEdit) -> dict:
    row = quests_store.edit(quest_id, **req.model_dump())
    if row is None:
        raise HTTPException(404, f"no quest {quest_id}, or nothing to change")
    return row


@app.post("/api/quests/{quest_id}/{action}")
async def curate_quest(quest_id: int, action: str) -> dict:
    if action not in ("keep", "discard", "reset"):
        raise HTTPException(400, "action must be keep, discard or reset")
    status = {"keep": "kept", "discard": "discarded", "reset": "proposed"}[action]
    row = quests_store.set_status(quest_id, status)
    if row is None:
        raise HTTPException(404, f"no quest {quest_id}")
    return row


@app.delete("/api/books/{book_id}/quests")
async def clear_quests(book_id: int, everything: bool = False) -> dict:
    _book(book_id)
    removed = quests_store.clear(book_id, only_proposed=not everything)
    return {"removed": removed, "counts": quests_store.counts(book_id)}


class LorebookRequest(BaseModel):
    # Progression rules are legitimate lorebook material — the design lists "magic/tech
    # systems" as an entry kind — but it is opt-out, because a rules-heavy lorebook is
    # not what every book wants.
    include_rules: bool = True
    include_quests: bool = True
    include_characters: bool = True
    # Which tiers earn an entry. All three by default, per the tier table in
    # PROJECT_PLAN.md §4: a lorebook line is precisely what a filler character earns and
    # the only thing they earn — they never become a card. What keeps that from filling
    # the book with spear-carriers is not a tier filter but the description rule, since
    # a filler nobody described is dropped anyway.
    character_tiers: list[str] = Field(
        default_factory=lambda: ["primary", "secondary", "filler"])
    # Default False: 'proposed' means extracted-but-unreviewed, and the whole point of
    # curation is that unreviewed content does not silently ship.
    kept_only: bool = False
    # Extra books to fold into one lorebook. A serialised webnovel is split into books
    # of ~400 chapters (11 for a 3000-chapter series), so one world spanning several
    # files is the normal case, not an edge case. Entities merge across books by
    # kind+name with their aliases and citations unioned — the same merge used within a
    # book, which is what makes "the Court" from book 1 and book 7 a single entry.
    also_books: list[int] = Field(default_factory=list)
    # Overrides the derived filename; needed when the combined book is a series rather
    # than any one volume ("shadow-slave" across eleven files).
    name: str = ""


@app.post("/api/books/{book_id}/lorebook")
async def build_lorebook(book_id: int, req: LorebookRequest) -> dict:
    """Compile `st-import/worlds/<Book>.json`. **No model runs** — this is a
    deterministic projection of curated rows, so rebuilding after an edit is instant."""
    book = _book(book_id)
    book_ids = [book_id] + [b for b in req.also_books if b != book_id]
    for extra in book_ids[1:]:
        _book(extra)          # 404 early rather than silently skipping a missing book

    def wanted(row: dict[str, Any]) -> bool:
        return row["status"] == "kept" or (not req.kept_only and row["status"] == "proposed")

    tiers = [t for t in req.character_tiers if t in census.TIERS]

    raw_entries: list[dict[str, Any]] = []
    raw_rules: list[dict[str, Any]] = []
    raw_quests: list[dict[str, Any]] = []
    raw_characters: list[dict[str, Any]] = []
    for bid in book_ids:
        raw_entries += [e for e in entries_store.list_entries(bid) if wanted(e)]
        if req.include_rules:
            raw_rules += [r for r in rules_store.list_rules(bid) if wanted(r)]
        if req.include_quests:
            raw_quests += [q for q in quests_store.list_quests(bid) if wanted(q)]
        if req.include_characters and tiers:
            raw_characters += [c for c in characters_store.list_characters(bid)
                               if wanted(c) and c["tier"] in tiers]

    # Cross-book merge. Reuses the same by-key merge used within a book, so an entity
    # named in several volumes becomes ONE entry carrying every alias and citation —
    # which is the whole point of a series-wide lorebook.
    entries = extract.merge_entities(raw_entries) if len(book_ids) > 1 else raw_entries
    rules = extract.merge_rules(raw_rules) if len(book_ids) > 1 else raw_rules
    quests = extract.merge_quests(raw_quests) if len(book_ids) > 1 else raw_quests
    characters = (census.merge_characters(raw_characters) if len(book_ids) > 1
                  else raw_characters)

    if not entries and not rules and not quests and not characters:
        raise HTTPException(409, "nothing to compile — run an extraction first, or relax "
                                 "'kept only'")

    # Characters with no description are dropped by build_world. Name them: the remedy
    # is a note written by hand on the L2 tab, and nobody writes it for a silent drop.
    skipped = lorebook.undescribed(characters)

    world = lorebook.build_world(entries, rules, quests, characters)
    stats = lorebook.summarise(world)

    # Written under st-import/, which mirrors SillyTavern's own tree verbatim: installing
    # is copying the folder contents into default-user/, with no renaming.
    name = (builds.slugify(req.name) if req.name
            else lorebook.book_filename(book["title"], book["slug"]))
    path = builds.write_report(book["slug"], "st-import/worlds", f"{name}.json", world)
    logs.info("process", f"lorebook compiled: {stats['entries']} entries",
              book_id=book_id, books=book_ids, path=str(path),
              characters_undescribed=len(skipped))
    return {"stats": stats, "path": str(path) if path else "",
            "filename": f"{name}.json", "books": book_ids,
            "sources": {"entities": len(entries), "rules": len(rules),
                        "quests": len(quests),
                        "characters": len(characters) - len(skipped)},
            "characters_undescribed": skipped[:20],
            "characters_undescribed_total": len(skipped)}


@app.get("/api/books/{book_id}/lorebook")
async def get_lorebook(book_id: int) -> dict:
    """Read back the compiled lorebook, for preview and download."""
    book = _book(book_id)
    name = lorebook.book_filename(book["title"], book["slug"])
    path = builds.safe_path(book["slug"], "st-import/worlds", f"{name}.json")
    if path is None or not path.is_file():
        raise HTTPException(404, "no lorebook compiled yet")
    try:
        world = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"could not read the lorebook: {exc}") from exc
    return {"stats": lorebook.summarise(world), "filename": f"{name}.json",
            "path": str(path), "world": world}


@app.get("/api/books/{book_id}/lorebook/download")
async def download_lorebook(book_id: int) -> FileResponse:
    book = _book(book_id)
    name = lorebook.book_filename(book["title"], book["slug"])
    path = builds.safe_path(book["slug"], "st-import/worlds", f"{name}.json")
    if path is None or not path.is_file():
        raise HTTPException(404, "no lorebook compiled yet")
    return FileResponse(path, media_type="application/json", filename=f"{name}.json")


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
