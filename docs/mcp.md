# The MCP tool surface

Lore Forge serves an MCP endpoint from the **same process** as the web app, at `/mcp`.
It is always on — there is no flag to enable and no second container to run.

    http://192.168.1.33:8891/mcp        deployed
    http://127.0.0.1:8891/mcp           rundev.py

## Why in-process, and why a facade

The HTTP API is shaped for the frontend: ~45 endpoints, one per widget. Handing an agent
all of them would make it re-derive the L0→L3 ladder, the gating rules and every measured
invariant this project paid for — badly, and freshly wrong every session.

So `backend/app/mcp_server.py` is a **curated facade**: 19 tools named for intentions, each
carrying its invariant in the docstring where the model actually reads it. The rule that
shapes the list is *an argument the engine already settles is not an argument the agent
gets* — tiers are computed, chapter stamps come from the passage the engine chose, and the
lorebook is a deterministic projection. None of that is exposed as a knob.

Tools call the app's own endpoints in-process over `httpx.ASGITransport`, so a tool and the
UI button beside it run the identical code path and cannot drift.

This does **not** change the settled decision that the backend talks to Ollama over its
native HTTP API rather than MCP. MCP is for the agent; HTTP is for the app. Both sit over
the same functions.

## Scope: read + queue

| Can | Cannot |
|---|---|
| Inspect anything — books, chapters, census, sheets, lorebook, jobs, logs | Delete or clear anything |
| Cited retrieval (`lore_search`) | Curate — keep / discard / merge / edit |
| Start any job — parse, index, census, sheets, extract, compile | Add or remove books |

Curation outranks extraction in this app, and curation is a human verb. Every `DELETE` and
every `POST /api/{thing}/{id}/{keep,discard}` stays in the UI where you can see what you
are throwing away.

## Tools

**Read** — `lore_status` · `lore_books` · `lore_book` · `lore_chapter` · `lore_search` ·
`lore_characters` · `lore_character_context` · `lore_sheets` · `lore_lorebook` ·
`lore_jobs` · `lore_logs`

**Handoff** — `lore_dossier` (one character, versioned, spoiler-filtered) ·
`lore_cast` (every describable character in one call)

**Queue** — `lore_parse` · `lore_index` · `lore_census` · `lore_write_sheets` ·
`lore_extract` · `lore_compile_lorebook`

### Two conventions worth knowing

- **Every tool returns an object, never a bare array.** Several endpoints return a
  top-level list; an *empty* list serialises to zero MCP content blocks, so an agent asking
  "what books are loaded?" would get a successful call with no output and no way to tell
  that from a malfunction. `{"books": [], "count": 0}` says what `[]` cannot.
- **Errors carry the API's own message.** "parse the book before extracting from it" tells
  the agent exactly which rung it skipped, which is more useful than a status code.

## The handoff to Persona Forge

`lore_dossier` returns the object defined by `backend/app/handoff.py`, which is **mirrored
verbatim** into Persona Forge. Neither app knows the other exists; the agent carries the
object across. `CONTRACT_VERSION` is what makes the mirror safe — a consumer refuses a
major it was not built to read instead of mis-parsing it.

```
lore_dossier(book_id=2, char_id=7, as_of_chapter=10)
        ↓  { contract_version, name, tier, as_of_chapter, withheld_facts, fields{...} }
persona_create_from_dossier(dossier)
```

The canon cursor travels with the object: export `as_of_chapter=10` and the resulting
Persona Forge project only knows the book to that point, with `withheld_facts` reporting
how much was held back.

`lore_cast` returns unusable characters **named**, with the reason. A cast build that
silently drops four people is worse than one that says which four.

## Client configuration

The endpoint speaks streamable HTTP, stateless, with JSON responses — no session to lose
when the container restarts. House preference is the `mcp-remote` bridge rather than a
native HTTP transport:

```json
{
  "mcpServers": {
    "lore-forge": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://192.168.1.33:8891/mcp"]
    },
    "persona-forge": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://192.168.1.33:8890/mcp"]
    }
  }
}
```

Connect both. The agent holding the two is what makes the LF→PF seam work without either
service depending on the other — which is why the two apps are staying separate rather
than merging.

## Implementation notes

- The route sits at **exactly** `/mcp`, added as a Starlette `Route` whose endpoint is an
  ASGI-app *object*. Mounting a sub-app instead would serve `/mcp/` and answer `/mcp` with
  a 307, which not every client follows. (A bare `POST /mcp` with no MCP envelope correctly
  returns 400 — that is the protocol rejecting the body, and proof the path resolves.)
- Startup moved from `@app.on_event("startup")` to a **lifespan**, because the session
  manager needs a task group held open around the whole run and Starlette ignores the
  `on_event` lists once a lifespan is supplied. `_startup()` itself is unchanged.
- Boot logs `MCP tool surface mounted` with the tool count and contract version.
