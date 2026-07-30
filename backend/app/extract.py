"""L2 — extraction. First target: the progression system (`rules/system.json`).

Why rules before characters: the genre *states* its own mechanics, usually in literal
system boxes, so this is the highest-signal, lowest-ambiguity thing a small local model
can be asked to pull out. It is the confidence test for every later extraction pass — if
a 12B model cannot reliably read a stat block, it certainly cannot read motivation.

Shape of the pass, and where the work is divided:

    engine   picks which chunks to read              (systext.py — no model)
    model    reads one chunk, emits structured facts (map)
    engine   validates, normalises, dedupes, cites   (reduce — this module)

The model never sees the whole book, never decides what is important, and never merges
anything. That is the standing rule — *database = truth, LLM = storyteller* — applied to
extraction: it reads and reports; the engine adjudicates.

**Transform, never reproduce.** The prompt demands a paraphrase, and `evidence_excerpt`
is hard-capped so a citation aid can never become a copy of the passage.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from . import llmjson

# The vocabulary the model is allowed to use. A closed set is what makes the output
# groupable; free-text kinds produce forty synonyms for "level up" and no way to merge.
RULE_KINDS = (
    "xp",         # how experience is earned or calculated
    "level",      # thresholds, what a level grants
    "attribute",  # stats that scale (strength, stamina, durability)
    "skill",      # acquisition, evolution, ranking of skills
    "class",      # class/job acquisition and evolution
    "currency",   # points, credits, any spendable resource
    "cap",        # hard limits and ceilings
    "penalty",    # costs, debuffs, death penalties
    "mechanic",   # anything else the system enforces
)
# `attribute` was added after the first live run: gemma3:12b filed "training increases
# Stamina" and "training increases Durability" under `skill`, because the closed
# vocabulary offered nowhere better. A missing kind doesn't produce a missing rule — it
# produces a miscategorised one, which is harder to notice.

CONFIDENCE = ("stated", "implied")

# Whether a rule governs the whole system or only one instance of something.
#
# Added after a real extraction filed "failing a quest costs body parts" as a universal
# law, when it was the stated penalty of ONE quest — and quests in this book each carry
# their own rewards and penalties. That error is worse than a miss: a wrong universal
# rule is confidently applied everywhere, and later passes inherit it.
SCOPES = ("system", "instance")

MAX_STATEMENT = 300
MAX_EVIDENCE = 200      # a citation aid, never a copy of the passage
MAX_NAME = 60

SYSTEM_PROMPT = """\
You extract game-progression rules from a novel's text.

You are reading one passage from a LitRPG novel. Some passages state the rules of the \
book's progression system (experience, levels, skills, classes, currencies, caps, \
penalties). Many passages state no rules at all.

Return ONLY a JSON object of this shape, with no commentary before or after:

{"rules": [
  {"kind": "<one of: xp|level|attribute|skill|class|currency|cap|penalty|mechanic>",
   "name": "<short label, under 60 characters>",
   "aliases": ["<other names or abbreviations the passage uses for this mechanic>"],
   "statement": "<the rule IN YOUR OWN WORDS, under 300 characters>",
   "scope": "<system if it governs the whole system, instance if it applies to one \
specific quest, item, character or contract>",
   "applies_to": "<when scope is instance, name the thing it applies to; else empty>",
   "formula": "<a formula if one is given, else empty string>",
   "confidence": "<stated if the passage says it outright, implied if inferred>",
   "evidence_excerpt": "<at most 20 words from the passage that show the rule>"}
]}

