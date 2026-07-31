# Changelog

Versioning is `0.<phase>.<iteration>` — the middle digit is the current phase, the last
digit bumps on each update. Phases follow the L0–L7 ladder in [`docs/design.md`](docs/design.md).

## 0.2.3 (local — not released) — 2026-07-30

### Added — characters reach the lorebook

The census had been building tiered, alias-merged, hand-curated character records that
the compile then ignored: `build_lorebook` gathered entities, rules and quests and
nothing else. Closes open decision 3 in `docs/ai-context.md` §7.

- **`character` is a lorebook kind**, at order 98 — under `system`, over `quest`. A
  mechanic stated once in chapter 4 is unrecoverable from anywhere else if it is evicted;
  how a character behaves is also visible in the chat itself.
- **Every censused surface form becomes a key.** This is why compiling characters is
  worth doing at pass 1: the aliases are already merged, already corrected by hand where
  the model got it wrong, and a name is the likeliest thing to appear in chat.
- **All three tiers are in by default** (`character_tiers`), per the tier table in
  `PROJECT_PLAN.md` §4: a lorebook line is exactly what a filler character earns, and the
  only thing they earn — they never become a card. A checkbox drops them for a
  principals-only book. What keeps filler from flooding the compile is not the tier
  filter but the description rule below.
- **A character the census could not describe is dropped — and named.** The entry body is
  the census note and nothing else, so no note means an entry that fires and then says
  nothing. The response and the UI list who was left out; the remedy is a note written by
  hand on the L2 tab, and nobody writes it for a silent drop.
- **Chapter counts and mention totals are deliberately NOT in the entry.** They are
  corpus statistics, and in a roleplay context they are noise at best and a quiet spoiler
  at worst ("last appears in chapter 12"). Pass 2 writes the sheet.
- **`census.merge_characters`** folds one character's per-volume rows together for a
  series compile, unioning aliases and taking the **best tier across books** — which
  narrows, but does not close, the per-corpus tiering gap (open decision 2).
- Characters are emitted **last**, so adding them does not renumber the entries an
  already-imported lorebook has: a uid is what an ST-side edit is attached to.

11 new tests (136 total, all offline).

## 0.2.2 (local — not released) — 2026-07-30

**Documentation consolidation**, so a new session can pick this up cold.

- **`docs/ai-context.md` rewritten** — it had drifted to "0.1.1, L0 + L1" while L2, L3,
  quests, the census and the lorebook all landed. It is the cold-start brief and the house
  convention says it moves every release. Now carries the ladder status, the
  engine/model division of labour that every module follows, settled infrastructure facts,
  the open decisions, and a "lessons the real book taught" section where each entry cost a
  bug.
- **`PROJECT_PLAN.md`** — ladder marked through L3, "Built" rewritten for 0.2.x, and §4
  reordered into the actual next four steps with the spoiler decision called out as the
  blocker on character pass 2. Series-level merging moved out of "deferred": it shipped.
- **`README.md`** — version table and phase table brought current.
- **Root `CURRENT_STATE.md`** — Track C rewritten as a handover: working mode, ladder,
  what is in the library right now, next steps, and the blocking decision.

No code changes.

## 0.2.1 (local — not released) — 2026-07-30

### Fixed — "showing as 0.2.0 so I don't know if I've updated"

A fair complaint, and my fault: during a run of local iteration the version deliberately
stays still while the code moves underneath it, so the badge could not answer *"am I
running the latest?"* — the one question it looked like it was answering.

- **`/api/health` now returns a `build` stamp** — a short hash over the size and mtime of
  every backend module and frontend asset, so any edit changes it. Cheap,
  dependency-free, and needs no git (the container has no repo).
- **The sidebar shows `v0.2.1 · 5c3ee4b7`.** Version says which release; build says which
  code.
- **Stale pages announce themselves.** The frontend records the build it loaded and
  compares it on every poll; when the server's build changes, a banner appears with a
  Reload button. This catches the case the no-cache header cannot — a page left open
  across a restart is running old JavaScript no matter how fresh the server is.
- The boot log states the build too, so `docker logs` answers the same question.
- The iteration digit now moves on each local change, per the versioning convention.

Verified by touching a source file and restarting: `71f3860a` → `5c3ee4b7`.

## 0.2.0 (local — not released) — 2026-07-30

Phase bump: the middle digit tracks the phase, and L2 (extraction, census) and L3 (the
SillyTavern lorebook) both landed. Still **local only** — no tag, no GHCR push, no deploy.

### Character pairing — automatic where it can be, manual where it can't

`Subject Diane Fitzgerald` and `Diane Fitzgerald` survived as two characters. Three
changes, because no single one is sufficient:

