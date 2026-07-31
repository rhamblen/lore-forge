"""Lore Forge's MCP tool surface — in-process, mounted at `/mcp`.

Why this exists, and why it is *not* a separate service:

The HTTP API is shaped for the frontend — ~45 endpoints, path params, job polling, one
call per widget. That is the wrong shape to hand an agent: it would have to know the L0→L3
ladder, which endpoint gates on which status, and every measured invariant this project
paid for. So this module is a **curated facade**, not a route-to-tool dump. Each tool is
narrow, named for an intention rather than a resource, and carries the invariant in its
docstring where the model will actually read it.

The rule that shapes the tool list: **an argument the engine already settles is not an
argument the agent gets.** Tiers are computed from measured presence, chapter stamps come
from the passage the engine chose, and the lorebook is a deterministic projection of
curated rows. None of that is exposed as a knob.

**Scope is read + queue** (decided 2026-07-31). An agent can inspect anything and start
any job; it cannot delete, clear or curate. Curation outranks extraction in this app, and
curation is a human verb — `POST /api/rules/{id}/keep` and every `DELETE` stay in the UI
where you can see what you are throwing away.

Implementation note: tools call the app's own endpoints in-process over
`httpx.ASGITransport`, so a tool and the UI button beside it run the identical code path
and cannot drift. The cost is one ASGI round trip with no socket, which is microseconds.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.routing import Route

from . import handoff
from .config import VERSION

INSTRUCTIONS = """\
Lore Forge turns a book into SillyTavern lore: characters, world entities, progression
rules, quests and a compiled lorebook.

Governing rule — **the database is truth, the model is the storyteller.** The engine
narrows, measures and adjudicates; a model only ever reads one passage and reports what it
shows. Do not ask a tool here to judge who matters or which chapter something happened in;
those are computed, and the computed answer is the right one.

The ladder is ordered and each rung gates the next: parse → index → census → sheets →
lorebook. `lore_book` tells you where a book actually is. Extraction needs chunks, not
embeddings, so you can extract from a parsed-but-unindexed book; only `lore_search` needs
the index.

Spoiler control: every character fact carries the chapter it became true. Pass
`as_of_chapter` to `lore_dossier` / `lore_cast` to export what a reader at that point
knows. Omit it and you get the whole book, reveals included.