Hard requirements:
- Extract RULES, not events or current state. A rule holds whenever its conditions are \
met. "Each training session must last 60 minutes" is a rule. "He is currently level 26", \
"she gained 500 points in that fight", "his Stamina rose to 14" are NOT rules — they are \
one character's situation at one moment, and they will be false a chapter later. Skip them.
- DO NOT GENERALISE FROM ONE INSTANCE. If the passage gives the reward or penalty of a \
single named quest, contract or item, that is scope "instance", and "applies_to" names \
it. Only mark scope "system" when the passage says the mechanic governs everything of \
that type. A quest that costs a limb on failure does not mean all quests cost limbs — \
different quests carry different terms.
- If the passage states no progression rules, return {"rules": []}. This is common and \
correct. Do not invent a rule to fill the space.
- "statement" must be YOUR OWN paraphrase. Do not copy sentences from the passage.
- Never guess numbers. If a threshold or cost is not given, leave it out of the statement \
rather than estimating it.
- One rule per distinct mechanic. Do not restate the same mechanic twice. If one named \
mechanic does two things, describe both in a single rule rather than emitting it twice \
under different kinds.
- "name" names the MECHANIC, not the passage. Use the book's own term for it where there \
is one, so the same mechanic gets the same name every time it appears.
- "aliases" lists any abbreviation or alternative wording the passage uses for the same \
mechanic — if it writes "System Points" and also "SP", aliases are ["SP"]. Leave it as an \
empty list if the passage uses only one name.
- Output JSON only."""


def build_map_prompt(chunk: dict[str, Any]) -> str:
    """The per-chunk extraction prompt.

    Chapter context is included because a rule is often phrased relative to where it
    appears ("at this point he could still..."), and the model reads better with the
    anchor than without it.
    """
    where = f"Chapter {chunk.get('chapter_position', '?')}"
    title = (chunk.get("chapter_title") or "").strip()
    if title:
        where += f" — {title}"
    return f"Passage from {where}:\n\n---\n{chunk.get('text', '')}\n---\n\nExtract the progression rules as JSON."


# --------------------------------------------------------------------------- #
# normalisation — everything the model is not trusted to get right
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return _WS.sub(" ", value).strip()[:limit]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _alias_list(raw: Any, exclude: str = "", limit: int = 12) -> list[str]:
    """Clean an alias list from the model, dropping blanks, duplicates and the primary
    name itself. Accepts a comma-joined string too — models return that fairly often."""
    items: list[str] = []
    if isinstance(raw, list):
        items = [str(a) for a in raw]
    elif isinstance(raw, str):
        items = raw.split(",")
    out: list[str] = []
    seen = {exclude.strip().lower()} if exclude else set()
    for a in items:
        a = _clean(a, MAX_NAME)
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out[:limit]


def rule_key(rule: dict[str, Any]) -> str:
    """Identity for deduplication: kind plus a normalised name.

    Names arrive with cosmetic variation across chunks — "XP on kill", "XP On Kill",
    "xp-on-kill" — so the key folds case and punctuation. Two genuinely different
    mechanics that share a name will collide; that is the accepted trade, because the
    alternative (no merging) produces a rules file with the same rule forty times.
    """
    return f"{rule.get('kind', 'mechanic')}:{_slug(rule.get('name', ''))}"


def normalise_rule(raw: Any, chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and clean one model-emitted rule. Returns None if unusable.

    Rejecting is cheap and silent by design: one bad row out of a 12B model is normal,
    and a rules file is more useful slightly short than slightly wrong.
    """
    if not isinstance(raw, dict):
        return None

    name = _clean(raw.get("name"), MAX_NAME)
    statement = _clean(raw.get("statement"), MAX_STATEMENT)
    if not name or not statement:
        return None

    kind = _clean(raw.get("kind"), 20).lower()
    if kind not in RULE_KINDS:
        kind = "mechanic"      # coerce rather than drop: the rule may still be good

    confidence = _clean(raw.get("confidence"), 10).lower()
    if confidence not in CONFIDENCE:
        confidence = "implied"  # unmarked claims are treated as the weaker case

    evidence = _clean(raw.get("evidence_excerpt"), MAX_EVIDENCE)
    aliases = _alias_list(raw.get("aliases"), exclude=name)

    scope = _clean(raw.get("scope"), 10).lower()
    if scope not in SCOPES:
        scope = "system"
    applies_to = _clean(raw.get("applies_to"), MAX_NAME)
    # A stated applies_to contradicts a 'system' scope — trust the specific over the
    # general, since naming a target is the stronger signal.
    if applies_to and scope == "system":
        scope = "instance"

    citation = {
        "chapter": chunk.get("chapter_position"),
        "chapter_title": chunk.get("chapter_title", ""),
        "source_ref": chunk.get("source_ref", ""),
        "chunk_id": chunk.get("id"),
        "char_start": chunk.get("char_start"),
        "char_end": chunk.get("char_end"),
    }

    return {
        "id": hashlib.sha1(f"{kind}:{_slug(name)}".encode()).hexdigest()[:12],
        "kind": kind,
        "name": name,
        # Aliases matter for the same reason they do on world entities: at L3 a rule
        # becomes a lorebook entry, and an entry fires only on its keys. A mechanic the
        # book abbreviates ("System Points" / "SP") needs both or it rarely triggers.
        "aliases": aliases,
        "scope": scope,
        "applies_to": applies_to,
        "statement": statement,
        "formula": _clean(raw.get("formula"), 120),
        "confidence": confidence,
        "evidence_excerpt": evidence,
        "citations": [citation],
    }


