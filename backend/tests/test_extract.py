"""Tests for L2 extraction: normalisation, merging, and the prefilter.

All offline — a stub stands in for the model, so the pipeline is provable with no GPU
and no Ollama.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import extract, systext  # noqa: E402

CHUNK = {
    "id": 7,
    "position": 6,
    "chapter_position": 2,
    "chapter_title": "Chapter 2 | Thresholds",
    "source_ref": "pages 8-12",
    "char_start": 0,
    "char_end": 900,
    "text": "irrelevant for these tests",
}


def _other_chunk(**over):
    c = dict(CHUNK, id=99, chapter_position=9, source_ref="pages 50-56")
    c.update(over)
    return c


class TestNormalise:
    def test_minimal_valid_rule(self):
        r = extract.normalise_rule(
            {"kind": "xp", "name": "XP on kill", "statement": "Defeating a foe grants XP.",
             "confidence": "stated"}, CHUNK)
        assert r["kind"] == "xp"
        assert r["citations"][0]["chapter"] == 2
        assert r["citations"][0]["source_ref"] == "pages 8-12"

    def test_unknown_kind_becomes_mechanic_not_dropped(self):
        r = extract.normalise_rule(
            {"kind": "leveling-up", "name": "n", "statement": "s"}, CHUNK)
        assert r["kind"] == "mechanic"

    def test_unmarked_confidence_defaults_to_implied(self):
        r = extract.normalise_rule({"kind": "xp", "name": "n", "statement": "s"}, CHUNK)
        assert r["confidence"] == "implied"

    def test_missing_name_is_rejected(self):
        assert extract.normalise_rule({"kind": "xp", "statement": "s"}, CHUNK) is None

    def test_missing_statement_is_rejected(self):
        assert extract.normalise_rule({"kind": "xp", "name": "n"}, CHUNK) is None

    def test_non_dict_is_rejected(self):
        assert extract.normalise_rule("a rule", CHUNK) is None
        assert extract.normalise_rule(None, CHUNK) is None

    def test_evidence_is_hard_capped(self):
        r = extract.normalise_rule(
            {"kind": "xp", "name": "n", "statement": "s", "evidence_excerpt": "w " * 400},
            CHUNK)
        assert len(r["evidence_excerpt"]) <= extract.MAX_EVIDENCE

    def test_statement_is_hard_capped(self):
        r = extract.normalise_rule(
            {"kind": "xp", "name": "n", "statement": "x" * 999}, CHUNK)
        assert len(r["statement"]) <= extract.MAX_STATEMENT

    def test_whitespace_is_collapsed(self):
        r = extract.normalise_rule(
            {"kind": "xp", "name": "  XP\n  on   kill ", "statement": "a\n\nb"}, CHUNK)
        assert r["name"] == "XP on kill"
        assert r["statement"] == "a b"

    def test_id_is_stable_across_cosmetic_variation(self):
        a = extract.normalise_rule({"kind": "xp", "name": "XP On Kill", "statement": "s"}, CHUNK)
        b = extract.normalise_rule({"kind": "xp", "name": "xp-on-kill", "statement": "s"}, CHUNK)
        assert a["id"] == b["id"]


class TestMerge:
    def _rule(self, name="XP on kill", kind="xp", conf="stated", stmt="Kills grant XP.",
              chunk=None):
        return extract.normalise_rule(
            {"kind": kind, "name": name, "statement": stmt, "confidence": conf},
            chunk or CHUNK)

    def test_duplicates_collapse_and_union_citations(self):
        merged = extract.merge_rules([self._rule(), self._rule(chunk=_other_chunk())])
        assert len(merged) == 1
        assert len(merged[0]["citations"]) == 2

    def test_same_chunk_twice_does_not_double_cite(self):
        merged = extract.merge_rules([self._rule(), self._rule()])
        assert len(merged[0]["citations"]) == 1

    def test_case_variation_is_the_same_rule(self):
        merged = extract.merge_rules([self._rule(name="XP on kill"),
                                      self._rule(name="xp ON kill", chunk=_other_chunk())])
        assert len(merged) == 1

    def test_different_kinds_stay_separate(self):
        merged = extract.merge_rules([self._rule(kind="xp"), self._rule(kind="cap")])
        assert len(merged) == 2

    def test_stated_beats_implied(self):
        implied = self._rule(conf="implied", stmt="Maybe kills grant XP.")
        stated = self._rule(conf="stated", stmt="Kills grant XP.", chunk=_other_chunk())
        merged = extract.merge_rules([implied, stated])
        assert merged[0]["confidence"] == "stated"
        assert merged[0]["statement"] == "Kills grant XP."

    def test_implied_does_not_overwrite_stated(self):
        stated = self._rule(conf="stated", stmt="Kills grant XP.")
        implied = self._rule(conf="implied", stmt="A much longer but weaker claim here.",
                             chunk=_other_chunk())
        merged = extract.merge_rules([stated, implied])
        assert merged[0]["confidence"] == "stated"
        assert merged[0]["statement"] == "Kills grant XP."

    def test_most_cited_rule_sorts_first(self):
        common = [self._rule(name="Common", chunk=CHUNK),
                  self._rule(name="Common", chunk=_other_chunk())]
        rare = [self._rule(name="Rare", kind="cap")]
        merged = extract.merge_rules(common + rare)
        assert merged[0]["name"] == "Common"

    def test_empty_input(self):
        assert extract.merge_rules([]) == []


class TestParseModelOutput:
    """The stub model. Each string is a real malformation shape."""

    def test_clean_output(self):
        text = json.dumps({"rules": [
            {"kind": "xp", "name": "XP on kill", "statement": "Kills grant XP.",
             "confidence": "stated"}]})
        rules, err = extract.parse_model_rules(text, CHUNK)
        assert err == "" and len(rules) == 1

    def test_fenced_with_preamble(self):
        text = ('Here are the rules I found:\n```json\n'
                '{"rules": [{"kind":"cap","name":"Level cap","statement":"Levels stop at 50.",'
                '"confidence":"stated"}]}\n```')
        rules, err = extract.parse_model_rules(text, CHUNK)
        assert err == "" and rules[0]["kind"] == "cap"

    def test_empty_rules_is_success_not_error(self):
        rules, err = extract.parse_model_rules('{"rules": []}', CHUNK)
        assert err == "" and rules == []

    def test_bare_list_accepted(self):
        text = '[{"kind":"xp","name":"n","statement":"s"}]'
        rules, err = extract.parse_model_rules(text, CHUNK)
        assert err == "" and len(rules) == 1

    def test_refusal_prose_is_an_error_not_a_crash(self):
        rules, err = extract.parse_model_rules(
            "This passage contains no progression rules.", CHUNK)
        assert rules == [] and "no JSON value" in err

    def test_truncation_is_reported(self):
        rules, err = extract.parse_model_rules('{"rules": [{"kind": "xp",', CHUNK)
        assert rules == [] and "truncated" in err

    def test_garbage_rows_are_skipped_not_fatal(self):
        text = json.dumps({"rules": [
            {"kind": "xp", "name": "Good", "statement": "Fine."},
            {"nonsense": True},
            "a string",
        ]})
        rules, err = extract.parse_model_rules(text, CHUNK)
        assert err == "" and len(rules) == 1 and rules[0]["name"] == "Good"


class TestPrefilter:
    def test_plain_prose_is_not_selected(self):
        text = ("She walked down the flooded stair, counting each step, and did not look "
                "back at the lamps burning behind her in the dark water.")
        assert not systext.score(text).selected

    def test_bracketed_system_box_is_selected(self):
        text = "[Level Up] You have reached Level 12. [Skill Acquired: Parry]"
        sig = systext.score(text)
        assert sig.selected and sig.reasons

    def test_stat_block_is_selected(self):
        text = "Strength: 14\nAgility: 9\nStamina: 22\n"
        assert systext.score(text).selected

    def test_rule_phrasing_with_terms_is_selected(self):
        text = ("A skill cannot exceed rank five, and each upgrade requires the "
                "expenditure of points earned at the previous level.")
        assert systext.score(text).selected

    def test_empty_text_scores_zero(self):
        assert systext.score("").score == 0.0

    def test_select_orders_by_score_and_annotates(self):
        chunks = [
            {"position": 0, "text": "just some quiet prose about the weather"},
            {"position": 1, "text": "[Quest Complete] +500 XP. Level 3 reached."},
        ]
        sel = systext.select(chunks)
        assert len(sel) == 1
        assert sel[0]["position"] == 1
        assert "_score" in sel[0] and sel[0]["_reasons"]

    def test_summarise_shape(self):
        s = systext.summarise([{"text": "[Level Up] Level 2"}, {"text": "quiet prose"}])
        assert s["chunks_total"] == 2 and s["chunks_selected"] == 1


class TestDocument:
    def test_counts_and_shape(self):
        rules = extract.merge_rules([
            extract.normalise_rule(
                {"kind": "xp", "name": "A", "statement": "s", "confidence": "stated"}, CHUNK),
            extract.normalise_rule(
                {"kind": "cap", "name": "B", "statement": "s", "confidence": "implied"}, CHUNK),
        ])
        doc = extract.build_document({"title": "T", "slug": "t"}, rules, "gemma3:12b",
                                     {"chunks_selected": 2})
        assert doc["counts"]["rules"] == 2
        assert doc["counts"]["stated"] == 1 and doc["counts"]["implied"] == 1
        assert doc["counts"]["by_kind"] == {"xp": 1, "cap": 1}
        assert doc["model"] == "gemma3:12b"
        json.dumps(doc)  # must be serialisable
