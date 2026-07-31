# AI context — cold-start brief for Lore Forge

House convention: every repo carries this file. **Read it first in a new session**, then
[`PROJECT_PLAN.md`](../PROJECT_PLAN.md) for what's next and [`design.md`](design.md) for
the full design of record. Keep it current every release.

**Version 0.2.5 — local only. Not deployed, not tagged, not pushed since `v0.1.1`.**

**If you are an agent:** this app serves MCP tools in-process at `/mcp` — 19 of them, read
+ queue scope. See [`mcp.md`](mcp.md). Prefer them over raw HTTP; they carry the invariants.

---

## 1. What this is

The **text** half of a two-app character pipeline. A book goes in; a SillyTavern lorebook,
character cards and campaign data come out, with citations.
[Persona Forge](https://github.com/rhamblen/persona-forge) owns the **image** half
(prompts → dataset → per-character LoRA → expression sprites).

Separate on purpose: separate repos, GHCR images, version lines and ports (LF 8891,
PF 8890).

## 2. The rule that governs every design decision

> **Database = truth. LLM = storyteller.**

The model extracts and narrates. It never remembers, never counts, and never arbitrates.
Applied consistently, it produces the pattern you'll see everywhere in this codebase:

    engine   narrows / measures / adjudicates      (deterministic, testable, free)
    model    reads one passage and reports         (expensive, fallible, bounded)
    engine   validates, merges, cites, persists

Concrete consequences, all of which are deliberate — do not "improve" them away:

- **L1's query returns raw cited passages and no generated text.** Seeing what retrieval
  actually returned is the whole point.
- **Character tiers are computed** from mentions, chapter spread and dialogue counts.
  "Is this character important?" is never asked of the model.
- **The rules prefilter is lexical.** The model sees ~35% of chunks, not all of them.
- **Curation outranks extraction.** A human edit or discard survives every later re-run.

## 3. Where things stand

| Rung | What | Status |
|---|---|---|
| **L0** | Intake + parse → chaptered text | ✅ |
| **L1** | Chunk + embed + index → cited retrieval | ✅ |
| **L2** | Extraction: rules, world, quests, census (pass 1) + sheets (pass 2) | ✅ |
| **L3** | SillyTavern lorebook | ✅ |
| **L4** | V3 character cards | ✗ next-but-one |
| **L5** | `rules/` `story/` `canon/` `relationships/` | partial (quests done) |
| **L6** | Merge into Persona Forge — **or stay standalone** | ✅ **standalone, settled 2026-07-31** |
| **L7** | Runtime / Director | GPU-gated |

**Character pass 2 shipped in 0.2.4.** Per character, one model call per passage, and
**every fact carries the chapter it became true** — so a sheet reads, and a dossier
exports, *as of* any point in the book. The stamp comes from the passage the engine
chose, never from the model. `L4 — V3 cards` is now the next build.

**L6 settled in 0.2.5: the two apps stay separate and pass the object across.** The seam
used to be a convention — LF wrote a dossier JSON to a shared mount and PF read it — which
works right up until the two disagree about a field, at which point nothing tells you. It
is now a versioned contract (`handoff.py`, mirrored verbatim into Persona Forge) carried by
an agent holding both MCP surfaces. Neither service depends on the other at runtime, and
the separate repos, images, ports and version lines all stay as they were. See
[`mcp.md`](mcp.md).

## 4. Layout

```
backend/app/
  config.py          env, paths, VERSION, BUILD stamp
  logs.py            5 levels x 5 categories -> stdout + ring + JSONL  (ported from PF)
  jobs.py            serial, resume-safe asyncio job engine            (ported from PF)
  db.py              schema + boot migrations
  builds.py          the on-disk file contract
  parse.py           L0 — JSON/JSONL, EPUB, PDF, TXT -> ordered chapters
  index.py           L1 — chunking, embeddings, brute-force cosine, citations
  systext.py         L2 — lexical prefilter for "states game mechanics" (no model)
  census.py          L2 — character harvest, tiering, pairing (mostly no model)
  sheets.py          L2 pass 2 — passage selection, chapter-stamped facts, dossiers
  llmjson.py         repairs the JSON a 12B model actually emits
  extract.py         prompts + normalise/merge for rules, world, quests, census
  lorebook.py        L3 — the SillyTavern world file (entities, quests, rules, characters)
  ollama.py          embeddings + generation
  {rules,entries,quests,characters,facts}_store.py   persistence + curation
  handoff.py         the versioned PF contract — MIRRORED VERBATIM in Persona Forge
  mcp_server.py      19 agent tools at /mcp, in-process facade over the endpoints below
  main.py            API, job handlers, static frontend
frontend/            no build step: index.html + app.js + style.css
backend/tests/       183 tests, all offline (a stub stands in for the model)
```

`handoff.py` is pure stdlib and imports nothing from this app, so the two copies diff byte
for byte. **If you change one, copy it to the other and re-run the tests in both.**

Job kinds: `parse`, `index`, `extract_rules`, `extract_world`, `extract_quests`,
`census`, `character_sheets`.

## 5. Conventions inherited from Persona Forge — do not diverge

1. FastAPI + SQLite; `jobs` table + reconcile; append-only where history matters.
2. Same log levels (`verbose|debug|info|warn|error`) and categories
   (`boot|integration|process|local|api`).
3. **`db/` and `logs/` are peers of `docker/`**, never nested inside it.
4. **Only `docker/` is copied to the server**; images come from GHCR.
   **Claude never builds or deploys containers.**
5. Frontend served `Cache-Control: no-cache`.
6. `docker/.env` is tracked on purpose (no secrets).

Every table is named as a PF table would be, so an L6 merge is an importer, not a rewrite.

## 6. Settled facts — don't re-derive

**Infrastructure**
- Ollama is at **`192.168.1.32`** on its own br0 macvlan — *not* UR1's `.33`. Models:
  `nomic-embed-text` (768d, embeddings), `bge-m3` (1024d), `gemma3:12b` (extraction).
- The embed model + dimension are recorded **per book**; a query against a different
  model is refused, not coerced.
- **UR1 saturates.** A 90 °C CPU and unresponsive containers on 2026-07-29 turned out to
  be host saturation (CPU pegged, RAM 29/33.5 GB, **no swap**) plus high ambient
  temperature — not a GPU fault. GPUs answered the driver throughout.

**Architecture**
- **No lxml / EbookLib.** EPUB is read via the OPF spine with stdlib zipfile+ElementTree
  (+ `bs4(html.parser)`); lxml needs libxml2 headers and fails on Python 3.14.
- **numpy brute-force cosine IS the vector store.** ~3–4k chunks per 400-chapter book;
  sub-10 ms. `index._CACHE` holds one book's matrix, keyed by `(embedded_count, model)`.
- **Extraction needs chunks, not embeddings.** The handlers build chunks on demand, so a
  freshly parsed book can be extracted without an embedding run.
- **`build` stamp** in `/api/health` hashes source mtimes; the UI compares it against the
  build the page loaded and shows a reload banner when they diverge.

**Content**
- Books come from the user's own **Webnovel→EPUB scraper**, split into volumes of up to
  ~400 chapters (Shadow Slave: 3000+ chapters over 11 books).
- **JSON/JSONL is the preferred intake** — chapter order, titles and the **source URL**
  are given rather than inferred, and the URL lands in the citation. Measured: a JSONL
  query scored 0.798 vs 0.618 for the equivalent EPUB one.

## 7. Open decisions — ask before building past them

1. ~~**Spoiler control**~~ — **settled 2026-07-30, built in 0.2.4.** Every fact carries
   the chapter it became true, so a sheet or card exports "as of chapter N" (the design's
   `must-not-yet` canon tier). The stamp is taken from the passage the engine fed the
   model, never reported by the model.
2. **Per-corpus tiering.** The census tiers one book at a time, so a character who is
   minor in book 1 and central in book 7 is systematically under-rated. Narrowed in
   0.2.3 — `census.merge_characters` takes the best tier across books when compiling a
   series — but not closed: each book's census still measures only its own volume.
3. ~~**Characters are not in the lorebook**~~ — **done in 0.2.3.** Entries are compiled
   from the census with every surface form as a key. All three tiers are included, per
   the plan's tier table; what bounds the size is the description rule — a character the
   census could not describe is dropped *and named*, never shipped as an empty entry.
4. **L6 merge with Persona Forge** — "when we are ready". Reasoning and the two safety
   constraints are in `PROJECT_PLAN.md` §6.

## 8. Gotchas

- `parse.parse()` is blocking CPU work — always via `asyncio.to_thread`.
- Job handlers process a few items per tick **on purpose**: that is what makes a restart
  resume instead of redoing the book. Don't collapse them into loops.
- A **scanned PDF** yields no text; `parse_pdf` detects <5 words/page and reports "needs
  OCR" rather than passing an empty book downstream.
- Writes into `/mnt/user/appdata/...` from Windows over SMB are **root-denied**; reads
  work. Verify over HTTP.
- `builds.slugify` ASCII-folds because the path lands on a Linux share read over SMB.

## 9. Lessons the real book taught (each cost a bug)

- **Test parse heuristics on real books of the target genre.** Synthetic text has none of
  the shapes that break them. LitRPG system boxes (`500 Scumbag Points`) matched a bare
  numeric heading pattern and invented chapters; heading detection is now two-tier.
- **A confidently wrong citation is worse than a vague one.** Folding a short fragment
  forward kept the fragment's page range, so a chapter holding pages 1–7 cited
  `pages 1-1`.
- **Never generalise from one instance.** One quest's failure penalty was extracted as a
  universal law about all quests. Rules gained `scope` (`system`/`instance`), and quests
  became first-class so their terms live on the quest.
- **A missing enum value causes miscategorisation, not omission** — and that is harder to
  spot. `attribute` was absent, so "training increases Stamina" was filed under `skill`.
- **Report ambiguity; don't auto-resolve it.** Same-name-different-kind rules are listed
  as `conflicts`; truncated JSON is reported, never patched.
- **An empty table must say which book it belongs to.** Uploading a second book switched
  the selector and every panel emptied, which read exactly like data loss.

## 10. Verified working (2026-07-30, v0.2.1)

Against a real 223-page LitRPG PDF and its sequel, on the live Ollama:

| | |
|---|---|
| Parse | 40 chapters, 49,700 words, 223/223 pages contiguous, no warnings |
| Index | 231 chunks, 768d, ~30 s |
| Retrieval | correct chapter first on **6 of 8** questions, mean 396 ms |
| Rules prefilter | 81/231 chunks selected — **65% of model calls avoided** |
| Rules extraction | 8 passages, `gemma3:12b`, **0 unparseable**, 16 rules |
| Census | 262 candidates → 11 characters (2 primary, 2 secondary), 242 pruned |
| Merge | `Subject …` duplicates folded; chapters **unioned**, not summed |

Current library: **Book 01** (40 ch, 231 chunks, 16 rules, 9 characters) and **Book 02**
(60 ch, 336 chunks, 28 rules, 13 characters, 13 world entities, 2 quests).

## 11. Next

1. **Characters → lorebook entries** (small, no GPU, closes §7.3).
2. **Finish the extraction set on Book 01** — world entities and quests never run there.
3. **Character pass 2 — the sheets.** Blocked on §7.1.
4. **L4 — V3 character cards**, the seam into Persona Forge.

Debt: no inline editing in the UI (PATCH endpoints exist for every kind, but the UI
offers only keep/discard/tier/merge).
