# v0.1.1 — first real book, two citation bugs fixed

v0.1.0 shipped L0 (intake + parse) and L1 (index + cited query) verified against a
synthetic test corpus. The first **real** book — a 223-page, 40-chapter LitRPG PDF —
immediately found two bugs that the synthetic one could not. Both corrupted **citations**,
which is the one thing this tool exists to get right.

## What was wrong

**1. System boxes were being read as chapter headings.** The heading detector accepted
bare numeric lines like `12. The Gate`. LitRPG prose is full of stat lines that match that
shape perfectly — `500 Scumbag Points` and friends. The parser invented two chapters out
of them, split real chapters mid-scene, and attributed passages to a chapter that does not
exist.

Heading detection is now **two-tier**: *named* headings (`Chapter 12`, `Prologue`,
`Part Three`) are tried first, and the bare numeric pattern only fires when fewer than
three named ones are found. When it does fire, the parse raises a warning and reports its
method as `pdf-headings-numeric`, so a guess is never silently mistaken for a confident
read. PDF heading search now also checks a page's second non-blank line, since a running
header often takes the first.

*Result on the real book: 42 chapters → the correct 40, no bogus titles.*

**2. Merged chapters cited the wrong pages.** A one-page chapter opener folded forward
into the body that followed it — correct behaviour — but the merged chapter kept the
*fragment's* page range. It claimed `pages 1-1` while actually holding pages 1–7. No text
was ever lost, but a citation that points at the wrong page is worse than a vague one,
because it looks precise. Page ranges are now unioned when fragments merge.

*Result on the real book: page-coverage discontinuities went from one to zero — all 223
pages accounted for, contiguously.*

## Measured on the real book

| | |
|---|---|
| Parse | 40 chapters, 49,700 words, method `pdf-headings`, **no warnings** |
| Coverage | 223/223 pages, contiguous, zero gaps |
| Index | 231 chunks, 768 dims, `nomic-embed-text`, ~30 s |
| Retrieval | correct chapter ranked **first on 6 of 8** questions |
| Latency | mean 396 ms, max 660 ms per query |

The two questions that didn't land top-1 were ones written from chapter *titles* without
knowing where the book actually explains those mechanics, so they aren't necessarily
misses — noting them as unresolved rather than scoring them either way.

**Extrapolating to your scale:** ~30 s and 231 chunks for 50k words means a 400-chapter
book (~500k words) is roughly **2,300 chunks and 5 minutes** to index — still far inside
what brute-force cosine handles comfortably.

## Upgrade

```bash
docker compose pull && docker compose up -d
```

**Re-parse and re-index any book ingested with 0.1.0** — its chapter boundaries and page
citations came from the buggy detector, and only a re-parse corrects them. The app will
tell you an index is stale after a re-parse rather than letting you query it.
