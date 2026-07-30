"""L3 — compile a SillyTavern lorebook.

The first output SillyTavern can actually load, and therefore the first rung with an
external contract to honour. The schema below was read from a real lorebook on the user's
own install, not from documentation: entries are a **map keyed by uid string**, not a
list, and every field carries a default that ST expects to be present.

**No model runs here.** The lorebook is compiled deterministically from curated database
rows — extraction already happened at L2. That keeps the standing rule intact (database =
truth) and means rebuilding after an edit is instant.

Two things decide whether a lorebook is any good, and neither is the prose:

1. **Keys.** An entry only fires when a key appears in the chat. "the Ashen Court", "the
   Court" and "Ashenites" must all be keys on one entry or it silently never triggers.
   Alias harvesting is the whole game; this module is careful never to drop one.
2. **Order and position.** World *rules* must sit deeper in context than trivia, or a
   long chat evicts the mechanics and keeps the flavour text.
"""

from __future__ import annotations

import re
from typing import Any

# Per-kind insertion policy. `order` is ST's priority (higher wins when context is
# tight); `position` 0 = before character definitions, 1 = after.
#
# Systems and factions outrank terminology on purpose: if the context budget forces a
# choice, the reader needs the rules of the world more than a place name's etymology.
KIND_POLICY: dict[str, dict[str, Any]] = {
    "system":    {"order": 100, "position": 0, "constant": False},
    "quest":     {"order": 95,  "position": 0, "constant": False},
    "faction":   {"order": 90,  "position": 0, "constant": False},
    "location":  {"order": 80,  "position": 0, "constant": False},
    "artefact":  {"order": 70,  "position": 0, "constant": False},
    "history":   {"order": 60,  "position": 0, "constant": False},
    "term":      {"order": 50,  "position": 0, "constant": False},
}
DEFAULT_POLICY = {"order": 50, "position": 0, "constant": False}

ENTRY_KINDS = tuple(KIND_POLICY)

# ST truncates very long entries awkwardly, and a lorebook entry earns its context by
# being dense. This is a guard rail, not a target.
MAX_CONTENT = 1200


def _clean_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip(",;")


def build_keys(name: str, aliases: list[str] | None) -> list[str]:
    """The trigger list: the name plus every alias, de-duplicated case-insensitively
    while keeping the original casing of the first spelling seen.

    Order matters only cosmetically, but the *count* matters a lot — a missing alias is
    an entry that never fires, and that failure is silent.
    """
    out: list[str] = []
    seen: set[str] = set()
    for candidate in [name, *(aliases or [])]:
        key = _clean_key(candidate)
        if not key:
            continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(key)
    return out


def build_entry(uid: int, kind: str, name: str, content: str,
                aliases: list[str] | None = None,
                comment: str = "", display_index: int | None = None) -> dict[str, Any]:
    """One ST world-info entry, with every field ST expects present.

    Defaults are chosen to be *quiet*: not constant (so it only fires on a key), not
    vectorised, no probability roll. An entry that fires unpredictably is worse than one
    that fires rarely, because it costs context on turns where it is irrelevant.
    """
    policy = KIND_POLICY.get(kind, DEFAULT_POLICY)
    keys = build_keys(name, aliases)
    return {
        "uid": uid,
        "key": keys,
        "keysecondary": [],
        "comment": comment or f"{kind}: {name}",
        "content": (content or "").strip()[:MAX_CONTENT],
        "constant": policy["constant"],
        "selective": True,
        "order": policy["order"],
        "position": policy["position"],
        "disable": False,
        "displayIndex": uid if display_index is None else display_index,
        "addMemo": True,
        "group": "",
        "groupOverride": False,
        "groupWeight": 100,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "probability": 100,
        "depth": 4,
        "useProbability": True,
        "role": 0,
        "vectorized": False,
        "excludeRecursion": False,
        "preventRecursion": False,
        "delayUntilRecursion": False,
        "scanDepth": None,
        "caseSensitive": None,
        "matchWholeWords": None,
        "useGroupScoring": None,
        "automationId": "",
    }


def book_filename(title: str, slug: str) -> str:
    """A lorebook filename a human can pick out of ST's world list.

    ST shows the filename as the world's name, so slugifying a full title-plus-subtitle
    produces something like `the-scumbag-s-guide-to-heroism-book-01-the-scumbag-system-traini`
    — truncated mid-word and unreadable in a dropdown. Cut at the subtitle, then at a word
    boundary.
    """
    text = (title or slug or "lorebook").strip()
    # Drop everything after the first strong separator: " - ", " — ", ": ".
    text = re.split(r"\s+[-—:]\s+", text)[0].strip() or text
    slugged = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(slugged) > 48:
        cut = slugged[:48].rsplit("-", 1)[0]
        slugged = cut or slugged[:48]
    return slugged or (slug or "lorebook")


