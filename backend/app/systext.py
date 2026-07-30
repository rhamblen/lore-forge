"""Finding the system text — a lexical prefilter, no model involved.

LitRPG states its own mechanics in literal system boxes: level-ups, stat blocks, quest
awards, skill unlocks. Those passages look nothing like the surrounding prose, and the
difference is visible to a regex. So before spending a 12B model on 231 chunks, score
them cheaply and send it only the ones that plausibly contain rules.

This is the standing principle applied to cost: **the engine decides where to look; the
model only reads.** It is also, conveniently, the part that needs no GPU to develop.

The scorer is deliberately *recall-biased*. A false positive costs one wasted model call.
A false negative silently loses a rule from `rules/system.json` and nothing downstream
can recover it. When in doubt, include the chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Vocabulary that is near-diagnostic of a progression system when it appears as a label.
# Matched case-insensitively as whole words.
_TERMS = (
    "level", "lvl", "xp", "exp", "experience", "quest", "skill", "ability",
    "class", "stat", "stats", "attribute", "rank", "tier", "title", "trait",
    "achievement", "cooldown", "mana", "stamina", "hp", "mp", "buff", "debuff",
    "perk", "unlock", "unlocked", "evolve", "evolution", "upgrade", "points",
    "threshold", "requirement", "penalty", "bonus", "reward", "tutorial",
    "system", "interface", "notification", "status",
)
_TERM_RE = re.compile(r"\b(" + "|".join(_TERMS) + r")\b", re.IGNORECASE)

# Bracketed callouts: [Level Up], 【Skill Acquired】, «Quest Complete», {System}
_BRACKETED = re.compile(r"[\[【«{][^\]】»}\n]{2,60}[\]】»}]")

# "Strength: 14", "Level: 3" — a label/value pair on its own short line
_LABEL_VALUE = re.compile(r"^\s*[A-Z][A-Za-z ]{1,24}\s*[:：]\s*[-+]?\d", re.MULTILINE)

# "+50 XP", "-10 Stamina", "500 Points", "x2", "Lv. 12", "Level 3"
_DELTA = re.compile(r"([-+]\s?\d[\d,]*|\b\d[\d,]*\s*(?:points?|xp|exp)\b|\bx\s?\d+\b"
                    r"|\blv\.?\s*\d+\b|\blevel\s+\d+\b)", re.IGNORECASE)

# A short line in shouty caps — how many books render a system announcement
_SHOUTY = re.compile(r"^\s*[A-Z][A-Z0-9 '!:,\-]{4,48}\s*$", re.MULTILINE)

# Conditional/rule phrasing: "you must", "cannot exceed", "requires", "once per"
_RULE_PHRASE = re.compile(
    r"\b(must|cannot|can not|may not|requires?|required|only if|once per|per day|"
    r"maximum of|max(?:imum)?|minimum of|limited to|no more than|is capped|"
    r"in order to|upon reaching|when you reach|each time you)\b",
    re.IGNORECASE,
)


@dataclass
class Signal:
    score: float
    reasons: list[str]

    @property
    def selected(self) -> bool:
        return self.score >= THRESHOLD


# Tuned to be generous: two weak signals are enough to look. See the recall bias note.
THRESHOLD = 2.0


def score(text: str) -> Signal:
    """Score a chunk for 'probably contains stated game mechanics'."""
    reasons: list[str] = []
    total = 0.0
    if not text or not text.strip():
        return Signal(0.0, reasons)

    n_terms = len(set(m.group(1).lower() for m in _TERM_RE.finditer(text)))
    if n_terms:
        # Distinct terms, not raw hits: one paragraph saying "level" ten times is weaker
        # evidence than one saying "level", "skill" and "threshold" once each.
        pts = min(n_terms * 0.5, 3.0)
        total += pts
        reasons.append(f"{n_terms} system term(s) (+{pts:.1f})")

    n_brackets = len(_BRACKETED.findall(text))
    if n_brackets:
        pts = min(n_brackets * 1.5, 3.0)
        total += pts
        reasons.append(f"{n_brackets} bracketed callout(s) (+{pts:.1f})")

    n_lv = len(_LABEL_VALUE.findall(text))
    if n_lv:
        pts = min(n_lv * 1.0, 2.5)
        total += pts
        reasons.append(f"{n_lv} label:value line(s) (+{pts:.1f})")

    n_delta = len(_DELTA.findall(text))
    if n_delta:
        pts = min(n_delta * 0.5, 2.0)
        total += pts
        reasons.append(f"{n_delta} numeric award/level token(s) (+{pts:.1f})")

    n_shout = len(_SHOUTY.findall(text))
    if n_shout:
        pts = min(n_shout * 1.0, 2.0)
        total += pts
        reasons.append(f"{n_shout} shouted line(s) (+{pts:.1f})")

    n_rule = len(_RULE_PHRASE.findall(text))
    if n_rule:
        pts = min(n_rule * 0.5, 2.0)
        total += pts
        reasons.append(f"{n_rule} rule phrase(s) (+{pts:.1f})")

    return Signal(round(total, 2), reasons)


def select(chunks: list[dict], limit: int | None = None) -> list[dict]:
    """Return the chunks worth sending to the model, best first.

    Each returned chunk gains `_score` and `_reasons` so the UI can show *why* a passage
    was chosen — a prefilter you cannot inspect is a prefilter you cannot trust when a
    rule turns up missing.
    """
    scored = []
    for c in chunks:
        sig = score(c.get("text", ""))
        if sig.selected:
            item = dict(c)
            item["_score"] = sig.score
            item["_reasons"] = sig.reasons
            scored.append(item)
    scored.sort(key=lambda c: (-c["_score"], c.get("position", 0)))
    return scored[:limit] if limit else scored


def summarise(chunks: list[dict]) -> dict:
    """Prefilter statistics, for the extraction report."""
    sigs = [score(c.get("text", "")) for c in chunks]
    hits = [s for s in sigs if s.selected]
    return {
        "chunks_total": len(chunks),
        "chunks_selected": len(hits),
        "selection_rate": round(len(hits) / len(chunks), 3) if chunks else 0.0,
        "threshold": THRESHOLD,
        "score_max": round(max((s.score for s in sigs), default=0.0), 2),
        "score_mean_selected": round(sum(s.score for s in hits) / len(hits), 2) if hits else 0.0,
    }
