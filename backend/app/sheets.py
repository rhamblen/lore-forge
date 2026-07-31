"""L2 pass 2 — the character sheets, as chapter-stamped facts.

You cannot write a character sheet chunk by chunk. A rule is stated in one place; a
character is spread over forty chapters, so pass 1 (`census.py`) establishes *who exists
and what they are called*, and this pass writes *who they are* — per character, from the
passages that are actually about them.

    engine   picks this character's passages, in reading order   (no model)
    model    reads ONE passage and reports what it shows         (bounded, fallible)
    engine   stamps the chapter, dedupes, cites, persists        (this module)

**The chapter stamp is why the loop is per passage.** The spoiler rule — decided
2026-07-30 — is that every fact records the chapter it became true, so a sheet exports
"as of chapter N" and a card handed to a reader twenty chapters in does not know the
reveals. Asking a model which chapter a claim came from would be asking it to remember,
which the standing rule forbids and which it is bad at. Feeding it one passage whose
chapter the engine already knows makes the stamp **correct by construction**: the model
never sees the chapter number and cannot get it wrong.

That is also why a sheet is stored as facts rather than as prose. Prose blends chapter 3
and chapter 39 into one paragraph, and no later filter can separate them again.

**Detail scales with tier** (`PROJECT_PLAN.md` §4): a filler character earns a lorebook
line and nothing else, and never reaches this pass at all.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import llmjson

# The closed field vocabulary. Closed for the same reason rule kinds are: free-text
# fields produce forty synonyms for "wants revenge" and nothing groups.
#
# Deliberately absent: greeting, scenario and example dialogue. Those are *written* for a
# card, not *observed* in a passage, so there is no chapter to stamp them with — they
# belong to L4 card assembly, synthesised from these facts.
FIELDS = (
    "role",          # what they are in the story — station, job, function
    "appearance",    # what they look like; feeds the Persona Forge looks prompt
    "personality",   # disposition and temperament
    "motivation",    # what they want and why — the charter ranks this above biography
    "speech",        # register, verbal habits, how they address people
    "quirks",        # mannerisms, tells, running habits
    "relationship",  # ties to a named other; `subject` carries who
)

# Which fields each tier earns, from the tier table in PROJECT_PLAN.md §4.
#
# `filler` is absent on purpose rather than empty: a filler character is not "a sheet
# with no fields", it is a character who does not get a sheet. The job skips them.
TIER_FIELDS: dict[str, tuple[str, ...]] = {
    "primary": FIELDS,
    "secondary": ("role", "appearance", "speech", "relationship", "motivation"),
}

# Passages read per character, by tier. This is the entire cost knob for the pass: one
# model call per passage per character. Primary characters earn the depth; secondary
# ones get enough to fill a lorebook entry and a thin card.
TIER_PASSAGES = {"primary": 10, "secondary": 4}

MAX_FACT = 240          # a fact is a claim, not a paragraph
MAX_SUBJECT = 60

_WS = re.compile(r"\s+")


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return _WS.sub(" ", value).strip()[:limit]


def fields_for(tier: str) -> tuple[str, ...]:
    return TIER_FIELDS.get(tier, ())


def passages_for(tier: str) -> int:
    return TIER_PASSAGES.get(tier, 0)


def wants_sheet(character: dict[str, Any]) -> bool:
    """Filler characters never reach pass 2 — a lorebook line is all they earn."""
    return bool(TIER_FIELDS.get(character.get("tier", "filler")))


# --------------------------------------------------------------------------- #
# selecting what the model reads — engine only, no model
# --------------------------------------------------------------------------- #

def mentions_in(text: str, forms: list[str]) -> int:
    """How many times any surface form of this character appears in a passage."""
    total = 0
    for form in forms:
        if not form.strip():
            continue
        total += len(re.findall(rf"\b{re.escape(form)}\b", text))
    return total


def select_passages(chunks: list[dict[str, Any]], forms: list[str],
                    limit: int) -> list[dict[str, Any]]:
    """The passages worth spending a model call on for this character.

    Lexical, and unapologetically so: a name is a surface feature, which is the same
    argument that makes the census lexical. A passage that never names the character is
    usually a passage where they are "he" — and a model reading it in isolation cannot
    tell whose "he" it is either, so retrieving it would buy a confident wrong answer.

    Ranking is by mention density, not raw count, so a short passage that is *about* the
    character beats a long one that name-drops them once. Ties break on reading order,
    which matters for the chapter stamp: given equal evidence, prefer the earliest
    sighting, because the earliest chapter a fact is visible is the honest stamp.
    """
    scored = []
    for chunk in chunks:
        text = chunk.get("text", "") or ""
        hits = mentions_in(text, forms)
        if not hits:
            continue
        density = hits / max(len(text), 1) * 1000
        scored.append((round(density, 4), hits, chunk))

    scored.sort(key=lambda s: (-s[0], -s[1], s[2].get("position", 0)))
    picked = [c for _, _, c in scored[:max(0, limit)]]
    # Hand them to the model in reading order. It does not change what is extracted, but
    # it makes a half-finished run's facts cover the early book rather than a scatter.
    picked.sort(key=lambda c: (c.get("chapter_position", 0), c.get("position", 0)))
    return picked


# --------------------------------------------------------------------------- #
# the model's job — one passage, what does it show about this person
# --------------------------------------------------------------------------- #

SHEET_SYSTEM_PROMPT = """\
You are reading ONE passage from a novel and reporting what it shows about ONE character.

