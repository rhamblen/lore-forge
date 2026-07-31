# Lore Forge

Turn a book into a **SillyTavern lorebook, character cards, and campaign data** — with
citations back to the source, and nothing ever written into SillyTavern automatically.

Lore Forge is the *book-ingest* half of the character pipeline. Its sibling,
[Persona Forge](https://github.com/rhamblen/persona-forge), owns the *visual* half —
prompts, datasets, per-character LoRAs and expression sprites. The two are deliberately
separate stacks: separate repos, separate images, separate version lines, separate ports.
A broken parse of a 400-chapter novel cannot take down a LoRA build.

| | Lore Forge | Persona Forge |
|---|---|---|
| Owns | text → lore, cards, campaign | image → dataset, LoRA, sprites |
| Port | `8891` | `8890` |
| Image | `ghcr.io/rhamblen/lore-forge` | `ghcr.io/rhamblen/persona-forge` |
| Needs | Ollama | ComfyUI + Ollama |
| Output | `lore-builds/` | `comfyui-builds/` |

---

## Versions

| Version | What landed |
|---|---|
| **0.2.4** *(local)* | **L2 pass 2 — character sheets.** Per-character extraction where every fact records the chapter it became true, so a sheet reads and a dossier exports *as of* any point in the book. |
| 0.2.3 *(local)* | Characters reach the lorebook: `character` entries compiled from the census, every alias as a key, best tier across books, and undescribed characters dropped by name rather than silently. |
| 0.2.2 *(local)* | Documentation consolidated so a new session can start cold. |
| 0.2.1 *(local)* | Build stamp + stale-page banner, so "am I running the latest?" is answerable during local iteration. |
| 0.2.0 *(local)* | **L2 + L3.** Extraction of progression rules, world entities, quests and a character census with computed tiers; curation UI; the SillyTavern lorebook, including multi-book compilation. |
| 0.1.1 | Two citation bugs found by the first real book: bare-numeric heading detection demoted to a fallback (LitRPG stat lines were being read as chapters), and merged chapters now widen their page citation instead of keeping the fragment's. |
| 0.1.0 | **L0 + L1** — upload EPUB / JSON / PDF / text, parse to clean chaptered text, chunk + embed + index, and query for **cited passages**. No generation yet. |

Versioning is `0.<phase>.<iteration>`: the middle digit is the current phase, the last
digit bumps on each update. The running version is shown in the sidebar and returned by
`GET /api/health`, so which build is deployed is never a guess.

---

## The standing principle

> **Database = truth. LLM = storyteller.**

The extracted text, its chapter structure and its citations live in rows. The model
extracts and narrates; it never remembers and never arbitrates. This is why L1 ends in a
query that returns *raw cited passages* and no generated text at all — if retrieval is
bad, you find out here, before a single generation prompt is written.

## Phases

Each step is provable without the next.

| | Deliverable | Proven by |
|---|---|---|
| **L0** ✅ | Intake + parse → clean chaptered text | Read the text; check the parse report |
| **L1** ✅ | Chunk + embed + index | Ask a question, get cited passages. **No generation** |
| **L2** ✅ | Extraction: rules, world, quests, character census + curation | Review the tables |
| **L3** ✅ | `worlds/<Book>.json` — the lorebook | **First ST-usable output** |
| **L4** | V3 character cards (`.json`) | Import one into SillyTavern |
| **L5** | `rules/` + `story/` + `canon/` + `relationships/` | Inspect as files (quests done) |
| **L6** | Merge into Persona Forge as a Book tab | Phase E exists by then |
| **L7** | Runtime / Director as a `generate_interceptor` | Gated on a second 3090 for the GM brain |

Full design, including the seven output artefacts and the three-tier canon table:
[`docs/design.md`](docs/design.md).

---

## Input formats

Ranked by how much is *stated* versus *inferred* — this ranking decides how much to
trust everything built on top.

| Format | Chapter boundaries | Citation quality | Verdict |
|---|---|---|---|
| **JSON / JSONL** | Exact — given | **Source URL** | **Scrape into this** |
| **EPUB** | Declared in the OPF spine | File href | Reliable |
| **PDF** | Heuristic — detected headings, else page blocks | Page range | Works, with caveats |
| **Plain text** | Heuristic — heading regex | Line range | Last resort |

### The recommended scraper output

One chapter per line, JSONL. Nothing is inferred and the source URL survives all the way
into the citation:

```json
{"index": 1, "title": "Chapter 1: The Nightmare Spell", "url": "https://…/chapter-1", "text": "…"}
```

`text` is required (aliases: `content`, `body`). Everything else is optional: `title`
(`name`, `chapter_title`), `url` (`source_ref`, `ref`, `source`, `link`), and
`index` (`position`, `chapter`) to force ordering. A single JSON document also works:
`{"title": …, "author": …, "chapters": [ … ]}`.

**Keep site boilerplate out of `text`** — navigation, "next chapter" links and ads
embed just as happily as prose and dilute retrieval.

### Scale

Sized for a serialised webnovel split into books of up to ~400 chapters. At that size a
book is roughly 3–4k chunks (~10 MB of vectors), which is a sub-10 ms brute-force cosine
search in numpy — no vector-database extension needed. `sqlite-vec` is the documented
upgrade path if a merged multi-book corpus ever outgrows it.

---

## Requirements

- **Ollama** reachable on the LAN, with an embedding model pulled:

  ```bash
  curl -s http://192.168.1.32:11434/api/pull -d '{"name":"nomic-embed-text"}'
  ```

  `nomic-embed-text` (768 dims, ~275 MB) is the default. `bge-m3` (1024 dims, ~1.2 GB)
  has a longer context and is worth measuring against it on real prose. The model and its
  dimension are recorded **per book**, and a query against a different model is refused
  rather than silently returning nonsense.
- **A writable output folder** for `lore-builds/`, a sibling of `comfyui-builds`.
- No GPU of its own, no ComfyUI, no SillyTavern connection.

## Deploy

Only the `docker/` folder goes on the server; the image is pulled from GHCR and is never
built there.

1. Copy `docker/` to `/mnt/user/appdata/lore-forge/docker/`.
2. Adjust paths in `docker/.env` if they differ from the defaults.
3. `docker compose pull && docker compose up -d`.

`db/` and `logs/` are created as **peers** of `docker/`, not inside it.

```
/mnt/user/appdata/lore-forge/
  docker/   compose + .env   ← the only folder copied
  db/       SQLite
  logs/     rolling JSONL
```

Update to a new version: `docker compose pull && docker compose up -d`, then confirm the
version in the sidebar changed.

## Output layout

```
lore-builds/<book-slug>/
  book.json          manifest — sources, hashes, models used, run config
  sources/           the upload + extracted text, chapter-structured
  index/             index report
  review/            parse report, citations, conflicts

  st-import/         MIRRORS SILLYTAVERN'S OWN TREE, VERBATIM
    worlds/  characters/  QuickReplies/

  campaign/          engine inputs, NOT for copying into ST
    dossiers/  rules/  story/  canon/  relationships/
```

Integration is *copy the contents of `st-import/` into `default-user/`* — the folder
names are already SillyTavern's own, so there is no path translation and no renaming.
`campaign/` sits deliberately outside it so runtime state can never be hand-copied into
ST by accident.

**Staged, never automatic.** Nothing is written into SillyTavern by this app, ever.

## API

| | |
|---|---|
| `GET /api/health` | `{status, version}` |
| `GET /api/status` | Ollama, embedding model, output mount |
| `GET/POST /api/books` · `DELETE /api/books/{id}` | library; `?purge_files=true` also deletes the folder |
| `POST /api/books/{id}/parse` | queue the L0 parse |
| `GET /api/books/{id}/chapters[/{position}]` | chaptered text |
| `POST /api/books/{id}/index` | queue the L1 index build |
| `POST /api/books/{id}/query` | **cited retrieval** — `{question, k}` |
| `GET /api/books/{id}/report?which=parse\|index` | the written reports |
| `GET /api/jobs` · `POST /api/jobs/{id}/cancel` | background jobs |
| `GET /api/logs` | levels + categories |

## Local development

```bash
docker compose -f docker/docker-compose.build.yml up -d --build
```

## Standing rules

- **Transform, never reproduce.** Cards and lore are summarised behavioural profiles
  with citations — never verbatim source text. Private use.
- **Staged, never auto-copied** into SillyTavern.
- **Prose, not tags**; no expression words in identity prompts.
- Only `docker/` is copied to the server; images come from GHCR.

## Licence

MIT — see [LICENSE](LICENSE).
