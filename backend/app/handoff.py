"""The Lore Forge → Persona Forge handoff contract.

**This file is mirrored verbatim in both repos** at `backend/app/handoff.py`. It is
deliberately pure stdlib and imports nothing from either application, so the two copies
can be diffed byte for byte. `CONTRACT_VERSION` is what makes the mirror safe: a consumer
checks the major digit and refuses a dossier it was not built to read, instead of
silently mis-parsing one.

Why a contract at all, and why not merge the two apps:

Lore Forge owns the left of the pipeline (corpus → cast → dossier) and Persona Forge owns
the right (prompt → dataset → LoRA → poses → sprites). The seam used to be a *convention*
— LF wrote `campaign/dossiers/<name>.json` to a shared mount and PF read it — which works
right up until the two disagree about a field, at which point nothing tells you. Making
the seam an object with a version means an agent (or a human) can carry a dossier across
without either service knowing the other exists, and without a runtime dependency between
them.

The dossier itself is built by `sheets.build_dossier()` in Lore Forge. This module only
stamps it, validates it, and derives the three things Persona Forge needs from it.

**The canon cursor travels with the object.** `as_of_chapter` is carried, checked and
re-stated in everything derived here. A dossier that does not say what it knows is one you
cannot safely hand to a reader who is twenty chapters in — so `validate()` treats a
missing `as_of_chapter` *key* as an error, while a null value (meaning "the whole book")
is fine.
"""

from __future__ import annotations

import re
from typing import Any

CONTRACT_VERSION = "1.0"
KIND = "character-dossier"


# --------------------------------------------------------------------------- #
# tier → how much of Persona Forge this character earns
# --------------------------------------------------------------------------- #

# Lore Forge computes the tier from measured presence (mentions, chapter spread, dialogue)
# and holds no expression logic of its own; this table is where the tier turns into work.
# It is the reason a cast build is affordable: authoring the full expression set for all 13
# characters in Book 02 is several hundred renders, and most of that cast is on screen twice.
#
# **The contract names counts, not labels.** Persona Forge owns the expression vocabulary —
# it lives in an editable emotion map, and a tier's labels are resolved from that map at
# use time. A list of label strings frozen into this file would be a second, silently
# diverging copy of a table the user edits in the UI.
#
# `expressions: None` means "everything the map offers"; a number means the first N in
# axis/tier order. `pose_preset` is a real Persona Forge preset name or None when no
# shipped preset matches the tier — None means the caller adds the labels explicitly, not
# that the tier gets nothing.
TIER_PLAN: dict[str, dict[str, Any]] = {
    "primary":   {"pose_preset": "expressions", "expressions": None, "train_lora": True},
    "secondary": {"pose_preset": None,          "expressions": 8,    "train_lora": True},
    "filler":    {"pose_preset": None,          "expressions": 1,    "train_lora": False},
}
DEFAULT_TIER = "filler"

# Not a preset choice — **every character must have this one**, because it is
# SillyTavern's fallback when a requested expression sprite is missing. A filler character
# is one sprite, never zero, and that one sprite is this.
FALLBACK_EXPRESSION = "neutral"


def plan_for(tier: str) -> dict[str, Any]:
    """What a character of this tier earns downstream. Unknown tiers fall to filler
    rather than raising — an unrecognised tier should cost one sprite, not the build."""
    plan = dict(TIER_PLAN.get(tier or "", TIER_PLAN[DEFAULT_TIER]))
    plan["fallback_expression"] = FALLBACK_EXPRESSION
    return plan


# --------------------------------------------------------------------------- #
# stamping + validation
# --------------------------------------------------------------------------- #

def stamp(dossier: dict[str, Any]) -> dict[str, Any]:
    """Add the contract identity to a dossier built by `sheets.build_dossier()`."""
    return {"contract_version": CONTRACT_VERSION, "kind": KIND, **dossier}


def major(version: str) -> str:
    return (version or "").split(".", 1)[0]


def validate(dossier: Any) -> list[str]:
    """Problems with a dossier, most disqualifying first. Empty list = usable.

    Returns problems rather than raising so a cast-wide import can report "9 of 13 usable,
    here is why the other 4 are not" instead of dying on the first bad row.
    """
    problems: list[str] = []
    if not isinstance(dossier, dict):
        return ["not an object"]

    got = str(dossier.get("contract_version") or "")
    if not got:
        problems.append("no contract_version — this is not a versioned dossier")
    elif major(got) != major(CONTRACT_VERSION):
        problems.append(
            f"contract_version {got} is not readable by this build (expects "
            f"{major(CONTRACT_VERSION)}.x)")
    if dossier.get("kind") not in (None, KIND):
        problems.append(f"kind is {dossier.get('kind')!r}, expected {KIND!r}")

    if not str(dossier.get("name") or "").strip():
        problems.append("no name")
    if "as_of_chapter" not in dossier:
        problems.append("no as_of_chapter key — cannot tell what this dossier knows")

    fields = dossier.get("fields")
    if not isinstance(fields, dict) or not fields:
        problems.append("no fields — the character sheet was never written "
                        "(run the sheets pass, or the character is filler tier)")
    return problems


