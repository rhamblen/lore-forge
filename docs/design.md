# Design — one tool, two seeds (Phase G x Phase E)

> Written 2026-07-29 in the Persona Forge repo as `docs/book-ingest.md`, and moved here
> when Lore Forge became its own repo, image and version line on the same day. This is
> the design of record for Lore Forge; the Persona Forge copy now points here.
>
> **Status:** L0 and L1 are built and shipped in v0.1.0. Everything from L2 onward is
> still design.

## 0. The two corrections that shape this doc

**One tool.** Persona Forge is the product. A book-ingest front end is a *seed* for the
existing engine, not a second application. `lore-forge` (below) is a temporary
development harness for parallel testing, and it is designed to be deleted.

**Two seeds, one engine.**

```
  "a stoic elven blacksmith"  ──┐
                                ├──►  character sheet / dossier  ──►  everything downstream
  Dungeon Crawler Carl.epub  ───┘
```

A single character and a whole cast extracted from a novel converge on the **same
structure** — the Phase E character sheet. That convergence is the design constraint: if
the book path produces something the hand-typed path can't consume, the merge has failed.
This was already the plan's own framing (Phase G is "an alternative *seed* for Phases E +
F"); the user confirmed it explicitly.

---

## 1. The standing principle

> **Database = truth. LLM = storyteller.**

The project has now arrived at this three times independently:

| Where | Statement |
|---|---|
| `prompt-language.md` §8 | Don't ask an LLM to write a prompt string; ask for structure and compile the string yourself |
| `emotion-depth.md` (H2) | The LLM emits deltas; the **engine** owns the score, decay and bucketing |
| LitRPG proposal | The LLM must never be responsible for remembering you are level 14 |

Three arrivals from three directions makes it the architecture, not a preference. It is the
rule that governs every decision below: **the model extracts and narrates; it never
remembers and never arbitrates.**

---

## 2. The parallel harness — and its expiry conditions

Build the ingest pipeline as a standalone container (`lore-forge`, port 8891, own
`appdata/lore-forge/{docker,db,logs}`, same GHCR + copy-`docker/`-only deploy convention).

**Why temporarily separate.** The RAG stack (PDF/EPUB parsers, OCR, embedding client,
vector store) is a large dependency footprint with a different failure mode, and it is
long-running and CPU-bound where Persona Forge is GPU-contended. A broken parse must not be
able to take down the sprite pipeline mid-LoRA-build. Testing it alone also means a bad
extraction run is a deleted folder, not a corrupted persona database.

> **Standing 2026-07-29 (revised):** Lore Forge is now a **separate repo, GHCR image and
> version line** (`ghcr.io/rhamblen/lore-forge`), not a scratch harness inside another
> project. The four merge-first rules below still hold and are honoured by the shipped
> code — same FastAPI + SQLite conventions, PF-absorbable table names, a file contract
> rather than a shared database, no throwaway UX. What changed is that the merge at L6
> became a **decision to make later** rather than a foregone conclusion: with its own
> release history, Lore Forge can equally stay a standalone tool that Persona Forge
> imports from. Revisit when Phase E exists.

**Merge-first rules — non-negotiable, or the merge gets expensive:**

1. **Same conventions as PF.** FastAPI + SQLite, append-only versioning, `jobs` table +
   reconcile, seeded-table CRUD (the emotion-map / pose-library pattern), the same logging
   levels and categories.
2. **No schema that PF can't absorb.** Every table must be nameable as a PF table.
3. **The handoff is a file contract, never a shared database.** The harness writes a
   defined folder layout; PF reads it. This is what makes the merge an *importer* rather
   than a rewrite.
4. **No user-facing feature the merged tool won't keep.** The harness is not a place to
   experiment with UX.

**Merge trigger (user, 2026-07-29):** once the character-building process is complete —
i.e. Phase E Character Studio exists and the dossier → character sheet → looks prompt path
is proven. At that point the harness's endpoints move into PF as a Book tab, and the file
contract becomes an internal call.

---

## 3. What it produces

Seven artefacts in two groups. The split matters: group A is copied into SillyTavern by
hand; group B is read by the engine and never leaves the app.

### Group A — ST-ready (drop-in)