def merge_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold duplicates, unioning their citations.

    A rule stated in five chapters is one rule with five citations, not five rules — and
    the citation count is itself signal: a mechanic the book restates repeatedly is a
    load-bearing one.

    On conflict the *stated* version beats the *implied* one; among equals the longer
    statement wins, on the assumption that it carries the extra clause.
    """
    merged: dict[str, dict[str, Any]] = {}
    for rule in rules:
        key = rule_key(rule)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(rule, aliases=list(rule.get("aliases") or []),
                               citations=list(rule["citations"]))
            continue

        seen = {(c.get("chunk_id"), c.get("chapter")) for c in existing["citations"]}
        for c in rule["citations"]:
            if (c.get("chunk_id"), c.get("chapter")) not in seen:
                existing["citations"].append(c)

        # Aliases union even when the statement doesn't win — a trigger seen in one
        # chapter is still a trigger, whichever chapter phrased the rule best.
        known = {a.lower() for a in existing["aliases"]} | {existing["name"].lower()}
        for a in rule.get("aliases", []):
            if a.lower() not in known:
                known.add(a.lower())
                existing["aliases"].append(a)

        upgrade = (rule["confidence"] == "stated" and existing["confidence"] != "stated")
        same_rank = rule["confidence"] == existing["confidence"]
        if upgrade or (same_rank and len(rule["statement"]) > len(existing["statement"])):
            existing["statement"] = rule["statement"]
            existing["confidence"] = rule["confidence"]
            if rule["formula"]:
                existing["formula"] = rule["formula"]
            if rule["evidence_excerpt"]:
                existing["evidence_excerpt"] = rule["evidence_excerpt"]

    out = list(merged.values())
    # Most-corroborated first, then by kind, so the file opens on load-bearing rules.
    out.sort(key=lambda r: (-len(r["citations"]), RULE_KINDS.index(r["kind"])
                            if r["kind"] in RULE_KINDS else 99, r["name"].lower()))
    return out


def parse_model_rules(text: str, chunk: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Model output -> clean rules. Returns `(rules, error)`; error is '' on success.

    An unparseable chunk is recorded and skipped, never retried blindly and never
    allowed to fail the whole run — a 200-chunk pass that dies on chunk 3 is useless.
    """
    try:
        raw_rules = llmjson.coerce_list(text, "rules")
    except llmjson.JSONRepairError as exc:
        return [], str(exc)

    out = []
    for raw in raw_rules:
        rule = normalise_rule(raw, chunk)
        if rule is not None:
            out.append(rule)
    return out, ""


# --------------------------------------------------------------------------- #
# world entities — the other half of L2, and the input to the L3 lorebook
# --------------------------------------------------------------------------- #

ENTITY_KINDS = ("location", "faction", "system", "artefact", "history", "term")

MAX_SUMMARY = 600
MAX_ALIASES = 12

