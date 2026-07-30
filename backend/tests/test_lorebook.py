"""Tests for quests (L2/L5) and the SillyTavern lorebook (L3).

The recurring theme: a lorebook entry fires only on its keys, so anything that loses an
alias is a silent failure. Most of these guard exactly that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import extract, lorebook  # noqa: E402

CHUNK = {"id": 1, "chapter_position": 2, "chapter_title": "Chapter 2", "source_ref": "pages 8-12"}
LATER = {"id": 9, "chapter_position": 9, "chapter_title": "Chapter 9", "source_ref": "pages 50-56"}


class TestQuestNormalise:
    def test_minimal_quest(self):
        q = extract.normalise_quest(
            {"name": "The Long Road", "objective": "Reach the citadel."}, CHUNK)
        assert q["name"] == "The Long Road"
        assert q["kind"] == "unknown" and q["outcome"] == "unknown"
        assert q["first_chapter"] == 2

    def test_missing_objective_rejected(self):
        assert extract.normalise_quest({"name": "X"}, CHUNK) is None

    def test_unknown_kind_and_outcome_coerced(self):
        q = extract.normalise_quest(
            {"name": "X", "objective": "o", "kind": "epic", "outcome": "in progress"}, CHUNK)
        assert q["kind"] == "unknown" and q["outcome"] == "unknown"


class TestQuestMerge:
    def _q(self, chunk=CHUNK, **over):
        base = {"name": "The Long Road", "objective": "Reach the citadel."}
        base.update(over)
        return extract.normalise_quest(base, chunk)

    def test_fields_fill_in_across_chapters(self):
        """Chapter 2 names the reward, chapter 9 names the penalty — keep both."""
        a = self._q(reward="A blade.")
        b = self._q(chunk=LATER, penalty="Lose a hand.")
        merged = extract.merge_quests([a, b])
        assert len(merged) == 1
        assert merged[0]["reward"] == "A blade."
        assert merged[0]["penalty"] == "Lose a hand."

    def test_first_chapter_is_the_earliest_sighting(self):
        merged = extract.merge_quests([self._q(chunk=LATER), self._q()])
        assert merged[0]["first_chapter"] == 2

    def test_journey_is_ordered_by_first_appearance(self):
        early = extract.normalise_quest({"name": "Early", "objective": "o"}, CHUNK)
        late = extract.normalise_quest({"name": "Late", "objective": "o"}, LATER)
        merged = extract.merge_quests([late, early])
        assert [q["name"] for q in merged] == ["Early", "Late"]

    def test_resolved_outcome_is_not_dragged_back(self):
        done = self._q(outcome="completed")
        mention = self._q(chunk=LATER, outcome="ongoing")
        assert extract.merge_quests([done, mention])[0]["outcome"] == "completed"

    def test_aliases_union(self):
        a = self._q(aliases=["Long Road"])
        b = self._q(chunk=LATER, aliases=["the Road"])
        merged = extract.merge_quests([a, b])
        assert set(merged[0]["aliases"]) == {"Long Road", "the Road"}


class TestRuleScope:
    """The correction that prompted scope: one quest's penalty is not a system law."""

    def test_applies_to_forces_instance_scope(self):
        r = extract.normalise_rule(
            {"kind": "penalty", "name": "Quest failure", "statement": "s",
             "scope": "system", "applies_to": "The Long Road"}, CHUNK)
        assert r["scope"] == "instance"

    def test_default_scope_is_system(self):
        r = extract.normalise_rule({"kind": "xp", "name": "n", "statement": "s"}, CHUNK)
        assert r["scope"] == "system"

    def test_document_splits_system_from_instance(self):
        rules = [
            extract.normalise_rule({"kind": "xp", "name": "XP", "statement": "s"}, CHUNK),
            extract.normalise_rule({"kind": "penalty", "name": "Road penalty",
                                    "statement": "s", "applies_to": "The Long Road"}, CHUNK),
        ]
        doc = extract.build_document({"title": "T", "slug": "t"}, rules, "m", {})
        assert doc["counts"]["rules"] == 1
        assert doc["counts"]["instance_rules"] == 1
        assert doc["rules"][0]["name"] == "XP"
        assert doc["instance_rules"][0]["name"] == "Road penalty"


