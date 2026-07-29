# Changelog

Versioning is `0.<phase>.<iteration>` — the middle digit is the current phase, the last
digit bumps on each update. Phases follow the L0–L7 ladder in [`docs/design.md`](docs/design.md).

## 0.1.1 — 2026-07-29

**Two parse bugs found by the first real book** — a 223-page, 40-chapter LitRPG PDF.
Both were invisible on the synthetic test corpus and both corrupted *citations*, which is
the one thing this tool exists to get right.

- **Bare numeric heading detection is now a fallback, not a default.** LitRPG system boxes
  are full of lines like `500 Scumbag Points`, which matched the numeric heading pattern
  perfectly. The result: two invented chapters, real chapters split mid-scene, and
  passages attributed to a chapter that does not exist. Detection now runs in two tiers —
  *named* headings (`Chapter 12`, `Prologue`, `Part Three`) first, and the bare numeric
  pattern only when fewer than three named ones are found. When the fallback does fire it
  raises a warning and reports the method as `pdf-headings-numeric`, so a heuristic parse
  is never silently mistaken for a confident one. Same two-tier logic for plain text.
  *Measured: 42 chapters → the correct 40, with no bogus titles.*
- **Folding a short fragment forward now widens the citation.** A one-page chapter opener
  merged into the body that followed it, but the merged chapter kept the *fragment's*
  page range — so it claimed `pages 1-1` while actually holding pages 1–7. No text was
  ever lost, but a citation that points at the wrong page is worse than a vague one
  because it looks precise. Page ranges are now unioned on merge.
  *Measured: page coverage across the book went from one discontinuity to zero.*
- PDF heading search also considers a page's **second** non-blank line, since a running
  header often occupies the first.

**Measured on the real book after the fixes:** 40 chapters, 49,700 words, 223/223 pages
covered contiguously, no warnings. 231 chunks embedded in ~30 s with `nomic-embed-text`.
Retrieval put the correct chapter first on 6 of 8 questions, mean query latency 396 ms.

## 0.1.0 — 2026-07-29

First release. **L0 (intake + parse) and L1 (index + cited query).**

Lore Forge is split out of Persona Forge into its own repo, GHCR image and version line,
so the text pipeline can be built, released and discussed independently of the sprite
pipeline. The design doc that specified it (`persona-forge/docs/book-ingest.md`, written
the same day) moves here as `docs/design.md`.

### L0 — intake and parse

- **Upload-only intake** for EPUB, PDF, plain text, and **structured JSON/JSONL**.
  Format is sniffed from content, not the file extension.
- **JSON/JSONL is the recommended input** and the only one with no heuristics: chapter
  order, titles and the **source URL** are given rather than inferred, so a citation
  points at a real URL instead of an offset inside a container file. Accepts JSONL (one
  chapter per line), a top-level list, or `{title, author, chapters: [...]}`, with field
  aliases for what a scraper naturally emits.
- **EPUB is read with the stdlib** — `zipfile` + `ElementTree` walk the OPF spine, so
  reading order comes from the book. No `EbookLib`, no `lxml`, and therefore no compiled
  XML dependency in the image.
- **PDF is honest about being heuristic**: chapter headings are detected at page tops and,
  when fewer than three are found, the book is split into 20-page blocks with a warning
  saying so. A scanned PDF (<5 words/page) is reported as needing OCR rather than passed
  downstream as an empty book.
- Sub-120-word fragments (title pages, dedications, dividers) fold forward into the next
  real chapter instead of littering the index.
- Chapters are written to the database **and** to `sources/text/NNN-title.txt`, because
  reading the text is how this stage is proven and that shouldn't need a database client.
- `review/parse-report.json` records method, per-chapter word counts and every warning.

### L1 — index and cited query

- **Chunking** splits on paragraph, then sentence, then word boundaries, with overlap, so
  a chunk doesn't end mid-sentence. Offsets are recorded into the chapter, which is what
  makes a hit quotable back to its exact passage.
- **Embeddings via Ollama** (`/api/embed`), one batch per job tick so progress persists
  and a restart mid-index resumes instead of re-embedding the book.
- **The embedding model and its dimension are recorded per book.** A query against a
  different model is refused with a "reindex needed" error — mixing dimensions returns
  confident nonsense, which is worse than an error.
- **Vector store is a float32 blob per row plus brute-force cosine in numpy.** A
  400-chapter book is ~3–4k chunks (~10 MB at 768 dims), a sub-10 ms matrix multiply. One
  book's matrix is cached in memory, keyed by `(embedded_count, model)` so a reindex
  self-invalidates. `sqlite-vec` is the documented upgrade path.
- **`POST /api/books/{id}/query` returns cited passages and no generated text.** That is
  the point: if retrieval is bad, everything built on top of it is bad, and you find out
  here rather than after writing generation prompts.
- Re-parsing a book invalidates its index and says so, rather than leaving stale chunks
  queryable against chapter rows that no longer exist.

### Platform

- FastAPI + SQLite (WAL), the **serial, resume-safe job engine ported from Persona Forge
  0.7.0** (`project_id` → `book_id`), and PF's exact log levels and categories.
- The full **file contract** is created for every book — `st-import/` mirrors
  SillyTavern's own tree verbatim so integration is a copy with no path translation, and
  `campaign/` sits outside it so runtime state can't be hand-copied into ST by accident.
  Later phases fill folders that already exist.
- `book.json` manifest per book — sources, hashes, models used, run config.
- Frontend with no build step; the **running version is pinned in the sidebar** from
  `/api/health`. Served `Cache-Control: no-cache` so a deploy can't leave a stale `app.js`.
- Container on port **8891** (Persona Forge is 8890), `appdata/lore-forge/{docker,db,logs}`,
  output to `lore-builds/` as a sibling of `comfyui-builds`. Published to
  `ghcr.io/rhamblen/lore-forge` on tag push.

### Verified

Run locally against the live Ollama at `.32` with a synthetic 5-chapter book: EPUB parsed
in spine order; JSONL parsed with URL-bearing citations; index built at 768 dims with
`nomic-embed-text`; queries returned correctly ranked passages on 3 of 4 questions. The
fourth missed — on a 5-chunk corpus that is not a meaningful measurement, and retrieval
quality should be re-measured on a real book.

### Not in this release

No generation of any kind. No extraction, no dossiers, no lorebook, no character cards —
those are L2–L4. `ollama.generate()` exists but nothing calls it.
