# AI context — cold-start brief for Lore Forge

House convention: every repo carries this file. Read it first in a new session, then
[`design.md`](design.md) for the full design of record. Keep it updated every release.

**Current version: 0.1.1** (L0 + L1).

---

## What this is

The **text** half of a two-app character pipeline. Lore Forge turns a book into a
SillyTavern lorebook, V3 character cards and campaign data, with citations.
[Persona Forge](https://github.com/rhamblen/persona-forge) owns the **image** half
(prompts → dataset → per-character LoRA → expression sprites).

They are separate on purpose: separate repos, GHCR images, version lines and ports
(LF 8891, PF 8890). A 400-chapter parse must not be able to kill a LoRA build.

## The rule that governs every design decision

> **Database = truth. LLM = storyteller.**

The model extracts and narrates. It never remembers and never arbitrates. The project
arrived at this from three independent directions (see design.md §1), which makes it the
architecture, not a preference.

Practical consequence you will hit immediately: **L1's query endpoint returns raw cited
passages and no generated text.** That is deliberate. Do not "improve" it by adding a
summarisation step — the whole point is to see what retrieval actually returned.

## Layout

```
backend/app/
  config.py    env + paths + VERSION resolution (both container and repo layouts)
  logs.py      5 levels x 5 categories -> stdout + ring + rolling JSONL   (ported from PF)
  db.py        books / chapters / chunks / jobs
  jobs.py      serial, resume-safe asyncio job engine                     (ported from PF)
  builds.py    the on-disk file contract (lore-builds/<slug>/...)
  parse.py     L0 — JSON/JSONL, EPUB, PDF, TXT -> ordered chapters
  index.py     L1 — chunking, embedding, brute-force cosine, citations
  ollama.py    embeddings now; generate() is there for L2
  main.py      API, job handlers, static frontend
frontend/      no build step: index.html + app.js + style.css
docker/        the ONLY folder copied to the server
```

## Conventions inherited from Persona Forge (do not diverge)

1. **FastAPI + SQLite**, append-only where history matters, `jobs` table + reconcile.
2. **Same log levels** (`verbose|debug|info|warn|error`) and **categories**
   (`boot|integration|process|local|api`).
3. **`db/` and `logs/` are peers of `docker/`**, never nested inside it — relative compose
   binds resolve against the project dir, which Unraid's Compose Manager doesn't set
   reliably. This bit PF before its 0.2.6.
4. **Only `docker/` is copied to the server**; images come from GHCR, never built there.
   **Claude never builds or deploys containers.**
5. **Frontend served `Cache-Control: no-cache`** — otherwise a browser serves a stale
   `app.js` after a deploy and you debug a phantom.
6. **`docker/.env` is tracked on purpose** (no secrets in it), so copying `docker/` gives
   a working stack with no rename step.

Every table is named as a PF table would be, so an L6 merge is an importer, not a rewrite.
`jobs` is byte-compatible with PF's except `project_id` → `book_id`.

## Things that are settled — don't re-derive

- **Embedding models are pulled and verified** on Ollama `.32`: `nomic-embed-text`
  (768 dims, default) and `bge-m3` (1024 dims). Ollama is at **`.32`**, its own br0
  macvlan — *not* UR1's `.33`. It lives there so embedding never contends with ComfyUI
  for the 3090.
- **The embed model + dimension are recorded per book.** A query against a different
  model is **refused**, not coerced — mixing dimensions returns confident nonsense.
- **No lxml, no EbookLib.** EbookLib pulls lxml, a compiled extension that needs libxml2
  headers wherever no wheel exists (it fails outright on Python 3.14). An EPUB declares
  its reading order in the OPF spine, so `zipfile` + `ElementTree` + `bs4(html.parser)`
  reads it in a page of code with no compiled dependency.
- **The vector store is numpy, on purpose.** ~3–4k chunks for a 400-chapter book (~10 MB
  at 768 dims) is a sub-10 ms matrix multiply. `index._CACHE` holds one book's matrix
  resident, keyed by `(embedded_count, model)` so a reindex self-invalidates.
  `sqlite-vec` is the upgrade path, not a current need.
- **Books come from the user's own Webnovel→EPUB scraper**, split into books of up to
  ~400 chapters (Shadow Slave: 3000+ chapters over 11 books). **JSON/JSONL is the
  preferred intake** — chapter order, titles and the source URL are *given*, so the
  citation points at a real URL instead of an offset in a container file.
- **Parse invalidates an index.** Chunks reference chapter rows; re-parsing drops them.
  `ParseHandler` clears `index_status` and says "reindex needed" rather than leaving a
  stale index queryable.

## Gotchas

- `parse.parse()` is **blocking CPU work** — always call it via `asyncio.to_thread`, or a
  600-page PDF freezes the event loop and the UI dies mid-parse.
- `IndexHandler` embeds **one batch per tick** on purpose. That is what makes a restart
  mid-index resume instead of re-embedding the book. Don't "optimise" it into a loop.
- A **scanned PDF** yields no text. `parse_pdf` detects the <5 words/page case and reports
  "needs OCR" rather than passing an empty book downstream.
- `builds.slugify` ASCII-folds because the path lands on a Linux share read over SMB from
  Windows, where accented folder names have caused trouble before.
- Writes into `/mnt/user/appdata/...` from Windows over SMB are **root-denied**; reads
  work. Verify over HTTP, not by opening the share.

## Verified working (2026-07-29, v0.1.1)

**On a real book** — a 223-page, 40-chapter LitRPG PDF, against the live Ollama at `.32`:

- Parse: 40 chapters, 49,700 words, method `pdf-headings`, no warnings, **223/223 pages
  covered contiguously**.
- Index: 231 chunks, 768 dims, `nomic-embed-text`, ~30 s.
- Query: correct chapter ranked **first on 6 of 8** questions; mean latency 396 ms. The
  two that didn't were written from chapter titles without knowing where the book explains
  those mechanics, so they are unresolved rather than confirmed misses.
- **Scale extrapolation:** 50k words ≈ 231 chunks ≈ 30 s, so a 400-chapter book (~500k
  words) is roughly 2,300 chunks and ~5 minutes.

Also on synthetic corpora: EPUB parsed in spine order; JSONL parsed with URL-bearing
citations.

**The real book found two bugs the synthetic one could not** (both fixed in 0.1.1, both
worth remembering because both produced *confidently wrong citations*):
LitRPG system boxes (`500 Scumbag Points`) matched the bare-numeric heading pattern and
invented chapters; and folding a short fragment forward kept the fragment's page range,
so a chapter holding pages 1-7 cited `pages 1-1`. **Lesson: test parsing on real books of
the target genre — synthetic text has none of the shapes that break heuristics.**

## Next

**L2 — extraction → dossiers + a curation UI.** `campaign/dossiers/<entity>.json` is the
merge contract and is deliberately shaped like the Phase E character sheet. Note from the
design doc: `rules/system.json` (the LitRPG progression system) is the
highest-signal, lowest-ambiguity extraction target because the genre states its rules
in-text — a good early confidence test, not a late-stage luxury.