Return ONLY a JSON object of this shape, with no commentary before or after:

{"facts": [
  {"field": "<one of: role|appearance|personality|motivation|speech|quirks|relationship>",
   "text": "<the claim, IN YOUR OWN WORDS, under 240 characters>",
   "subject": "<for a relationship: the other person's name. Otherwise empty string>"}
]}

Hard requirements:
- Report ONLY what THIS passage shows. Do not use anything you know about this character \
from elsewhere, and do not guess at what happens later.
- If the passage shows nothing about them, return {"facts": []}. This is common and \
correct — a passage can name someone without revealing anything.
- One claim per fact. "Tall, and bitter about his brother" is two facts, and the second \
one is a relationship.
- All text must be YOUR OWN words. Do not copy sentences from the passage.
- Never state a motive the passage does not support. "He wants revenge" needs the passage \
to show it, not merely to show him angry.
- Do not mention chapters, the book, the reader, or the passage itself. Write claims \
about the person.
- Output JSON only."""


def build_sheet_prompt(character: dict[str, Any], chunk: dict[str, Any],
                       fields: tuple[str, ...]) -> str:
    """One passage, one character, and the fields this character's tier earns.

    The alias list is included because the passage may only ever use a nickname, and a
    model told to report on "Diane Fitzgerald" while reading a passage that says "Mom"
    will honestly report that she is absent.
    """
    forms = [character.get("name", ""), *(character.get("aliases") or [])]
    known = ", ".join(f'"{f}"' for f in forms if f)
    where = f"Chapter {chunk.get('chapter_position', '?')}"
    title = (chunk.get("chapter_title") or "").strip()
    if title:
        where += f" — {title}"
    return (f"Character: {character.get('name', '')}\n"
            f"Also called: {known}\n"
            f"Report only these fields: {', '.join(fields)}\n\n"
            f"Passage from {where}:\n\n---\n{chunk.get('text', '')}\n---\n\n"
            "What does this passage show about this character? Answer as JSON.")


def fact_key(field: str, text: str, subject: str = "") -> str:
    """Dedupe identity. Normalised hard on purpose: the same observation phrased two ways
    across two chapters is one fact, and a sheet listing it twice reads as sloppy."""
    norm = re.sub(r"[^a-z0-9 ]+", "", text.lower())
    norm = " ".join(sorted(w for w in norm.split() if len(w) > 3))
    return f"{field}:{subject.lower().strip()}:{norm}"[:300]


def normalise_fact(raw: Any, chunk: dict[str, Any],
                   allowed: tuple[str, ...]) -> dict[str, Any] | None:
    """Model output -> one stamped fact, or None.

    The chapter and citation come from `chunk` — the passage the engine chose — and are
    never read from the model's reply. That is the guarantee the whole spoiler scheme
    rests on: a fact cannot be stamped with a chapter the model made up.
    """
    if not isinstance(raw, dict):
        return None
    field = _clean(raw.get("field"), 20).lower()
    text = _clean(raw.get("text"), MAX_FACT)
    if field not in allowed or not text:
        return None
    subject = _clean(raw.get("subject"), MAX_SUBJECT) if field == "relationship" else ""
    # A relationship with nobody on the other end is not a relationship; it is usually
    # the model restating a personality trait under the wrong field.
    if field == "relationship" and not subject:
        return None
    return {
        "field": field,
        "text": text,
        "subject": subject,
        "chapter": chunk.get("chapter_position") or 0,
        "citation": chunk.get("citation", ""),
        "chunk_id": chunk.get("id") or 0,
        "fact_key": fact_key(field, text, subject),
    }


def parse_facts(text: str, chunk: dict[str, Any],
                allowed: tuple[str, ...]) -> tuple[list[dict[str, Any]], str]:
    try:
        raw_items = llmjson.coerce_list(text, "facts")
    except llmjson.JSONRepairError as exc:
        return [], str(exc)
    out = []
    for raw in raw_items:
        fact = normalise_fact(raw, chunk, allowed)
        if fact is not None:
            out.append(fact)
    return out, ""


# --------------------------------------------------------------------------- #
# reading the sheet back — including "as of chapter N"
# --------------------------------------------------------------------------- #

def as_of(facts: list[dict[str, Any]], chapter: int | None) -> list[dict[str, Any]]:
    """The facts a reader who has reached `chapter` is allowed to know.

    This is the spoiler control in one line, and it only works because the stamp is on
    the fact rather than on the sheet. `None` means no cutoff — the whole book.
    """
    if chapter is None:
        return list(facts)
    return [f for f in facts if (f.get("chapter") or 0) <= chapter]


def group(facts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Facts by field, each field in chapter order — which is the order they became
    true, and therefore the order a sheet reads best in."""
    out: dict[str, list[dict[str, Any]]] = {}
    for fact in sorted(facts, key=lambda f: (f.get("chapter") or 0, f.get("id") or 0)):
        out.setdefault(fact["field"], []).append(fact)
    return out