- **Containment matching now compares whole-token runs, not just single tokens.** The
  first version paired `Lukas` inside `Lukas Belmont` but could not see `Diane
  Fitzgerald` inside `Subject Diane Fitzgerald` — the same failure one word wider.
- **Display names ignore status-box labels.** `preferred_name()` picks the fullest form
  after excluding those led by a label word (`Subject`, `Target`, `Host`…), which gets
  both cases right at once: `Lukas Belmont` beats `Lukas`, while `Diane Fitzgerald` beats
  `Subject Diane Fitzgerald`.
- **Manual merge**, because heuristics will never be enough. A relational reference —
  "Mom" for Diane Fitzgerald — shares no tokens with the real name, so no matching rule
  can ever propose it. Each row has a *merge into…* control; the absorbed name survives
  as an **alias**, which keeps the lorebook trigger alive and makes the pairing stick:
  `_find_by_any_name` means a later census lands on the existing character instead of
  resurrecting the row you merged away.

- **Context lookup on every character.** A *context* button lists the passages where the
  name or any alias appears, with chapter attribution — this is what makes a merge
  judgeable, since "Mom" can only be resolved by reading the surrounding sentence.
- Characters gained `chapters_json`, because a merge must **union** chapter sets: "24
  chapters" plus "2 chapters" is anywhere from 24 to 26, and counts cannot be added.
  Merging recomputes the tier from the combined evidence unless it is human-locked.

Verified on the real book: both `Subject …` duplicates merged, giving Sloane Fitzgerald
319 mentions over 39 chapters and Diane Fitzgerald 155 over 24 — chapter counts unioned,
not double-counted, and each now carries three lorebook keys.

### Fixed — "adding a second book lost the first book's details"

It did not: the first book's 40 chapters, 231 chunks, 16 rules and 11 characters were all
intact in the database. **The bug was in the UI.** Uploading a book switches the selector
to it, and every L2/L3 panel then correctly showed the *new* book's empty state — but
nothing on those panels said which book they were showing, so an empty table was
indistinguishable from lost work.

- Both tabs now carry a **scope banner** naming the selected book, and stating that other
  books keep their own rules, characters and quests.
- Empty tables now say *"No rules extracted for **this book**"* and point at the Book
  selector, instead of a bare "nothing extracted yet".
- **Extraction no longer requires an index.** A freshly parsed book has chapters but no
  chunks, and the extraction passes failed with "build the index first" — misleading,
  since embeddings are only needed for retrieval. Chunking is deterministic and takes
  milliseconds, so the extraction handlers now build chunks on demand.

## Unreleased (local)

**Working mode changed 2026-07-29: local only.** No UR1 deploy and no per-change release
until asked — build and test with `python rundev.py`, and keep this changelog, the plan
and `VERSION` current instead. New entries accumulate here; the version bumps when a
release is actually wanted. See `PROJECT_PLAN.md` §6.

### L2 groundwork — progression-rule extraction (no model needed to develop it)

Built while UR1 was down, so all of it is proven offline with a stub model and **64
passing tests**. Starting with `rules/system.json` rather than characters, because the
genre states its own mechanics in system boxes — the highest-signal, lowest-ambiguity
target, and therefore the right confidence test for every later pass.

- **`systext.py` — a lexical prefilter, no model involved.** Scores each chunk for
  "probably states game mechanics" using bracketed callouts, `Label: value` stat lines,
  numeric awards, shouted lines, system vocabulary and rule phrasing. Deliberately
  recall-biased: a false positive wastes one model call, a false negative silently loses
  a rule forever. **Measured on the real book: 81 of 231 chunks selected — 65% of model
  calls avoided**, with the top scorers landing exactly on the mechanics chapters. Every
  selection carries its reasons, because a prefilter you can't inspect is one you can't
  trust when a rule turns up missing.
- **`llmjson.py` — repairing what a 12B model actually emits.** Code fences, prose
  preamble, trailing commas, typographic quotes, bare lists where an object was asked
  for. Repairs only the unambiguous: **truncated output is reported, never patched**,
  since a silently invented rule is far worse than a missing one. String-aware
  throughout — a value containing `,}` or `[1]` survives repair, which a regex approach
  corrupts (there is a regression test for exactly that).
- **`extract.py` — the contract.** Closed vocabulary of rule kinds so output is
  groupable; hard caps on every field; unknown kinds coerced rather than dropped;
  unmarked confidence treated as the weaker `implied`. Merging folds duplicate rules and
  **unions their citations**, so a mechanic restated across five chapters becomes one
  rule with five citations — and the citation count doubles as a load-bearing signal.
  Prompts demand paraphrase, and `evidence_excerpt` is hard-capped, honouring *transform,
  never reproduce*.