# --------------------------------------------------------------------------- #
# derivation — what Persona Forge actually consumes
# --------------------------------------------------------------------------- #

# Proven on this pipeline: an expression baked into the CHARACTER prompt leaks into every
# other expression — a smile in the identity gives you a crying-and-smiling `grief` sprite.
# The looks prompt is assembled from observed appearance facts, so it *will* pick up "she
# smiles constantly" unless something drops it. This is that something, and it is
# mechanical: a fact mentioning an expression is skipped whole, never reworded. Rewriting
# the user's prose is what dropped detail and broke garments the last time.
_EXPRESSION_WORDS = re.compile(
    r"\b(smil\w*|grin\w*|frown\w*|scowl\w*|glar\w*|smirk\w*|laugh\w*|cry\w*|cried|weep\w*|"
    r"wept|sneer\w*|pout\w*|blush\w*|wink\w*|gasp\w*|snarl\w*|beam\w*|"
    r"expression|angry|sad|happy|furious|delighted)\b", re.I)

_SENTENCE_END = re.compile(r"[.!?]$")


def looks_prompt(dossier: dict[str, Any], max_facts: int = 12) -> str:
    """The appearance facts as prose, ready to seed a Persona Forge character prompt.

    Mechanical assembly only — no model, no tag rewriting. Lore Forge observed these
    sentences in the book and Persona Forge renders prose, so the honest transform is to
    join them and stop. Facts stay in chapter order (the earliest is the most-established
    description), and each was already spoiler-filtered by `as_of_chapter` upstream.
    """
    rows = (dossier.get("fields") or {}).get("appearance") or []
    out: list[str] = []
    for row in rows:
        text = str((row or {}).get("text") or "").strip()
        if not text or _EXPRESSION_WORDS.search(text):
            continue
        out.append(text if _SENTENCE_END.search(text) else text + ".")
        if len(out) >= max_facts:
            break
    return " ".join(out)


def dropped_expression_facts(dossier: dict[str, Any]) -> list[str]:
    """Appearance facts `looks_prompt()` refused, so the caller can say so rather than
    leave the user wondering where a description went."""
    rows = (dossier.get("fields") or {}).get("appearance") or []
    return [str(r.get("text") or "") for r in rows
            if r and _EXPRESSION_WORDS.search(str(r.get("text") or ""))]


def _first(dossier: dict[str, Any], field: str) -> str:
    rows = (dossier.get("fields") or {}).get(field) or []
    return str((rows[0] or {}).get("text") or "").strip() if rows else ""


def persona_seed(dossier: dict[str, Any]) -> dict[str, Any]:
    """Everything Persona Forge needs to open a project for this character.

    `character` is the looks prompt and nothing else. Role, motivation, speech and quirks
    are carried in `sheet_summary` for the character *card*, not folded into the image
    prompt — a diffusion model cannot render a motivation, and putting one in the prompt
    just spends tokens that the appearance needed.
    """
    problems = validate(dossier)
    if problems:
        raise ValueError("; ".join(problems))

    tier = str(dossier.get("tier") or DEFAULT_TIER)
    book = dossier.get("book") or {}
    return {
        "contract_version": CONTRACT_VERSION,
        "name": str(dossier.get("name") or "").strip(),
        "aliases": list(dossier.get("aliases") or []),
        "character": looks_prompt(dossier),
        "dropped_expression_facts": dropped_expression_facts(dossier),
        "tier": tier,
        "plan": plan_for(tier),
        # Restated, not inherited by accident: everything derived here is only true
        # as far as this chapter.
        "as_of_chapter": dossier.get("as_of_chapter"),
        "withheld_facts": dossier.get("withheld_facts", 0),
        "source": {
            "app": dossier.get("written_by") or "lore-forge",
            "book_title": book.get("title", ""),
            "book_slug": book.get("slug", ""),
            "written_at": dossier.get("written_at", ""),
        },
        "sheet_summary": {
            field: _first(dossier, field)
            for field in ("role", "personality", "motivation", "speech", "quirks")
            if _first(dossier, field)
        },
    }
