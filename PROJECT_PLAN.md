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
| **L2** | Extraction: rules, world, quests, census + sheets, curation UI | Review the tables | ✅ 0.2.4 |
| **L3** | `worlds/<Book>.json` — the lorebook | **First ST-usable output** | ✅ 0.2.0 |
| **L4** | V3 character cards (`.json`, not PNG) | Import one into SillyTavern | after pass 2 |
| **L5** | `rules/` + `story/` + `canon/` + `relationships/` | Inspect as files | partial — quests done |
| **L6** | Merge into Persona Forge as a Book tab — **or stay standalone** | Phase E exists by then | decision |
| **L7** | Runtime / Director as a `generate_interceptor` | | GPU-gated |

**Character extraction is two passes.** Pass 1 (census — who exists, what they are
called, who matters) is ✅. Pass 2 (the sheets) is the agreed next build and is
retrieval-driven **per character**, not per chunk: a rule is stated in one place, but a
character is spread across forty chapters, so assembling a sheet chunk-by-chunk is a
merge problem that worsens with the character's importance. Detail scales with tier.

---

## 3. Built — through 0.2.4

**L0.** Upload-only intake (JSON/JSONL, EPUB, PDF, TXT), content-sniffed. EPUB via the
stdlib OPF spine — no lxml. PDF heading detection is two-tier (named headings first, bare
numeric only as a warned fallback). Chapters to the database *and* to `sources/text/`.

**L1.** Paragraph-aware chunking with overlap and chapter offsets; Ollama embeddings one
batch per tick; per-book model + dimension with refusal on mismatch; numpy brute-force
cosine with a self-invalidating matrix cache; cited-passage query with no generation.

**L2.**
- **Rules** — lexical prefilter (`systext.py`) selects ~35% of chunks, avoiding 65% of
  model calls; closed kind vocabulary; `scope` separates system-wide laws from one
  quest's terms; same-name-different-kind reported as `conflicts`, never auto-merged.
- **World entities** — no prefilter (entities are named anywhere); aliases unioned on
  merge, because a lost alias is a silently dead lorebook entry.
- **Quests** — first-class, ordered by first appearance, each carrying its *own* reward,
  penalty, giver and outcome. Fields fill in across chapters; a resolved outcome is never
  dragged back.
- **Character census** — lexical harvest (36 ms, no model), model prunes non-people and
  groups aliases, engine computes the tier from evidence. Cross-batch reconciliation,
  manual merge, and a context lookup showing the passages behind each name.
- **Character sheets (pass 2)** — one model call per passage, the engine stamping each
  fact with that passage's chapter. Facts rather than prose, so a sheet reads *as of* any
  point in the book; the earliest chapter wins on a restated claim; lexical passage
  selection by mention density; `campaign/dossiers/<name>.json` per character.
- **Curation everywhere** — keep/discard/edit; re-running an extraction merges into
  existing rows and never overwrites a human edit or resurrects a discard.