**A1. `worlds/<Book>.json` — the lorebook.** The primary deliverable and the first
genuinely useful output. **Verified against the live install:** ST lorebooks are
free-standing files in `default-user/worlds/`, shape `{"entries": {"<uid>": {…}}}`, with
this per-entry schema (read from the user's own `Eldoria.json`):

```
uid, key[], keysecondary[], comment, content, constant, selective, order, position,
disable, displayIndex, addMemo, group, groupOverride, groupWeight, sticky, cooldown,
delay, probability, depth, useProbability, role, vectorized, excludeRecursion,
preventRecursion, delayUntilRecursion, scanDepth, caseSensitive, matchWholeWords,
useGroupScoring, automationId
```

Entries are grouped by kind — locations, factions, magic/tech systems, history, artefacts,
terminology. **`key[]` alias harvesting is where extraction earns its keep:** "the Ashen
Court", "the Court" and "Ashenites" must land as three keys on one entry, or the lorebook
silently never fires. Set `order` / `depth` by kind so world *rules* sit deeper in context
than trivia.

**A2. `characters/<Name>.json` — V3 character cards.** JSON only, **not PNG**. The ingest
path has no ComfyUI and no portrait; Persona Forge owns the face. ST imports `.json`
directly, so this is immediately usable, and the PNG card (portrait + embedded `ccv3`
chunk) stays PF's job once a base image is signed off. That is a clean split rather than a
compromise — and it is exactly the merge seam.

**A3. `QuickReplies/<Book>.json`** — deferred until the runtime question (§5) is settled.

### Group B — engine inputs

**B1. `dossiers/<entity>.json` — the merge contract.** Per-entity structured extraction
with source citations: identity, appearance, personality, motivation, relationships, speech
samples, timeline of appearances. **This is the artefact the whole merge rests on** — it is
deliberately the shape of the Phase E character sheet, so Character Studio can prefill from
it instead of eliciting from a one-line seed. It also carries the appearance field that
becomes the Phase A looks prompt → dataset → LoRA → sprites.

Honour the standing rules on the way out: **prose, not tags**, and **no expression words in
the identity prompt** (proven to leak a baked-in smile into `anger` and `grief`).

**B2. `rules/system.json` — the LitRPG progression system.** XP formula, level thresholds,
skill-evolution conditions, class-evolution triggers, hard caps. This is the anti-"you
unlock God Slayer Level 9000" layer.

It is also **the highest-signal, lowest-ambiguity extraction target in the pipeline**,
because the genre states its own rules in-text, usually in literal system boxes. If any
extraction pass is going to work reliably on a small local model, it is this one — which
makes it a good early confidence test, not a late-stage luxury.

**B3. `story/graph.json` — story nodes.** Not a flat beat list. Nodes carry
`requirements[]` (`player_level >= 5`, `entered_dungeon`) and `outcome[]`, so the engine can
ask *is this reachable yet*, not merely *what comes next*.

**B4. `canon/constraints.json` — the three-tier canon table.**

| Tier | Meaning | Example |
|---|---|---|
| **must** | Has to happen; carries importance + allowed deviation | Jake discovers the dragon egg (critical) |
| **may** | Free to change | Sarah survives (medium) |
| **must-not-yet** | True, but not revealable before a point | The Emperor is the villain — not before Ch. 30 |

The third tier is the novel one and exists nowhere in the current plan. It is what
distinguishes "the AI knows the plot" from "the AI spoils the plot in message four."

**B5. `relationships/matrix.json`** — implied by the dossiers, but emitted separately
because the runtime reads it every turn and should not have to parse N dossiers to do it.

---

## 4. Where it goes on disk

A sibling of `comfyui-builds` on the same shared mount PF already uses:

```
/mnt/user/data-and-backups/blender-and-comfyui-output/lore-builds/<book-slug>/
  book.json            manifest — sources, hashes, models used, run config
  sources/             uploaded originals + extracted text, chapter-structured
  index/               chunks + embeddings (sqlite-vec)
  review/              extraction report, citations, coreference conflicts

  st-import/           ← MIRRORS ST'S OWN TREE, VERBATIM
    worlds/<Book>.json
    characters/<Name>.json
    QuickReplies/<Book>.json

  campaign/            ← engine inputs, NOT for copying into ST
    dossiers/  rules/  story/  canon/  relationships/
```

**`st-import/` mirroring ST's tree is the whole answer to "easy to integrate".**
Integration is *copy the contents of `st-import/` into `default-user/`* — no path
translation, no renaming, no per-file instructions, because the folder names are already
ST's own. Verified target:

```
\\192.168.1.33\appdata\STConfig\Data\default-user\
  characters\<Name>.png    the card
  characters\<Name>\       expression sprites  (Aisha currently has 6, not 28 —
                                                the archetype-tiering decision in the wild)
  worlds\<Name>.json       lorebooks
  groups\                  empty — no group chats yet
  QuickReplies\Default.json
```

`campaign/` is deliberately *outside* `st-import/` so runtime state never gets hand-copied
into ST by accident.

**Staged, never automatic** — the same rule as sprites and VRM, and it also sidesteps the
known appdata SMB write denial (reads work; writes from Windows are root-denied).

---

## 5. The runtime — and the one unknown that gates it

The proposal's architecture splits cleanly in two:

| | What | Shape |
|---|---|---|
| **Compiler** | Book → the seven artefacts. Run once per book. | Batch, offline, no GPU contention |
| **Runtime (Director)** | Holds live campaign state; sits on ST's message path | Live service, every message |

**Build the compiler first.** Its output is inspectable as files and provable without the
runtime existing. The runtime is a much larger commitment and depends on the following.

### What was verified on 2026-07-29

Read from the user's **actually installed** `Extension-Live2d/index.js`:

```js
import { eventSource, event_types, getCharacters } from '../../../../script.js';
import { extension_settings, getContext, ModuleWorkerWrapper } from '../../../extensions.js';
import { registerSlashCommand } from '../../../slash-commands.js';

eventSource.on(event_types.CHAT_CHANGED,     …);
eventSource.on(event_types.GROUP_UPDATED,    …);
eventSource.on(event_types.MESSAGE_RECEIVED, …);
```

So, confirmed present: an **event bus**, a **context accessor** (`getContext()`), and
**slash-command registration** (STScript). Third-party extensions do load on this install.

### ✅ SETTLED — the pre-generation hook exists

**The Director is buildable as designed.** Confirmed by reading SillyTavern **1.18.0**
source (`public/scripts/extensions.js`, the version this box actually runs):

```js
// Called from Generate() in script.js, AFTER the chat is assembled and the token
// limit computed, BEFORE the prompt is built and sent:
const aborted = await runGenerationInterceptors(coreChat, this_max_context, type);
if (aborted) { unblockGeneration(type); return Promise.resolve(); }
```

An extension opts in with `"generate_interceptor": "<globalFnName>"` in its `manifest.json`;
ST then calls `globalThis[key](chat, contextSize, abort, type)`.

The contract, and why each part matters here:

| Parameter | What the Director gets |
|---|---|
| `chat` | The **live `coreChat` array, by reference** — mutate it and you change what the model receives. This is the context-packet injection point. |
| `contextSize` | The computed token budget, so the packet can be **sized to fit** rather than blindly prepended |
| `abort(immediately)` | Cancel generation outright — the hard end of "canon damage 95%" enforcement |
| `type` | Generation type, so `swipe` / `regenerate` / `impersonate` / `quiet` can be treated differently — a re-swipe must not re-fire story progression |

Three further properties that matter:

- **It is `await`ed.** An async HTTP call to an external service is legitimate, so the
  Director can be its own container talking to Ollama and the campaign DB — exactly the
  architecture the proposal wanted, rather than logic trapped inside a browser extension.
- **Skipped on `dryRun`** (token counting), which is correct — state must not advance on a
  measurement pass.
- **Ordered across extensions** by `loading_order`, and `abort(true)` stops the chain — so
  the Director and the Phase H2 emotion-state engine can be two cooperating interceptors
  with a defined precedence, not a single tangled one.

Also confirmed present, from the user's installed `Extension-Live2d/index.js`: the event
bus (`eventSource` / `event_types`), `getContext()`, and `registerSlashCommand` (STScript).
Both installed extensions only use *post*-generation events (`MESSAGE_RECEIVED`,
`CHAT_CHANGED`, `GROUP_UPDATED`) — which is why the interceptor had to be confirmed from
source rather than by example.

**Remaining caveats:**
- The image is `ghcr.io/sillytavern/sillytavern:staging` with an update pending. Staging
  tracks the dev branch — good for V3 card support, but the extension API can move
  underneath us. Pin a known-good tag before the runtime depends on this.
- Exactly how injected `chat` entries render and are attributed was **not** verified; only
  that the array is passed by reference and mutation is the intended mechanism.

*(Verified 2026-07-29 from source, not from the running instance: `192.168.1.38` currently
has an **IP conflict** — it answers from MAC `9c:9c:1f:89:e9:5b` while the ST container is
`02:42:c0:a8:01:26`. Something claimed the address during ST's two-day downtime. Needs a
DHCP reservation; unrelated to this design.)*

### Hardware gate

The proposal sizes the GM brain at 70B-class (Llama 3.3 70B / Qwen 2.5 72B). That is gated
on the **same second-3090 upgrade** already tracked in `PROJECT_PLAN.md` §7. Today there is
one 3090 plus a 3060, both contended with ComfyUI and training — demonstrated on 2026-07-29
when Immich plus a stray `minicpm-v` held 8.4 GB and killed a 1500-step build. **A 70B GM
and a LoRA build cannot coexist on one card.**

Extraction, by contrast, is explicitly fine on 8–14B, which the already-pulled
`gemma3:12b` covers — and which the Phase 9 bakeoff independently measured as the best
installed model for structured output.

---

## 6. Phasing

Each step is provable without the next.

| | Deliverable | How it's proven alone |
|---|---|---|
| **L0** | Intake + parse → clean chaptered text | Read the text; check the report |
| **L1** | Chunk + embed + index | Ask a question, get cited passages. **No generation involved** |
| **L2** | Extraction → dossiers + curation UI (promote to card / lore / discard) | Review the entity list |
| **L3** | **`worlds/<Book>.json`** | **First ST-usable output — aim here** |
| **L4** | V3 character cards (`.json`) | Import one into ST |
| **L5** | `rules/` + `story/` + `canon/` + `relationships/` | Inspect as files |
| **L6** | Merge into Persona Forge as a Book tab | Phase E exists by now |
| **L7** | Runtime / Director as a `generate_interceptor` extension + service | Unblocked (§5); gated on the 70B hardware only for the *GM brain*, not for the plumbing |

**L1 is the real risk checkpoint.** If retrieval is bad, everything above it is bad — and
you find that out before writing a single generation prompt.

**Prerequisite — DONE 2026-07-29:** `nomic-embed-text` (768 dims) and `bge-m3` (1024 dims)
are both pulled on Ollama `.32` and verified returning real vectors. `minicpm-v` is already
there and remains the plausible OCR fallback for scanned pages.

---

## 7. Open questions

- ~~**Intake source.**~~ **SETTLED 2026-07-29:** upload-only for v0.1.0, and in practice
  books arrive from the user's own **Webnovel→EPUB scraper**, not from Calibre. Because
  that scraper can emit any format, v0.1.0 added a **structured JSON/JSONL** intake — the
  only format where chapter order, titles and the **source URL** are given rather than
  inferred. Calibre remains a possible second source later; it would be a new intake, not
  a new pipeline.
- **One book → many, in practice.** A serialised webnovel is split into books of up to
  ~400 chapters (Shadow Slave: 3000+ chapters across 11 books). So the "merged or
  per-book world file" question below is not hypothetical — it is the normal case.
- **One book → one world file, or merged?** One-per-book is simpler. A series probably
  wants merging — the user already has a hand-made `Merged for Anya.json`, so this has
  been hit in practice.
- **How much auto-generation vs. mandatory human review** of extracted entities before
  cards are emitted.
- **Parsing / OCR stack** for messy scans; **vector store** choice (sqlite-vec vs. Chroma).
- **Where the Director runs** if §5 rules out an ST extension — an external service the
  card talks to, or something else.

## 8. Standing rules inherited

- **Transform, never reproduce.** Cards and lore are summarised behavioural profiles with
  citations — never verbatim book text. Private use; the charter's "motivation over
  biography, consistency over length" priorities already pull this way.
- **Staged, never auto-copied** into SillyTavern.
- **Prose, not tags**; no expression words in identity prompts.
- **Claude never builds or deploys containers**; only `docker/` is copied to the server.
