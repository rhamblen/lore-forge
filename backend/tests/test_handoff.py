"""The Lore Forge → Persona Forge contract.

`handoff.py` is mirrored verbatim into Persona Forge, so these tests are the only place
the contract's behaviour is pinned. The two rules worth a test each are the ones that
were expensive to learn: an expression word in the character prompt leaks a smile into
every other sprite, and a dossier that does not say which chapter it stops at cannot be
handed to a reader mid-series.
"""

from app import handoff, sheets

BOOK = {"title": "Test Book", "slug": "test-book"}
PERSON = {"name": "Sunny", "aliases": ["Sunless"], "tier": "secondary", "note": "",
          "mentions": 412, "dialogue_hits": 88, "chapter_count": 30,
          "first_chapter": 1, "last_chapter": 40}


def fact(field, text, chapter):
    return {"field": field, "text": text, "chapter": chapter, "subject": "",
            "citation": f"ch{chapter}", "status": "kept"}


FACTS = [
    fact("appearance", "Tall and lean, with black hair cut short", 2),
    fact("appearance", "Wears a patched grey travelling coat", 3),
    fact("appearance", "She smiles constantly, even when threatened", 4),
    fact("appearance", "A pale scar runs from jaw to collarbone", 31),
    fact("role", "A conscript in the Nightmare Spell", 2),
    fact("motivation", "Wants to survive long enough to find his sister", 5),
]


def dossier(chapter=None):
    return sheets.build_dossier(BOOK, PERSON, FACTS, "test-model", chapter)


# --------------------------------------------------------------------------- #
# stamping + validation
# --------------------------------------------------------------------------- #

def test_build_dossier_is_stamped():
    d = dossier()
    assert d["contract_version"] == handoff.CONTRACT_VERSION
    assert d["kind"] == handoff.KIND
    assert handoff.validate(d) == []


def test_a_future_major_is_refused_not_guessed_at():
    problems = handoff.validate(dict(dossier(), contract_version="9.0"))
    assert problems and "not readable" in problems[0]


def test_an_unstamped_object_is_refused():
    raw = dict(dossier())
    del raw["contract_version"]
    assert "no contract_version" in "; ".join(handoff.validate(raw))


def test_a_dossier_that_cannot_say_what_it_knows_is_refused():
    raw = dict(dossier(10))
    del raw["as_of_chapter"]
    assert any("as_of_chapter" in p for p in handoff.validate(raw))


def test_null_as_of_chapter_is_valid_and_means_the_whole_book():
    d = dossier(None)
    assert d["as_of_chapter"] is None
    assert handoff.validate(d) == []


def test_validate_reports_every_problem_rather_than_the_first():
    """A cast-wide import needs "9 of 13 usable, here is why", not a first-failure abort."""
    assert len(handoff.validate({"contract_version": "1.0"})) >= 3


# --------------------------------------------------------------------------- #
# the looks prompt
# --------------------------------------------------------------------------- #

def test_expression_facts_never_reach_the_character_prompt():
    """Proven on the pipeline: a smile baked into the identity renders `grief` as a
    character who is crying and smiling at once."""
    prompt = handoff.looks_prompt(dossier())
    assert "smiles" not in prompt
    assert "black hair" in prompt and "travelling coat" in prompt


def test_a_dropped_expression_fact_is_named_not_silently_lost():
    assert handoff.dropped_expression_facts(dossier()) == [
        "She smiles constantly, even when threatened"]


def test_the_prompt_is_prose_and_is_not_rewritten_into_tags():
    """Only mechanical edits are allowed — the source sentences survive verbatim."""
    prompt = handoff.looks_prompt(dossier())
    assert "Tall and lean, with black hair cut short." in prompt


def test_spoiler_cutoff_keeps_later_appearance_out_of_the_prompt():
    early = handoff.looks_prompt(dossier(10))
    whole = handoff.looks_prompt(dossier())
    assert "scar" not in early
    assert "scar" in whole
    assert dossier(10)["withheld_facts"] == 1


# --------------------------------------------------------------------------- #
# tier plan + seed
# --------------------------------------------------------------------------- #

def test_tier_decides_the_size_of_the_build():
    assert handoff.plan_for("primary")["expressions"] is None      # everything
    assert handoff.plan_for("secondary")["expressions"] == 8
    assert handoff.plan_for("filler")["expressions"] == 1
    assert handoff.plan_for("filler")["train_lora"] is False


def test_an_unknown_tier_costs_one_sprite_not_the_build():
    assert handoff.plan_for("wildly-unexpected") == handoff.plan_for("filler")


def test_every_tier_carries_the_fallback_expression():
    """A missing sprite falls back to `neutral` in SillyTavern, so no tier may omit it."""
    for tier in ("primary", "secondary", "filler"):
        assert handoff.plan_for(tier)["fallback_expression"] == "neutral"


def test_persona_seed_carries_the_canon_cursor_and_the_card_fields():
    seed = handoff.persona_seed(dossier(10))
    assert seed["as_of_chapter"] == 10
    assert seed["withheld_facts"] == 1
    assert seed["tier"] == "secondary"
    # Motivation belongs to the card, never to the image prompt.
    assert "sister" in seed["sheet_summary"]["motivation"]
    assert "sister" not in seed["character"]


def test_persona_seed_refuses_an_invalid_dossier():
    try:
        handoff.persona_seed({"name": "nobody"})
    except ValueError as exc:
        assert "contract_version" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unstamped dossier must not produce a seed")
