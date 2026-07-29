# Lore Forge — project plan

The design of record is [`docs/design.md`](docs/design.md). This file is the working
roadmap: what is built, what is next, and what is deliberately deferred.

---

## 1. What it is

The **text** half of the character pipeline: a book goes in, a SillyTavern lorebook,
V3 character cards and campaign data come out, with citations back to the source.

Persona Forge owns the **image** half. The two are separate stacks by design — separate
repos, GHCR images, version lines and ports (LF 8891, PF 8890). See `docs/design.md` §2
for why, and for the four merge-first rules that keep an eventual merge cheap.

Governing rule, arrived at three times independently:

> **Database = truth. LLM = storyteller.** The model extracts and narrates; it never
> remembers and never arbitrates.

---

## 2. The ladder

Each rung is provable without the next.

| | Deliverable | Proven alone by | Status |
|---|---|---|---|
| **L0** | Intake + parse → clean chaptered text | Read the text; check the report | ✅ 0.1.0 |
| **L1** | Chunk + embed + index | Ask a question, get cited passages | ✅ 0.1.0 |
| **L2** | Extraction → dossiers + curation UI | Review the entity list | next |
| **L3** | `worlds/<Book>.json` — the lorebook | **First ST-usable output** | |
| **L4** | V3 character cards (`.json`, not PNG) | Import one into SillyTavern | |
| **L5** | `rules/` + `story/` + `canon/` + `relationships/` | Inspect as files | |
| **L6** | Merge into Persona Forge as a Book tab — **or stay standalone** | Phase E exists by then | decision |
| **L7** | Runtime / Director as a `generate_interceptor` | | GPU-gated |

---

## 3. Built — 0.1.x

**L0.** Upload-only intake (JSON/JSONL, EPUB, PDF, TXT), content-sniffed. EPUB via the
stdlib OPF spine — no lxml. PDF heuristic and honest about it. Chapters to the database
*and* to `sources/text/`. Parse report in `review/`.

**L1.** Paragraph-aware chunking with overlap and chapter offsets; Ollama embeddings one
batch per job tick (resume-safe); per-book model + dimension with a refusal on mismatch;
numpy brute-force cosine with an in-memory matrix cache; cited-passage query with no
generation.

**Platform.** FastAPI + SQLite (WAL), PF's job engine and logging conventions, the full
`st-import/` + `campaign/` file contract created up front, `book.json` manifest, no-build
frontend with the version pinned in the sidebar, GHCR publish on tag.

---

## 4. Next — L2, extraction

The first rung that uses a generation model (`gemma3:12b`; 8–14B is explicitly enough).

**Deliverable:** `campaign/dossiers/<entity>.json` — per-entity structured extraction with
source citations: identity, appearance, personality, motivation, relationships, speech
samples, timeline of appearances. Plus a curation UI: promote to card / promote to lore /
discard.

**Why this shape:** the dossier is deliberately the shape of the Persona Forge Phase E
character sheet, so Character Studio can prefill from it rather than eliciting from a
one-line seed. It also carries the appearance field that becomes the Phase A looks prompt
→ dataset → LoRA → sprites. **This is the artefact the whole merge rests on.**

**Start with `rules/system.json`, not with characters.** The LitRPG progression system is
the highest-signal, lowest-ambiguity extraction target in the pipeline, because the genre
states its own rules in-text, usually in literal system boxes. If any extraction pass will
work reliably on a small local model, it is that one — which makes it a good early
confidence test rather than a late-stage luxury.

**Honour on the way out:** prose, not tags; no expression words in the identity prompt
(proven to leak a baked-in smile into `anger` and `grief`); transform, never reproduce.

### Open before L2 starts

- **How much auto-generation vs. mandatory human review** before cards are emitted.
- **Alias harvesting is where extraction earns its keep.** "the Ashen Court", "the Court"
  and "Ashenites" must land as three keys on one lorebook entry, or it silently never
  fires.
- **Retrieval quality on a real book is unmeasured.** The 0.1.0 numbers come from a
  5-chunk synthetic corpus and mean little. Measure before trusting anything on top.

---

## 5. Deferred, with reasons

- **Series-level merging.** A serialised webnovel arrives as up to ~400 chapters per book,
  11 books for a 3000-chapter series — so "one world file per book, or merged?" is the
  normal case, not a hypothetical. Deferred until L3 makes a world file at all.
- **Calibre intake.** Upload works and the books come from the user's own scraper. Calibre
  would be a second intake, not a new pipeline.
- **OCR for scanned PDFs.** Detected and reported; `minicpm-v` is the intended fallback.
- **`sqlite-vec`.** numpy is faster than the problem needs at current scale. Revisit if a
  merged multi-book corpus outgrows it.
- **`QuickReplies/`** — waits on the runtime question.
- **L7 runtime / Director.** Unblocked technically: SillyTavern 1.18.0's
  `runGenerationInterceptors` gives an awaited pre-generation hook with the live chat array
  by reference, so the Director can be its own container. But the 70B GM brain is gated on
  the **second 3090** — a 70B GM and a LoRA build cannot coexist on one card.

---

## 6. Standing rules

- **Transform, never reproduce.** Summarised behavioural profiles with citations, never
  verbatim source text. Private use.
- **Staged, never auto-copied** into SillyTavern.
- **Claude never builds or deploys containers.** Only `docker/` is copied to the server;
  images come from GHCR.
- **Prose, not tags**; no expression words in identity prompts.
