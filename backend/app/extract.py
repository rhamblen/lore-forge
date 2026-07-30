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
    "skill",      # acquisition, evolution, ranking of skills
    "class",      # class/job acquisition and evolution
    "currency",   # points, credits, any spendable resource
    "cap",        # hard limits and ceilings
    "penalty",    # costs, debuffs, death penalties
    "mechanic",   # anything else the system enforces
)

CONFIDENCE = ("stated", "implied")

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
  {"kind": "<one of: xp|level|skill|class|currency|cap|penalty|mechanic>",
   "name": "<short label, under 60 characters>",
   "statement": "<the rule IN YOUR OWN WORDS, under 300 characters>",
   "formula": "<a formula if one is given, else empty string>",
   "confidence": "<stated if the passage says it outright, implied if inferred>",
   "evidence_excerpt": "<at most 20 words from the passage that show the rule>"}
]}

Hard requirements:
- If the passage states no progression rules, return {"rules": []}. This is common and \
correct. Do not invent a rule to fill the space.
- "statement" must be YOUR OWN paraphrase. Do not copy sentences from the passage.
- Never guess numbers. If a threshold or cost is not given, leave it out of the statement \
rather than estimating it.
- One rule per distinct mechanic. Do not restate the same mechanic twice.
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
            merged[key] = dict(rule)
            continue

        seen = {(c.get("chunk_id"), c.get("chapter")) for c in existing["citations"]}
        for c in rule["citations"]:
            if (c.get("chunk_id"), c.get("chapter")) not in seen:
                existing["citations"].append(c)

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


def build_document(book: dict[str, Any], rules: list[dict[str, Any]],
                   model: str, stats: dict[str, Any]) -> dict[str, Any]:
    """The `campaign/rules/system.json` artefact."""
    by_kind: dict[str, int] = {}
    for r in rules:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return {
        "written_by": "lore-forge",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {"title": book.get("title", ""), "slug": book.get("slug", "")},
        "model": model,
        "extraction": stats,
        "counts": {"rules": len(rules), "by_kind": by_kind,
                   "stated": sum(1 for r in rules if r["confidence"] == "stated"),
                   "implied": sum(1 for r in rules if r["confidence"] == "implied")},
        "rules": rules,
    }