def rule_to_entry(rule: dict[str, Any], uid: int) -> dict[str, Any]:
    """Turn an extracted progression rule into a `system` entry.

    The design lists "magic/tech systems" as a lorebook kind, and a LitRPG's progression
    rules are exactly that — so the L2 rules are lorebook material, not only engine
    input. The formula is appended when present because a rule with its arithmetic is far
    more useful in play than one without.
    """
    content = rule.get("statement", "")
    formula = (rule.get("formula") or "").strip()
    if formula:
        content = f"{content}\nFormula: {formula}"
    # Triggers: the mechanic's name, any aliases the extraction captured, plus a
    # spaced-out variant so "TemptationGauge" and "Temptation Gauge" both fire.
    name = rule.get("name", "")
    aliases = list(rule.get("aliases") or [])
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    if spaced != name:
        aliases.append(spaced)
    return build_entry(
        uid=uid,
        kind="system",
        name=name,
        content=content,
        aliases=aliases,
        # Format matters: `summarise` groups on the text before the colon, so this must
        # start with the entry kind, not with prose.
        comment=f"system: {name} ({rule.get('kind', 'mechanic')}, {rule.get('confidence', '')})",
    )


def entity_to_entry(entity: dict[str, Any], uid: int) -> dict[str, Any]:
    return build_entry(
        uid=uid,
        kind=entity.get("kind", "term"),
        name=entity.get("name", ""),
        content=entity.get("summary", ""),
        aliases=entity.get("aliases") or [],
        comment=f"{entity.get('kind', 'term')}: {entity.get('name', '')}",
    )


def quest_to_entry(quest: dict[str, Any], uid: int) -> dict[str, Any]:
    """A quest as a lorebook entry, with its OWN reward and penalty attached.

    Keeping the terms on the quest — rather than promoting them to system rules — is the
    fix for the real error this uncovered: one quest's failure penalty had been recorded
    as a law governing every quest.
    """
    parts = [quest.get("objective", "")]
    for label, field in (("Given by", "giver"), ("Requires", "requirements"),
                         ("Reward", "reward"), ("Penalty on failure", "penalty"),
                         ("Deadline", "deadline")):
        value = (quest.get(field) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    outcome = quest.get("outcome", "unknown")
    if outcome != "unknown":
        parts.append(f"Outcome: {outcome}")
    return build_entry(
        uid=uid,
        kind="quest",
        name=quest.get("name", ""),
        content="\n".join(p for p in parts if p),
        aliases=quest.get("aliases") or [],
        comment=f"quest: {quest.get('name', '')} ({quest.get('kind', 'unknown')})",
    )


def build_world(entities: list[dict[str, Any]],
                rules: list[dict[str, Any]] | None = None,
                quests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assemble the `worlds/<Book>.json` document.

    Entries are keyed by uid **as a string** — ST's own format, verified against a real
    lorebook file. Emitting a list here would import as an empty book with no error.
    """
    entries: dict[str, Any] = {}
    uid = 0
    for entity in entities:
        entry = entity_to_entry(entity, uid)
        if not entry["key"]:
            continue          # an entry with no trigger is dead weight in ST
        entries[str(uid)] = entry
        uid += 1
    for quest in quests or []:
        entry = quest_to_entry(quest, uid)
        if not entry["key"]:
            continue
        entries[str(uid)] = entry
        uid += 1
    for rule in rules or []:
        entry = rule_to_entry(rule, uid)
        if not entry["key"]:
            continue
        entries[str(uid)] = entry
        uid += 1
    return {"entries": entries}


def summarise(world: dict[str, Any]) -> dict[str, Any]:
    """Counts for the UI and the report — including the key statistics, since key
    coverage is what decides whether the lorebook fires at all."""
    entries = list(world.get("entries", {}).values())
    by_kind: dict[str, int] = {}
    for e in entries:
        kind = (e.get("comment", "") or "").split(":")[0].strip() or "?"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    key_counts = [len(e.get("key", [])) for e in entries]
    return {
        "entries": len(entries),
        "by_kind": by_kind,
        "keys_total": sum(key_counts),
        "keys_mean": round(sum(key_counts) / len(key_counts), 2) if key_counts else 0,
        "entries_with_one_key": sum(1 for n in key_counts if n == 1),
    }