Hand a dossier to Persona Forge's `persona_create_from_dossier` to turn a character into
sprites. The two apps share `handoff.py` and nothing else.
"""

mcp: FastMCP = FastMCP(
    name="lore-forge",
    instructions=INSTRUCTIONS,
    stateless_http=True,   # no session state to lose when the container restarts
    json_response=True,    # plain JSON responses; no SSE stream to hold open
)

_APP: Any = None
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if _APP is None:  # pragma: no cover - install() runs at import time
            raise RuntimeError("mcp_server.install(app) has not run")
        _client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_APP, raise_app_exceptions=False),
            base_url="http://lore-forge.internal",
            timeout=httpx.Timeout(900.0),
        )
    return _client


async def _api(method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
    """One in-process call. A 4xx/5xx becomes an exception carrying the API's own
    message — those messages are the useful part ("parse the book before extracting from
    it" tells the agent exactly which rung it skipped)."""
    r = await _http().request(method, path, json=json, params=params)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:  # noqa: BLE001
            detail = r.text
        raise RuntimeError(f"{path} → {r.status_code}: {detail}")
    return r.json()


def _obj(payload: Any, key: str) -> dict:
    """Wrap a bare JSON array in an object.

    Several endpoints return a top-level list, which is fine for `fetch()` and wrong here:
    an *empty* list serialises to zero content blocks, so an agent asking "what books are
    loaded?" gets a successful call with no output and no way to tell that from a
    malfunction. `{"books": []}` says the thing that `[]` fails to say. Every tool in this
    module therefore returns an object.
    """
    return payload if isinstance(payload, dict) else {key: payload, "count": len(payload or [])}


# --------------------------------------------------------------------------- #
# read — where things stand
# --------------------------------------------------------------------------- #

@mcp.tool()
async def lore_status() -> dict:
    """Version, build stamp, Ollama reachability and the whole library at a glance.

    Call this first in a session. If Ollama is unreachable, every extraction tool here
    will fail — nothing else is worth trying until that is fixed."""
    status = await _api("GET", "/api/status")
    return {"version": VERSION, "contract_version": handoff.CONTRACT_VERSION, **status}


@mcp.tool()
async def lore_books() -> dict:
    """Every book in the library with its parse/index state and word counts."""
    return _obj(await _api("GET", "/api/books"), "books")


@mcp.tool()
async def lore_book(book_id: int) -> dict:
    """One book in full: chapters, parse report, and how much of the ladder is done.

    The `report` is where you find out whether the parse heuristics did something silly —
    LitRPG system boxes have been read as chapter headings before, and a book that parsed
    into 400 two-line "chapters" is not one to extract from."""
    book = await _api("GET", f"/api/books/{book_id}")
    report = await _api("GET", f"/api/books/{book_id}/report")
    chapters = await _api("GET", f"/api/books/{book_id}/chapters")
    return {"book": book, "report": report, "chapter_count": len(chapters or [])}


@mcp.tool()
async def lore_chapter(book_id: int, position: int) -> dict:
    """The text of one chapter, by reading position. Use it to check a claim yourself
    rather than asking a model to remember."""
    return _obj(await _api("GET", f"/api/books/{book_id}/chapters/{position}"), "chapter")


@mcp.tool()
async def lore_search(book_id: int, question: str, k: int = 6) -> dict:
    """Cited retrieval — passages that answer a question, each with its chapter.

    No generation happens here: you get the book's own words back. Requires the index
    (`lore_index`). A confidently wrong citation is worse than a vague answer, so quote
    what comes back rather than paraphrasing it from memory."""
    return await _api("POST", f"/api/books/{book_id}/query",
                      json={"question": question, "k": max(1, min(25, k))})


@mcp.tool()
async def lore_characters(book_id: int, tier: str = "") -> dict:
    """The character census: who exists, their aliases, and their computed tier.

    Tier is *measured* (mentions, chapter spread, dialogue), never asked of a model. It
    decides how much work each character earns downstream — see `lore_dossier`. Optional
    `tier` filter: primary, secondary or filler."""
    return await _api("GET", f"/api/books/{book_id}/characters",
                      params={"tier": tier} if tier else None)


@mcp.tool()
async def lore_character_context(book_id: int, char_id: int, limit: int = 12) -> dict:
    """The passages behind a name — what the census actually saw.

    This is the tool for an alias judgement call. "Mom" and "Diane Fitzgerald" share no
    tokens, so no matching rule can ever propose that merge; only reading the passages
    settles it. Report the ambiguity rather than auto-resolving it."""
    return await _api("GET", f"/api/books/{book_id}/characters/{char_id}/mentions",
                      params={"limit": limit})


@mcp.tool()
async def lore_sheets(book_id: int) -> dict:
    """Which characters have written sheets, and how many facts each earned."""
    return await _api("GET", f"/api/books/{book_id}/sheets")


# --------------------------------------------------------------------------- #
# the handoff — what Persona Forge consumes
# --------------------------------------------------------------------------- #

@mcp.tool()
async def lore_dossier(book_id: int, char_id: int,
                       as_of_chapter: int | None = None) -> dict:
    """One character's dossier — the versioned handoff object for Persona Forge.

    Pass `as_of_chapter` to export only what a reader at that point in the book knows;
    `withheld_facts` then reports how much was held back, so you can see the spoiler
    control doing something. Omit it for the whole book.

    Feed the result straight to Persona Forge's `persona_create_from_dossier`. `plan`
    tells you what the character's tier earns: 28 expressions and a LoRA for a primary,
    8 for a secondary, a single neutral sprite for filler."""
    params = {"as_of": as_of_chapter} if as_of_chapter is not None else None
    sheet = await _api("GET", f"/api/books/{book_id}/sheets/{char_id}", params=params)
    dossier = sheet.get("dossier") or {}
    problems = handoff.validate(dossier)
    return {
        "dossier": dossier,
        "usable": not problems,
        "problems": problems,
        "plan": handoff.plan_for(str(dossier.get("tier") or "")),
        "withheld_facts": sheet.get("withheld"),
        "as_of_chapter": as_of_chapter,
    }


@mcp.tool()
async def lore_cast(book_id: int, as_of_chapter: int | None = None,
                    tiers: list[str] | None = None) -> dict:
    """Every describable character's dossier in one call — the input to a cast build.

    Defaults to primary + secondary, because filler characters have no sheet by design:
    a filler earns a lorebook line and nothing else. Characters whose sheet was never
    written come back under `unusable` **named**, with the reason — a cast build that
    silently drops four people is worse than one that says which four."""
    wanted = [t for t in (tiers or ["primary", "secondary"])]
    listing = await _api("GET", f"/api/books/{book_id}/characters")
    people = [c for c in listing.get("characters", [])
              if c.get("tier") in wanted and c.get("status") != "discarded"]

    cast: list[dict] = []
    unusable: list[dict] = []
    for person in people:
        try:
            got = await lore_dossier(book_id, int(person["id"]), as_of_chapter)
        except RuntimeError as exc:
            unusable.append({"name": person.get("name"), "reason": str(exc)})
            continue
        (cast if got["usable"] else unusable).append(
            got["dossier"] if got["usable"]
            else {"name": person.get("name"), "reason": "; ".join(got["problems"])})
    return {"book_id": book_id, "as_of_chapter": as_of_chapter,
            "cast": cast, "unusable": unusable,
            "counts": {"usable": len(cast), "unusable": len(unusable)}}


@mcp.tool()
async def lore_lorebook(book_id: int) -> dict:
    """The compiled lorebook for a book, in SillyTavern's own format.

    Read-only — `lore_compile_lorebook` is what rebuilds it. Compilation runs no model at
    all, so a rebuild after an edit is instant and always reflects the curated rows."""
    return await _api("GET", f"/api/books/{book_id}/lorebook")


# --------------------------------------------------------------------------- #
# queue — start work, then poll
# --------------------------------------------------------------------------- #

@mcp.tool()
async def lore_parse(book_id: int) -> dict:
    """L0: parse a book into ordered, titled chapters. Everything else gates on this.

    Check `lore_book`'s report afterwards before extracting — parse heuristics are the
    one place in this pipeline where a plausible-looking result can be nonsense."""
    return await _api("POST", f"/api/books/{book_id}/parse")


@mcp.tool()
async def lore_index(book_id: int, model: str = "") -> dict:
    """L1: chunk and embed the book so `lore_search` works. Only retrieval needs this —
    extraction reads chunk text and will build its own chunks if asked."""
    return await _api("POST", f"/api/books/{book_id}/index", json={"model": model})


@mcp.tool()
async def lore_census(book_id: int, model: str = "") -> dict:
    """L2 pass 1: who exists, what they are called, who matters.

    Lexical harvest first, then the model only prunes non-people and groups aliases; the
    engine computes each tier from mentions, chapter spread and dialogue. Queue it, then
    review with `lore_characters` — the census proposes and a human disposes."""
    return await _api("POST", f"/api/books/{book_id}/census", json={"model": model})


@mcp.tool()
async def lore_write_sheets(book_id: int, character_ids: list[int] | None = None,
                            as_of_chapter: int | None = None, model: str = "") -> dict:
    """L2 pass 2: write the character sheets as chapter-stamped facts.

    One model call per passage per character — this is the expensive pass, and the tier
    table is its only cost knob (primary reads 10 passages, secondary 4, filler none).
    Name `character_ids` to re-run one person after fixing their aliases instead of
    re-reading the whole cast.

    `as_of_chapter` only affects the dossiers written to disk; facts are always stored
    with their own stamp, so nothing is lost by picking a number here."""
    return await _api("POST", f"/api/books/{book_id}/sheets", json={
        "model": model, "character_ids": character_ids or [],
        "as_of_chapter": as_of_chapter})


@mcp.tool()
async def lore_extract(book_id: int, kind: str, model: str = "", limit: int = 0) -> dict:
    """L2: extract `rules` (progression systems), `world` (entities) or `quests`.

    Each quest carries its own reward and penalty — never generalise one quest's penalty
    into a universal law of the setting; that error shipped once already. Results land as
    *proposed* rows for review, not as fact."""
    if kind not in ("rules", "world", "quests"):
        raise ValueError("kind must be one of: rules, world, quests")
    return await _api("POST", f"/api/books/{book_id}/extract/{kind}",
                      json={"model": model, "limit": limit})


@mcp.tool()
async def lore_compile_lorebook(book_id: int, also_books: list[int] | None = None,
                                name: str = "", kept_only: bool = False,
                                include_rules: bool = True, include_quests: bool = True,
                                include_characters: bool = True) -> dict:
    """L3: compile the lorebook. Deterministic — no model runs, so this is instant.

    `also_books` merges several volumes into one lorebook, unioning entities and
    characters across them; a serialised webnovel split into eleven files is the normal
    case, not an edge case. Set `name` when the result is the series rather than any one
    volume. `kept_only=True` ships only human-reviewed rows."""
    return await _api("POST", f"/api/books/{book_id}/lorebook", json={
        "also_books": also_books or [], "name": name, "kept_only": kept_only,
        "include_rules": include_rules, "include_quests": include_quests,
        "include_characters": include_characters})


@mcp.tool()
async def lore_jobs(job_id: int | None = None) -> dict:
    """Job state. Everything queued above is polled here; jobs survive a restart and
    resume from their stored stage."""
    if job_id is not None:
        return await _api("GET", f"/api/jobs/{job_id}")
    return _obj(await _api("GET", "/api/jobs"), "jobs")


@mcp.tool()
async def lore_logs(level: str = "", category: str = "", search: str = "",
                    limit: int = 100) -> dict:
    """Recent log lines. Categories: boot, integration, process, local, api."""
    params = {"limit": limit}
    for key, value in (("level", level), ("category", category), ("search", search)):
        if value:
            params[key] = value
    return await _api("GET", "/api/logs", params=params)


# --------------------------------------------------------------------------- #
# mounting
# --------------------------------------------------------------------------- #

class _ASGIEndpoint:
    """A class, not a function, on purpose: Starlette treats a plain function endpoint as
    a request/response handler and only an object as a raw ASGI app. Wrapping the session
    manager this way lets the route sit at exactly `/mcp` — mounting a sub-app instead
    would serve `/mcp/` and answer `/mcp` with a 307, which not every client follows."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._manager.handle_request(scope, receive, send)


def install(app: Any, path: str = "/mcp") -> None:
    """Add the MCP endpoint to an existing FastAPI app. Call once, at import time."""
    global _APP
    _APP = app
    mcp.streamable_http_app()          # lazily builds the session manager
    app.router.routes.append(
        Route(path, endpoint=_ASGIEndpoint(mcp.session_manager)))


@contextlib.asynccontextmanager
async def session() -> AsyncIterator[None]:
    """Run the MCP session manager for the life of the app. Enter this from the app's
    lifespan — without it the endpoint accepts requests and then hangs."""
    async with mcp.session_manager.run():
        yield


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