- **`rules_store.py` + a `rules` table — curation outranks extraction.** Re-running an
  extraction **merges into** existing rows rather than replacing them: a human-edited
  rule keeps its text and only gains citations, and a discarded rule is never resurrected
  by a later run. Verified end to end against the real book's database.
- **Job handler + endpoints:** `extract_rules` runs a few chunks per tick with its cursor
  in `state_json`, so a restart resumes mid-book and one unparseable response is stepped
  over instead of failing a 200-passage run. `GET /extract/preview` shows what the
  prefilter would send **and why, before spending any model time**; `POST /extract/rules`
  runs it; `GET /rules`, `PATCH /rules/{id}` and `POST /rules/{id}/{keep|discard|reset}`
  drive curation.
- Extraction reads chunk *text*, not embeddings, so L2 can run on a parsed book whose
  index was never built.

#### First live run — 8 passages, `gemma3:12b`

**0 unparseable responses.** The model returned clean JSON on all eight, so the repair
layer went unused (it stays, for when it isn't). 16 rules extracted. The mechanics work;
the extraction *quality* showed three defects, all now fixed:

- **State was captured as rules.** `[level] "The character's level is currently 26"` is a
  snapshot of one character at one moment, not a mechanic — false a chapter later, and no
  business being in `rules/system.json`. The prompt now draws the distinction explicitly
  and gives examples of what to skip.
- **One mechanic split across two kinds.** "Temptation Gauge" came back as both `mechanic`
  and `skill`; since merge identity is `kind:name`, the two never folded. `find_conflicts()`
  now reports same-name-different-kind into a `conflicts` block in `system.json`. They are
  **reported, not auto-merged** — two rules can legitimately share a name (a Stamina *cap*
  and a Stamina *attribute* are different rules), so collapsing them blindly would destroy
  information. The judgement belongs with curation.
- **No `attribute` kind existed.** "Training increases Stamina" and "...Durability" were
  filed under `skill` because the closed vocabulary offered nowhere better. A missing kind
  doesn't cause a missing rule — it causes a miscategorised one, which is harder to spot.
  `attribute` added.

The prompt also now asks for the book's own term as the rule `name`, so the same mechanic
gets the same name each time it appears and merges across chapters as intended.

**Still to do:** the full 81-passage run (deferred — high ambient temperature), and the
frontend L2 tab. The endpoints work now.

### Character census — pass 1 of two

Characters get **two passes**, for a structural reason rather than a cost one: a rule is
stated in one place, but a character is distributed across forty chapters, so a sheet
assembled chunk-by-chunk is a merge problem that worsens with the character's importance.

    pass 1 (this)  who exists, what they are called, who matters
    pass 2 (next)  per character, retrieve their passages and write ONE coherent sheet

- **`census.py` — lexical harvest, no model.** Capitalised runs plus dialogue attribution
  (`X said` / `said X`), counted per chapter. Runs over a 50k-word book in **36 ms**.
  Asking a 12B model "who are the characters?" 231 times is slower, dearer and worse at
  counting than a regex — names are surface features.
- **The tier is COMPUTED, never asked of the model.** Mentions, chapter spread and speech
  acts are evidence; "is this character important?" is a question a model answers
  confidently and inconsistently. Chapter spread outweighs raw mentions (a name in 30 of
  40 chapters is structural; 50 mentions in one chapter is a set-piece), and dialogue is
  weighted heavily — a character who speaks needs a voice, which is most of what a card
  is for. Every tier carries its arithmetic as `tier_reason`, and a human override sets
  `tier_locked` so a re-census cannot quietly undo it.
- **The model has exactly two jobs:** decide which candidates are people, and group the
  surface forms of one person. On the real book it pruned 242 of 262 candidates —
  game terms, places and scanner artefacts — and merged `Diane`/`Diane Fitzgerald` while
  correctly keeping the two Fitzgeralds apart.
- **Cross-batch reconciliation.** Resolution runs in batches of 25, so two forms of one
  character can land in different batches and never be compared — `Lukas` and `Lukas
  Belmont` came out as two characters for exactly that reason. The engine now shortlists
  containment pairs and the model confirms only those it is sure of; a missed merge is
  fixed by hand, a wrong merge silently fuses two characters.
- **Leading stopwords are stripped.** A sentence beginning "If Lukas had…" yielded the
  candidate `If Lukas`, which also *stole mentions* from the real character. Dropping the
  leading stopword removed the junk and returned the counts: Lukas went from 96 to 113.
- The tier is the **handoff to Persona Forge** — it decides how many expression sprites a
  character warrants. Lore Forge holds no expression logic of its own.

Measured on the real book: 262 candidates → **11 characters** (2 primary, 2 secondary,
7 filler), 242 pruned. Sloane Fitzgerald at 309 mentions across 39/40 chapters with 31
speech acts; Lukas Belmont recurring but silent, correctly secondary.

**Known residue:** a few filler-tier artefacts survive (`Subject Diane Fitzgerald` from
the book's status boxes, `Plan Sloane`, `Dr`). They are all filler, so they cost nothing
downstream, and curation clears them in a click.

### L2 + L3 in the UI, quests, and multi-book lorebooks

- **L2 and L3 are now tabs**, not curl. "Extract & curate" runs the passes and shows
  rules, quests and world entities in tables with keep/discard on each row; "Lorebook"
  compiles, previews and downloads the ST file.
- **L3 — the SillyTavern lorebook.** Compiled deterministically from curated rows with
  **no model run**, so a rebuild after an edit is instant. Entries are a map keyed by uid
  *string* (ST's real format — a list imports as an empty book, silently), every field ST
  expects is present, and systems/quests outrank terminology in `order` so a tight context
  keeps the mechanics rather than the flavour text. Written to `st-import/worlds/`, which
  mirrors ST's own tree, so installing is a copy with no renaming.
- **Progression rules are lorebook material.** The design lists "magic/tech systems" as an
  entry kind, so L2's rules compile into `system` entries — which meant L3 produced
  something real from data already extracted.
- **Quests are first-class** (`campaign/story/quests.json`), ordered by where the book
  first meets them — the journey. Each quest carries **its own** reward, penalty,
  giver, requirements, deadline and outcome. Merging fills fields in rather than
  overwriting, so a reward named in chapter 3 and a penalty named in chapter 9 end up on
  one quest; a resolved outcome is never dragged back to `ongoing` by a later mention.
- **Rules gained `scope`** (`system` vs `instance`) plus `applies_to`, and `system.json`
  now splits the two. **This came from a real error the user caught:** one quest's failure
  penalty had been extracted as a universal law governing every quest. The prompt now
  forbids generalising from a single instance, and naming an `applies_to` forces
  `instance` scope. That class of error is worse than a miss — a wrong universal rule is
  confidently applied everywhere and later passes inherit it.
- **Multi-book lorebooks.** `also_books` folds several volumes into one file, merging
  entities across books by kind+name with aliases and citations unioned — so "the Court"
  in book 1 and book 7 becomes one entry. A serialised webnovel splits into ~400-chapter
  volumes (11 for a 3000-chapter series), so this is the normal case. `name` overrides the
  filename for a series.
- **Aliases everywhere.** Rules and quests now capture aliases like world entities do,
  because at L3 they all become entries and **an entry fires only on its keys** — a lost
  alias is a silently dead entry. The UI flags entries that have only one key.
- **Readable filenames.** The derived name cut mid-word at 64 characters
  (`...-the-scumbag-system-traini.json`); ST shows the filename as the world's name, so it
  now drops the subtitle and cuts on a word boundary.
- Schema migrations run on boot (`ALTER TABLE` where `CREATE TABLE IF NOT EXISTS` is a
  no-op), so existing databases pick up new columns.

**93 tests.** First live run of the rules pass: 8 passages, `gemma3:12b`, 0 unparseable.

- **"Unreachable" now says why.** Ollama went down mid-session and the status panel
  reported `reachable: false` with a **blank** error, because httpx's timeout exceptions
  stringify to the empty string and the code used `str(exc)`. `ollama.describe_error()`
  now maps the exception type to something actionable — and the distinction earned its
  keep immediately: it separated *"nothing is listening"* (`ConnectTimeout`) from
  *"connected, but the server did not reply in time"* (`ReadTimeout`), which is what
  identified a **hung** Ollama process rather than a restarting one. Applied to the
  status probe and to embedding failures during an index build, which had the same blind
  spot and could park a book in `error` with no stated reason. Unreachability is now
  logged at `warn` rather than `verbose`.
- **The Embed row no longer claims "not pulled" when Ollama is down.** It can't know that,
  and it would send you off pulling a model that is already there. It now reads `unknown`
  with "can't check, Ollama is down".
- Documented the deferred **Persona Forge compose merge** ("when we are ready") in
  `PROJECT_PLAN.md` §6, including the two constraints that make it safe: every new
  variable needs a compose-level default so a missing `LORE_*` value cannot block the PF
  stack, and Lore Forge must never join the `docker-ctl` network.

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