class TestLorebook:
    def test_entries_are_keyed_by_uid_string(self):
        """ST reads a MAP keyed by uid. A list imports as an empty book, silently."""
        world = lorebook.build_world([
            {"kind": "location", "name": "Drowned Quarter", "summary": "s", "aliases": []}])
        assert list(world["entries"]) == ["0"]
        assert world["entries"]["0"]["uid"] == 0

    def test_name_and_aliases_all_become_keys(self):
        world = lorebook.build_world([
            {"kind": "faction", "name": "Ashen Court", "summary": "s",
             "aliases": ["the Court", "Ashenites"]}])
        assert world["entries"]["0"]["key"] == ["Ashen Court", "the Court", "Ashenites"]

    def test_duplicate_alias_is_dropped_case_insensitively(self):
        keys = lorebook.build_keys("The Court", ["the court", "Ashenites"])
        assert keys == ["The Court", "Ashenites"]

    def test_entry_with_no_key_is_omitted(self):
        world = lorebook.build_world([{"kind": "term", "name": "", "summary": "s"}])
        assert world["entries"] == {}

    def test_systems_outrank_terminology(self):
        world = lorebook.build_world(
            [{"kind": "term", "name": "Tide", "summary": "s", "aliases": []}],
            rules=[{"kind": "xp", "name": "XP", "statement": "s", "formula": "",
                    "confidence": "stated", "aliases": []}])
        by_comment = {e["comment"].split(":")[0]: e["order"] for e in world["entries"].values()}
        assert by_comment["system"] > by_comment["term"]

    def test_quest_entry_carries_its_own_terms(self):
        world = lorebook.build_world([], quests=[{
            "name": "The Long Road", "kind": "main", "objective": "Reach the citadel.",
            "reward": "A blade.", "penalty": "Lose a hand.", "giver": "", "requirements": "",
            "deadline": "", "outcome": "ongoing", "aliases": []}])
        content = world["entries"]["0"]["content"]
        assert "Reward: A blade." in content
        assert "Penalty on failure: Lose a hand." in content

    def test_summarise_groups_by_kind_from_the_comment_prefix(self):
        world = lorebook.build_world(
            [{"kind": "location", "name": "A", "summary": "s", "aliases": []}],
            rules=[{"kind": "xp", "name": "B", "statement": "s", "formula": "",
                    "confidence": "stated", "aliases": []}])
        s = lorebook.summarise(world)
        assert s["by_kind"] == {"location": 1, "system": 1}

    def test_single_key_entries_are_counted(self):
        world = lorebook.build_world([
            {"kind": "term", "name": "Solo", "summary": "s", "aliases": []},
            {"kind": "term", "name": "Multi", "summary": "s", "aliases": ["M"]}])
        assert lorebook.summarise(world)["entries_with_one_key"] == 1

    def test_content_is_capped(self):
        world = lorebook.build_world([
            {"kind": "term", "name": "X", "summary": "y" * 5000, "aliases": []}])
        assert len(world["entries"]["0"]["content"]) <= lorebook.MAX_CONTENT


class TestFilename:
    def test_subtitle_is_dropped(self):
        assert lorebook.book_filename(
            "The Scumbag's Guide To Heroism - Book 01 - The Scumbag System", "slug") \
            == "the-scumbag-s-guide-to-heroism"

    def test_long_title_cuts_on_a_word_boundary(self):
        name = lorebook.book_filename("a " * 60, "slug")
        assert len(name) <= 48 and not name.endswith("-")

    def test_falls_back_to_slug(self):
        assert lorebook.book_filename("", "my-book") == "my-book"