**L3.** The lorebook compiles deterministically from curated rows with **no model run**.
Entries are a map keyed by uid string (ST's real format); systems, characters and quests
outrank terminology in `order`; multi-book compilation merges entities *and characters*
across volumes with aliases unioned. Characters carry every censused surface form as a
key, and one with no description is reported rather than shipped empty.

**Platform.** PF's job engine and logging; boot migrations; `build` stamp with a stale-page
banner; the full `st-import/` + `campaign/` file contract; 168 offline tests.

## 4. Next

In the order I would do them:

**1. ~~Characters → lorebook entries.~~ BUILT in 0.2.3.** The compile now takes the
census as a fourth source: `character` entries at order 98, every surface form as a key,
all three tiers, and a cross-book merge that takes the best tier a character earns in any
volume. A character the census could not describe is dropped and named rather than
shipped as an entry that fires and says nothing.

**2. Finish the extraction set on Book 01.** World entities and quests have never been run
there (Book 02 has them). GPU-bound; gated on ambient temperature, not on code.

**3. ~~Character pass 2 — the sheets.~~ BUILT in 0.2.4.** Per character, one model call
per passage, detail scaled by tier — and every fact stamped with the chapter it became
true, so a sheet reads *as of* any point in the book. Passage selection is **lexical**
(mention density) rather than via the L1 index: a passage that never names the character
is one where they are "he", and a model reading it alone cannot tell whose "he" it is
either. Semantic retrieval stays the upgrade path if a sheet ever comes out thin.

The tier table it implements:

| | Filler | Secondary | Primary |
|---|---|---|---|
| Name + **aliases**, role, first/last seen | ✅ | ✅ | ✅ |
| Lorebook entry | ✅ | ✅ | ✅ |
| Appearance, relationship, speech register | — | ✅ | ✅ |
| Motivation, personality, quirks | — | partial | ✅ |
| Example dialogue, greeting, scenario | — | — | ✅ |
| V3 card (L4) | — | optional | ✅ |
| Looks prompt → Persona Forge | — | — | ✅ |

Aliases matter at every tier — that is what makes an entry fire. The charter priorities
point the same way: *motivation over biography, behaviour over description*.

Greeting, scenario and example dialogue are **not** extracted here: they are written for
a card rather than observed in a passage, so there is no chapter to stamp them with. They
belong to L4 assembly, synthesised from these facts.

**Spoiler decision — settled 2026-07-30, implemented 2026-07-31.** See §4.1.

**4. L4 — V3 character cards — NEXT.** JSON only, no PNG: the ingest path has no portrait, and
Persona Forge owns the face. This is the seam between the two apps.

### 4.1 Spoilers — settled 2026-07-30, built 2026-07-31

A sheet written from the whole book knows the reveals; a card used at chapter 5 would
spoil them. The options were: ignore for now; **chapter-stamp each fact**; or split
safe/spoiler sections.

**Decided: chapter-stamp.** Every fact pass 2 extracts records the chapter it became
true, so a sheet or card exports "as of chapter N" — the design's `must-not-yet` canon
tier. The user reads serialised volumes and will want mid-series cards. The cost was
roughly double the scope of pass 2, and it means the extraction schema carries a chapter
on each claim rather than on the sheet as a whole.

**As built:** the stamp is never asked of the model. The engine picks one passage, whose
chapter it already knows, and stamps whatever the model reports from it — so a wrong
chapter is not a thing that can happen. `sheets.as_of()` is then a one-line filter, and
both the sheet view and the dossier writer report how many facts they are withholding.

### 4.2 Also open

- **Per-corpus tiering.** The census tiers one book at a time, so a character who is minor
  in book 1 and central in book 7 is under-rated. Narrowed in 0.2.3 — a series compile
  takes the best tier across the books — but the census itself still measures one volume.
- **Emotions are not Lore Forge's business.** The tier is the handoff; Persona Forge
  decides sprite counts from it (existing decision: filler/baddie/goodie/hero, and every
  character must have `neutral`).
- **No inline editing in the UI.** PATCH endpoints exist for every kind; the UI offers
  only keep/discard/tier/merge.

## 5. Deferred, with reasons

- ~~**Series-level merging.**~~ **BUILT in 0.2.0** — `also_books` on the lorebook compile
  folds several volumes into one file, merging entities across books by kind+name with
  aliases and citations unioned, plus a `name` override for the series. Still to do:
  apply the same cross-book view to the character census (see §4.2).
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

## 6. Working mode — LOCAL ONLY (user, 2026-07-29)

**Do not deploy to UR1, and do not cut releases per change.** Build and test locally
(`python rundev.py` → http://127.0.0.1:8891), keep the **changelog, this plan and the
VERSION file** current as changes land, and hold deployment until asked.

This supersedes the "ship without asking" rule for Lore Forge specifically — that rule
was set for Persona Forge, which is deployed and in daily use. Lore Forge is not deployed
yet, so a release adds ceremony without adding feedback.

`v0.1.0` and `v0.1.1` were tagged and pushed before this decision, so those two images
exist on GHCR. **Neither is deployed** — an image in a registry does nothing until it is
pulled. Nothing on UR1 has been touched by this project.

New work accumulates under **`## Unreleased (local)`** at the top of `CHANGELOG.md`, with
a version bump only when a release is actually wanted.

### Combining with Persona Forge — deferred, agreed for "when we are ready"

The user runs multiple pushed packages and would rather one compose stack covered both
tracks. That works, and here is the reasoning already worked out so it doesn't need
re-deriving:

- **Compose grouping costs nothing in isolation terms.** Two services in one compose file
  are still two containers with separate processes, filesystems and restart policies. The
  "a 400-chapter parse must not kill a LoRA build" property is *container* isolation and
  survives the merge intact. The earlier framing of this as an argument for separate
  compose files was wrong.
- **What actually gives two tracks is the separate repo, image and version line** — all of
  which are unaffected by how the services are deployed.
- **Shape of the merge:** add a `lore-forge` service to
  `persona-forge/docker/docker-compose.yml`, with `LF_PORT`, `EMBED_MODEL`,
  `LF_OLLAMA_MODEL`, `LORE_DB_HOST_PATH`, `LORE_LOGS_HOST_PATH` and
  `LORE_BUILDS_HOST_PATH` added to that folder's `.env`.
- **Two hard constraints for that merge:**
  1. Give **every** new variable a compose-level default (`${VAR:-default}`), never the
     required form (`${VAR:?...}`). A missing `LORE_*` value must not be able to stop the
     Persona Forge stack from coming up.
  2. Lore Forge gets `networks: [default]` **only** — never `docker-ctl`. It has no
     container-control feature and must not be handed a path to the socket proxy.
- Keep Lore Forge's own standalone compose in this repo either way, so the repo remains
  independently deployable.

*(This was built and validated once — no missing variables, no port collision, no bind-path
collision — then reverted, because the decision is "when we are ready", not now.)*

## 7. Standing rules

- **Transform, never reproduce.** Summarised behavioural profiles with citations, never
  verbatim source text. Private use.
- **Staged, never auto-copied** into SillyTavern.
- **Claude never builds or deploys containers.** Only `docker/` is copied to the server;
  images come from GHCR.
- **Prose, not tags**; no expression words in identity prompts.