def build_dossier(book: dict[str, Any], character: dict[str, Any],
                  facts: list[dict[str, Any]], model: str,
                  chapter: int | None = None) -> dict[str, Any]:
    """`campaign/dossiers/<name>.json` — the merge contract with Persona Forge.

    Shaped as the design's B1 artefact, so Character Studio can prefill from it rather
    than elicit from a one-line seed. Two fields carry the handoff explicitly:
    `tier`, which is what PF reads to decide sprite counts, and `appearance`, which is
    what becomes the looks prompt — prose, never tags, per the standing rule.

    `as_of` is recorded even when null, because a dossier that does not say what it
    knows is one you cannot safely hand to a reader mid-series.
    """
    visible = as_of(facts, chapter)
    grouped = group(visible)
    return {
        "written_by": "lore-forge",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "model": model,
        "name": character.get("name", ""),
        "aliases": character.get("aliases") or [],
        # The handoff to Persona Forge. Lore Forge holds no expression logic of its own.
        "tier": character.get("tier", "filler"),
        "note": character.get("note", ""),
        "presence": {
            "mentions": character.get("mentions", 0),
            "dialogue_hits": character.get("dialogue_hits", 0),
            "chapters": character.get("chapter_count", 0),
            "first_chapter": character.get("first_chapter", 0),
            "last_chapter": character.get("last_chapter", 0),
        },
        # null = the whole book. A number means every fact after it was withheld.
        "as_of_chapter": chapter,
        "withheld_facts": len(facts) - len(visible),
        "fields": {
            field: [{"text": f["text"], "subject": f["subject"],
                     "chapter": f["chapter"], "citation": f["citation"]}
                    for f in rows]
            for field, rows in grouped.items()
        },
    }


def summarise(facts: list[dict[str, Any]]) -> dict[str, Any]:
    by_field: dict[str, int] = {}
    for f in facts:
        by_field[f["field"]] = by_field.get(f["field"], 0) + 1
    chapters = [f.get("chapter") or 0 for f in facts]
    return {
        "facts": len(facts),
        "by_field": by_field,
        "first_chapter": min(chapters) if chapters else 0,
        "last_chapter": max(chapters) if chapters else 0,
    }