WORLD_SYSTEM_PROMPT = """\
You extract world-building entities from a novel's text, for a lookup reference.

Read the passage and list the world entities it describes: places, organisations and \
factions, magic or technology systems, notable objects, historical events, and \
in-world terminology.

Return ONLY a JSON object of this shape, with no commentary before or after:

{"entities": [
  {"kind": "<one of: location|faction|system|artefact|history|term>",
   "name": "<the entity's primary name>",
   "aliases": ["<every other way the passage refers to it>"],
   "summary": "<what a reader needs to know, IN YOUR OWN WORDS, under 600 characters>"}
]}

Hard requirements:
- ALIASES ARE THE MOST IMPORTANT FIELD. List every shortened form, nickname, epithet and \
demonym the passage uses for the entity. If the text says "the Ashen Court", later "the \
Court", and calls its members "Ashenites", then aliases are ["the Court", "Ashenites"]. \
Missing an alias makes the entry useless.
- Do NOT list individual people as entities. Characters are handled separately.
- "summary" must be YOUR OWN words. Do not copy sentences from the passage.
- Describe what is durably true of the entity, not what happens to it in this scene.
- If the passage introduces no world entities, return {"entities": []}. This is common \
and correct. Do not invent entities.
- Output JSON only."""


def build_world_prompt(chunk: dict[str, Any]) -> str:
    where = f"Chapter {chunk.get('chapter_position', '?')}"
    title = (chunk.get("chapter_title") or "").strip()
    if title:
        where += f" — {title}"
    return (f"Passage from {where}:\n\n---\n{chunk.get('text', '')}\n---\n\n"
            "Extract the world entities as JSON.")


def entity_key(entity: dict[str, Any]) -> str:
    return f"{entity.get('kind', 'term')}:{_slug(entity.get('name', ''))}"


