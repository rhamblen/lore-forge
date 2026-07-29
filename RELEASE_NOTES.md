# v0.1.0 — Lore Forge, standing on its own

First release. Lore Forge is the **text** half of the character pipeline: it turns a book
into a SillyTavern lorebook, character cards and campaign data, with citations back to the
source. [Persona Forge](https://github.com/rhamblen/persona-forge) keeps the **image**
half. Separate repos, separate images, separate version lines, separate ports — a broken
parse of a 400-chapter novel cannot take down a LoRA build.

This release ships the bottom two rungs of the ladder: **L0 (intake + parse)** and
**L1 (index + cited query)**. No generation of any kind yet — that starts at L2.

## What you can do with it

1. Upload a book — **JSON/JSONL, EPUB, PDF or plain text**.
2. Parse it to clean chaptered text and *read the text* to check the parse.
3. Build a vector index over it with a local embedding model.
4. Ask it a question and get back **cited passages** — chapter, source reference, and
   character offsets.

Step 4 returns raw passages and no generated text on purpose. **L1 is the risk
checkpoint:** if retrieval is bad, every extraction pass built on top of it is bad, and
this is where you find that out — before a single generation prompt exists.

## Scrape into JSON/JSONL

Four input formats, ranked by how much is *stated* rather than *inferred*:

| Format | Chapter boundaries | Citation quality |
|---|---|---|
| **JSON / JSONL** | Exact — given | **Source URL** |
| EPUB | Declared in the OPF spine | File href |
| PDF | Heuristic — headings, else page blocks | Page range |
| Plain text | Heuristic — heading regex | Line range |

One chapter per line is all it takes, and the URL follows the passage all the way into the
citation:

```json
{"index": 1, "title": "Chapter 1: The Nightmare Spell", "url": "https://…/chapter-1", "text": "…"}
```

Only `text` is required. Keep site boilerplate — nav, "next chapter" links, ads — out of
it; that embeds just as happily as prose and dilutes retrieval.

## Before you deploy

Pull an embedding model on Ollama, or indexing will fail:

```bash
curl -s http://192.168.1.32:11434/api/pull -d '{"name":"nomic-embed-text"}'
```

`nomic-embed-text` (768 dims) is the default; `bge-m3` (1024 dims) has a longer context
and is worth measuring against it. The model and dimension are recorded **per book**, and
a query against a different model is refused rather than silently returning nonsense.

## Deploy

Only `docker/` goes on the server; the image is pulled from GHCR.

1. Copy `docker/` to `/mnt/user/appdata/lore-forge/docker/`.
2. Check the paths in `docker/.env`.
3. `docker compose pull && docker compose up -d`.

`db/` and `logs/` are created as peers of `docker/`. The app lands on **port 8891**, and
output goes to `lore-builds/`, a sibling of `comfyui-builds`. The running version is
pinned in the sidebar, so confirming an update is a glance.

## Notes

- Sized for a serialised webnovel split into books of up to ~400 chapters. That's ~3–4k
  chunks, which brute-force cosine in numpy searches in under 10 ms — no vector database
  needed yet.
- The `st-import/` folder mirrors SillyTavern's own tree verbatim, so integration will be
  a straight copy with no renaming. Nothing is ever written into SillyTavern automatically.
- Re-parsing a book invalidates its index and tells you so.

## Verified

Run against the live Ollama with a synthetic 5-chapter book: EPUB parsed in spine order;
JSONL parsed with URL-bearing citations; index built at 768 dims; queries put the correct
chapter first on 3 of 4 questions. The fourth missed — on a 5-chunk corpus that number
means very little, and retrieval quality wants re-measuring on a real book. That is
exactly what L1 is for.