def normalise_entity(raw: Any, chunk: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name"), MAX_NAME)
    summary = _clean(raw.get("summary"), MAX_SUMMARY)
    if not name or not summary:
        return None

    kind = _clean(raw.get("kind"), 20).lower()
    if kind not in ENTITY_KINDS:
        kind = "term"

    aliases = _alias_list(raw.get("aliases"), exclude=name, limit=MAX_ALIASES)

    return {
        "id": hashlib.sha1(entity_key({"kind": kind, "name": name}).encode()).hexdigest()[:12],
        "kind": kind,
        "name": name,
        "aliases": aliases,
        "summary": summary,
        "citations": [{
            "chapter": chunk.get("chapter_position"),
            "chapter_title": chunk.get("chapter_title", ""),
            "source_ref": chunk.get("source_ref", ""),
            "chunk_id": chunk.get("id"),
        }],
    }


def merge_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold duplicates, **unioning aliases** as well as citations.

    Alias union is the point of doing this at all: chapter 2 calls it "the Ashen Court",
    chapter 9 calls it "the Court". Merging by name alone would keep one spelling and
    lose the other, and the lost one is exactly the trigger that would have fired.
    """
    merged: dict[str, dict[str, Any]] = {}
    for entity in entities:
        key = entity_key(entity)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(entity, aliases=list(entity["aliases"]),
                               citations=list(entity["citations"]))
            continue

        seen = {a.lower() for a in existing["aliases"]} | {existing["name"].lower()}
        for a in entity["aliases"]:
            if a.lower() not in seen:
                seen.add(a.lower())
                existing["aliases"].append(a)

        cited = {(c.get("chunk_id"), c.get("chapter")) for c in existing["citations"]}
        for c in entity["citations"]:
            if (c.get("chunk_id"), c.get("chapter")) not in cited:
                existing["citations"].append(c)

        # Prefer the fuller description; a later chapter usually knows more.
        if len(entity["summary"]) > len(existing["summary"]):
            existing["summary"] = entity["summary"]

    out = list(merged.values())
    out.sort(key=lambda e: (-len(e["citations"]),
                            ENTITY_KINDS.index(e["kind"]) if e["kind"] in ENTITY_KINDS else 99,
                            e["name"].lower()))
    return out


def parse_model_entities(text: str, chunk: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    try:
        raw_items = llmjson.coerce_list(text, "entities")
    except llmjson.JSONRepairError as exc:
        return [], str(exc)
    out = []
    for raw in raw_items:
        entity = normalise_entity(raw, chunk)
        if entity is not None:
            out.append(entity)
    return out, ""


# --------------------------------------------------------------------------- #
# character census, pass 1b — the model's only two jobs
# --------------------------------------------------------------------------- #

CENSUS_SYSTEM_PROMPT = """\
You are cleaning a list of candidate names harvested from a novel by a text scanner.

The scanner found every capitalised phrase, so the list contains real characters mixed \
with place names, game or magic terminology, organisations, and scanning mistakes. You \
have two jobs and no others:

1. Decide which candidates are PEOPLE (characters), and which are not.
2. Group the surface forms that refer to the SAME person.

Return ONLY a JSON object of this shape, with no commentary:

{"people": [
   {"name": "<the fullest, most complete form of the name>",
    "aliases": ["<every other listed form for this same person>"],
    "note": "<a few words on who they are, if the context makes it clear, else empty>"}
 ],
 "not_people": ["<candidate>", "<candidate>"]}

Hard requirements:
- Every candidate given to you must appear exactly once, either inside one "people" \
entry (as the name or an alias) or in "not_people". Do not invent candidates.
- "Diane" and "Diane Fitzgerald" are the SAME person: name is "Diane Fitzgerald", \
aliases ["Diane"]. Group given names with full names, and nicknames with both.
- Do NOT group two different people who share a surname or a first name. If you are \
unsure whether two forms are the same person, keep them separate.
- Game mechanics, stats, abilities, item names, places, factions and species are \
NOT people. Neither are fragments that are obviously a scanning error.
- Output JSON only."""


def build_census_prompt(candidates: list[dict[str, Any]]) -> str:
    """One prompt covering a batch of candidates.

    Counts are included because they carry real signal the text does not: a form
    appearing in 37 chapters with 31 speech acts is a protagonist, and one appearing once
    is probably noise. Snippets are short and transient — they exist to disambiguate, and
    are never stored.
    """
    lines = []
    for c in candidates:
        bits = [f"- \"{c['name']}\"",
                f"({c['mentions']} mentions, {c['chapter_count']} chapters,"
                f" {c['dialogue_hits']} speech acts)"]
        for snippet in c.get("snippets", [])[:1]:
            bits.append(f"| context: …{snippet}…")
        lines.append(" ".join(bits))
    return ("Candidates:\n" + "\n".join(lines)
            + "\n\nGroup the people and list the non-people, as JSON.")


def parse_census(text: str, known: list[str]) -> tuple[list[dict[str, Any]], list[str], str]:
    """Model output -> `(people, not_people, error)`.

    Anything the model silently dropped is returned as unresolved rather than lost: a
    12B model asked to account for fifty candidates will forget some, and a forgotten
    character is invisible downstream.
    """
    try:
        doc = llmjson.coerce_object(text)
    except llmjson.JSONRepairError as exc:
        return [], [], str(exc)

    if "_list" in doc:                      # bare list came back
        doc = {"people": doc["_list"], "not_people": []}

    known_lower = {k.lower(): k for k in known}
    seen: set[str] = set()
    people: list[dict[str, Any]] = []

    for raw in doc.get("people") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean(raw.get("name"), MAX_NAME)
        if not name:
            continue
        aliases = _alias_list(raw.get("aliases"), exclude=name)
        # Only keep surface forms the scanner actually saw — the model must not invent
        # names, and a hallucinated alias would poison the lorebook keys.
        members = [name] + aliases
        kept = [known_lower[m.lower()] for m in members if m.lower() in known_lower]
        if not kept:
            continue
        for m in kept:
            seen.add(m.lower())
        primary = max(kept, key=len)        # fullest form wins
        people.append({
            "name": primary,
            "aliases": [k for k in kept if k != primary],
            "note": _clean(raw.get("note"), 120),
        })

    not_people = []
    for raw in doc.get("not_people") or []:
        candidate = _clean(raw, MAX_NAME)
        if candidate and candidate.lower() in known_lower:
            not_people.append(known_lower[candidate.lower()])
            seen.add(candidate.lower())

    unresolved = [k for k in known if k.lower() not in seen]
    return people, not_people, ("" if not unresolved
                                else f"{len(unresolved)} candidate(s) unaccounted for")


RECONCILE_SYSTEM_PROMPT = """\
You are checking whether pairs of names from one novel refer to the SAME character.

Return ONLY a JSON object, no commentary:

{"same": [["<shorter name>", "<longer name>"], ...]}

List a pair ONLY when you are confident both names refer to one person — a given name \
and that person's full name, or a nickname and its formal form.

Do NOT list a pair when the two could be different people who merely share a name: a \
parent and child, two siblings, or an unrelated character with the same first name. If \
you are unsure, leave the pair out. A missed merge is easily fixed by hand; a wrong \
merge silently fuses two characters into one.

Output JSON only."""


def build_reconcile_prompt(pairs: list[tuple[str, str]],
                           stats: dict[str, dict[str, Any]]) -> str:
    """Ask about a shortlist of candidate merges, with the evidence for each side."""
    lines = []
    for short, long in pairs:
        s, l = stats.get(short, {}), stats.get(long, {})
        lines.append(
            f'- "{short}" ({s.get("mentions", 0)} mentions, '
            f'{s.get("chapter_count", 0)} chapters, {s.get("dialogue_hits", 0)} speech acts)'
            f'  vs  "{long}" ({l.get("mentions", 0)} mentions, '
            f'{l.get("chapter_count", 0)} chapters, {l.get("dialogue_hits", 0)} speech acts)')
    return ("Do these name pairs refer to the same character?\n"
            + "\n".join(lines) + "\n\nAnswer as JSON.")


def parse_reconcile(text: str, pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    """Model output -> confirmed merge pairs, restricted to the ones actually asked."""
    try:
        doc = llmjson.coerce_object(text)
    except llmjson.JSONRepairError as exc:
        return [], str(exc)
    raw = doc.get("_list") if "_list" in doc else doc.get("same")
    if not isinstance(raw, list):
        return [], ""
    asked = {(a.lower(), b.lower()) for a, b in pairs}
    lookup = {(a.lower(), b.lower()): (a, b) for a, b in pairs}
    out = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            continue
        a, b = _clean(item[0], MAX_NAME), _clean(item[1], MAX_NAME)
        for key in ((a.lower(), b.lower()), (b.lower(), a.lower())):
            if key in asked:
                out.append(lookup[key])
                break
    return out, ""


# --------------------------------------------------------------------------- #
# quests — the journey, and the right home for per-quest terms
# --------------------------------------------------------------------------- #

MAX_FIELD = 300

QUEST_SYSTEM_PROMPT = """\
You extract QUESTS from a novel's text.

A quest is a specific task the system, or a character, sets for someone — with its own \
objective and usually its own reward and its own penalty for failure. Different quests \
carry DIFFERENT terms: one quest's punishment is not a rule about all quests.

Return ONLY a JSON object of this shape, with no commentary before or after:

{"quests": [
  {"name": "<the quest's name or title as the book gives it>",
   "aliases": ["<other ways the passage refers to this quest>"],
   "kind": "<one of: main|side|hidden|optional|tutorial|unknown>",
   "objective": "<what must be done, IN YOUR OWN WORDS, under 300 characters>",
   "giver": "<who or what issued it, else empty string>",
   "requirements": "<conditions to accept or start it, else empty string>",
   "reward": "<what completing it grants, else empty string>",
   "penalty": "<what failing it costs, else empty string>",
   "deadline": "<any time limit stated, else empty string>",
   "outcome": "<one of: accepted|completed|failed|declined|ongoing|unknown>"}
]}

Hard requirements:
- Each quest is one entry. Its reward and penalty belong to THAT quest only. Never \
describe one quest's terms as if they applied to all quests.
- If the passage names no quest, return {"quests": []}. This is common and correct.
- Use the book's own name for the quest so the same quest merges across chapters. If it \
is unnamed, describe it in a few words instead.
- All text must be YOUR OWN words. Do not copy sentences from the passage.
- Never invent a reward, penalty or deadline. Leave the field empty if it is not stated.
- Output JSON only."""

QUEST_KINDS = ("main", "side", "hidden", "optional", "tutorial", "unknown")
QUEST_OUTCOMES = ("accepted", "completed", "failed", "declined", "ongoing", "unknown")


def build_quest_prompt(chunk: dict[str, Any]) -> str:
    where = f"Chapter {chunk.get('chapter_position', '?')}"
    title = (chunk.get("chapter_title") or "").strip()
    if title:
        where += f" — {title}"
    return (f"Passage from {where}:\n\n---\n{chunk.get('text', '')}\n---\n\n"
            "Extract the quests as JSON.")


def quest_key(quest: dict[str, Any]) -> str:
    return _slug(quest.get("name", ""))


def normalise_quest(raw: Any, chunk: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name"), MAX_NAME)
    objective = _clean(raw.get("objective"), MAX_FIELD)
    if not name or not objective:
        return None

    kind = _clean(raw.get("kind"), 12).lower()
    if kind not in QUEST_KINDS:
        kind = "unknown"
    outcome = _clean(raw.get("outcome"), 12).lower()
    if outcome not in QUEST_OUTCOMES:
        outcome = "unknown"

    return {
        "id": hashlib.sha1(quest_key({"name": name}).encode()).hexdigest()[:12],
        "name": name,
        "aliases": _alias_list(raw.get("aliases"), exclude=name),
        "kind": kind,
        "objective": objective,
        "giver": _clean(raw.get("giver"), MAX_NAME),
        "requirements": _clean(raw.get("requirements"), MAX_FIELD),
        "reward": _clean(raw.get("reward"), MAX_FIELD),
        "penalty": _clean(raw.get("penalty"), MAX_FIELD),
        "deadline": _clean(raw.get("deadline"), MAX_NAME),
        "outcome": outcome,
        # The journey is reading order, so a quest is placed by where it FIRST appears.
        "first_chapter": chunk.get("chapter_position") or 0,
        "citations": [{
            "chapter": chunk.get("chapter_position"),
            "chapter_title": chunk.get("chapter_title", ""),
            "source_ref": chunk.get("source_ref", ""),
            "chunk_id": chunk.get("id"),
        }],
    }


# Once a quest resolves it stays resolved; a later passage merely mentioning it must not
# drag it back to 'ongoing'. Higher wins.
_OUTCOME_RANK = {"unknown": 0, "accepted": 1, "ongoing": 2, "declined": 3,
                 "failed": 4, "completed": 5}


def merge_quests(quests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold repeat mentions of the same quest.

    Fields fill in rather than overwrite: chapter 3 may name the reward and chapter 9 the
    penalty, and the merged quest should carry both. `first_chapter` keeps the earliest
    sighting, because that is the quest's position in the journey.
    """
    merged: dict[str, dict[str, Any]] = {}
    for quest in quests:
        key = quest_key(quest)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(quest, aliases=list(quest["aliases"]),
                               citations=list(quest["citations"]))
            continue

        known = {a.lower() for a in existing["aliases"]} | {existing["name"].lower()}
        for a in quest["aliases"]:
            if a.lower() not in known:
                known.add(a.lower())
                existing["aliases"].append(a)

        cited = {(c.get("chunk_id"), c.get("chapter")) for c in existing["citations"]}
        for c in quest["citations"]:
            if (c.get("chunk_id"), c.get("chapter")) not in cited:
                existing["citations"].append(c)

        # Fill empties; prefer the longer text where both are present.
        for field in ("objective", "giver", "requirements", "reward", "penalty", "deadline"):
            new, old = quest.get(field, ""), existing.get(field, "")
            if new and len(new) > len(old):
                existing[field] = new
        if quest["kind"] != "unknown" and existing["kind"] == "unknown":
            existing["kind"] = quest["kind"]
        if _OUTCOME_RANK[quest["outcome"]] > _OUTCOME_RANK[existing["outcome"]]:
            existing["outcome"] = quest["outcome"]
        existing["first_chapter"] = min(existing["first_chapter"] or 10**6,
                                        quest["first_chapter"] or 10**6)

    # Journey order: where each quest first appears.
    return sorted(merged.values(), key=lambda q: (q["first_chapter"], q["name"].lower()))


def parse_model_quests(text: str, chunk: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    try:
        raw_items = llmjson.coerce_list(text, "quests")
    except llmjson.JSONRepairError as exc:
        return [], str(exc)
    out = []
    for raw in raw_items:
        quest = normalise_quest(raw, chunk)
        if quest is not None:
            out.append(quest)
    return out, ""


def build_journey(book: dict[str, Any], quests: list[dict[str, Any]],
                  model: str) -> dict[str, Any]:
    """`campaign/story/quests.json` — the quests in the order the book meets them.

    Deliberately NOT merged into `rules/system.json`: a quest's reward and penalty are
    that quest's terms, and flattening them into system rules is exactly the error that
    made this artefact necessary.
    """
    by_kind: dict[str, int] = {}
    for q in quests:
        by_kind[q["kind"]] = by_kind.get(q["kind"], 0) + 1
    return {
        "written_by": "lore-forge",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "model": model,
        "counts": {"quests": len(quests), "by_kind": by_kind,
                   "with_reward": sum(1 for q in quests if q.get("reward")),
                   "with_penalty": sum(1 for q in quests if q.get("penalty"))},
        "journey": quests,
    }


def find_conflicts(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same mechanic name filed under two different kinds.

    Found on the first live run: the book's "Temptation Gauge" came back once as
    `mechanic` and once as `skill`, and since the merge identity is `kind:name` the two
    never folded together.

    These are *reported*, not auto-merged. Two genuinely different rules can share a name
    — a "Stamina" cap and a "Stamina" attribute are both legitimate — so collapsing them
    blindly would destroy information. Surfacing them puts the judgement where the design
    already says it belongs: with the human doing curation.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        by_name.setdefault(_slug(r["name"]), []).append(r)
    conflicts = []
    for name_slug, group in sorted(by_name.items()):
        kinds = sorted({r["kind"] for r in group})
        if len(kinds) > 1:
            conflicts.append({
                "name": group[0]["name"],
                "kinds": kinds,
                "rule_ids": [r["id"] for r in group],
                "note": ("the same mechanic name was filed under more than one kind — "
                         "merge them by hand if they are one rule"),
            })
    return conflicts


def build_document(book: dict[str, Any], rules: list[dict[str, Any]],
                   model: str, stats: dict[str, Any]) -> dict[str, Any]:
    """The `campaign/rules/system.json` artefact."""
    system_rules = [r for r in rules if r.get("scope", "system") == "system"]
    instance_rules = [r for r in rules if r.get("scope", "system") == "instance"]
    by_kind: dict[str, int] = {}
    for r in system_rules:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return {
        "written_by": "lore-forge",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "model": model,
        "extraction": stats,
        "counts": {"rules": len(system_rules), "instance_rules": len(instance_rules),
                   "by_kind": by_kind,
                   "stated": sum(1 for r in rules if r.get("confidence") == "stated"),
                   "implied": sum(1 for r in rules if r.get("confidence") == "implied")},
        # Reported, never auto-resolved — see find_conflicts.
        "conflicts": find_conflicts(rules),
        # Split, not mixed: a universal law and one quest's terms are different claims,
        # and merging them is what produced "all quests cost a limb on failure".
        "rules": system_rules,
        "instance_rules": instance_rules,
    }
