"""Tests for the character census: harvesting, tiering, pairing and merging."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import census  # noqa: E402


class TestHarvest:
    def _chapters(self, *texts):
        return [{"position": i, "text": t} for i, t in enumerate(texts, start=1)]

    def test_finds_a_speaker(self):
        out = census.harvest(self._chapters('"Go," Sera said. Sera said it again. Sera waited.'))
        names = {c["name"]: c for c in out}
        assert "Sera" in names
        assert names["Sera"]["dialogue_hits"] >= 2

    def test_contractions_are_not_names(self):
        out = census.harvest(self._chapters(
            "I'd gone. You're late. Don't. That's it. I'm here. I'll go. We've won."))
        assert not [c for c in out if "'" in c["name"] or "’" in c["name"]]

    def test_possessive_folds_into_the_base_name(self):
        out = census.harvest(self._chapters(
            "Sloane went. Sloane's ledger. Sloane's coat. Sloane again."))
        names = [c["name"] for c in out]
        assert "Sloane" in names and "Sloane's" not in names

    def test_leading_stopword_is_stripped(self):
        """'If Lukas had...' must not create a character called 'If Lukas' — and the
        mentions must land on Lukas instead of being stolen."""
        out = census.harvest(self._chapters(
            "If Lukas had gone. When Lukas arrived. But Lukas waited. Lukas stayed."))
        names = {c["name"]: c for c in out}
        assert "If Lukas" not in names
        assert names["Lukas"]["mentions"] == 4

    def test_titles_are_stripped(self):
        out = census.harvest(self._chapters(
            "Doctor Vance spoke. Vance nodded. Vance left."))
        assert "Vance" in {c["name"] for c in out}

    def test_rare_non_speaker_is_dropped(self):
        out = census.harvest(self._chapters("Corwin appeared once."))
        assert "Corwin" not in {c["name"] for c in out}

    def test_rare_speaker_is_kept(self):
        """One line of dialogue is enough — silence is the discriminator, not frequency."""
        out = census.harvest(self._chapters('"Hello," Corwin said.'))
        assert "Corwin" in {c["name"] for c in out}


class TestTiering:
    def _c(self, mentions=10, chapters=4, dialogue=0):
        return {"name": "X", "mentions": mentions, "chapter_count": chapters,
                "dialogue_hits": dialogue}

    def test_wide_spread_and_speaks_is_primary(self):
        tier, _ = census.assign_tier(self._c(200, 30, 20), 40)
        assert tier == "primary"

    def test_speaks_often_is_primary_even_when_narrow(self):
        tier, _ = census.assign_tier(self._c(60, 3, 20), 40)
        assert tier == "primary"

    def test_speaks_a_little_is_secondary(self):
        tier, _ = census.assign_tier(self._c(19, 2, 4), 40)
        assert tier == "secondary"

    def test_recurring_but_silent_is_secondary(self):
        tier, reason = census.assign_tier(self._c(96, 27, 0), 40)
        assert tier == "secondary" and "silent" in reason

    def test_rare_and_silent_is_filler(self):
        tier, _ = census.assign_tier(self._c(3, 1, 0), 40)
        assert tier == "filler"

    def test_reason_always_shows_the_arithmetic(self):
        _, reason = census.assign_tier(self._c(96, 27, 0), 40)
        assert "96 mentions" in reason and "27/40" in reason


class TestContainmentPairs:
    def test_single_token_inside_longer_name(self):
        pairs = census.containment_pairs(["Lukas", "Lukas Belmont"])
        assert ("Lukas", "Lukas Belmont") in pairs

    def test_multi_token_run_inside_longer_name(self):
        """The case that escaped the first version: two tokens inside three."""
        pairs = census.containment_pairs(["Diane Fitzgerald", "Subject Diane Fitzgerald"])
        assert ("Diane Fitzgerald", "Subject Diane Fitzgerald") in pairs

    def test_shared_surname_is_still_only_a_candidate(self):
        # Proposed for adjudication, never merged automatically.
        pairs = census.containment_pairs(["Diane Fitzgerald", "Sloane Fitzgerald"])
        assert pairs == []

    def test_unrelated_names_do_not_pair(self):
        assert census.containment_pairs(["Sera", "Orun Kell"]) == []

    def test_non_contiguous_tokens_do_not_pair(self):
        assert census.containment_pairs(["Diane Belmont", "Diane Marie Belmont"]) == []


class TestPreferredName:
    def test_fuller_name_wins(self):
        assert census.preferred_name(["Lukas", "Lukas Belmont"]) == "Lukas Belmont"

    def test_status_box_label_loses_despite_being_longer(self):
        assert census.preferred_name(
            ["Diane Fitzgerald", "Subject Diane Fitzgerald"]) == "Diane Fitzgerald"

    def test_all_labelled_falls_back_to_stripping_the_label(self):
        assert census.preferred_name(["Subject Diane"]) == "Diane"

    def test_empty(self):
        assert census.preferred_name([]) == ""


class TestFindMentions:
    def test_returns_chapter_attribution_and_the_matched_form(self):
        chapters = [{"position": 3, "title": "Chapter 3", "text": "Then Mom arrived early."}]
        out = census.find_mentions(chapters, ["Mom"])
        assert out and out[0]["chapter"] == 3 and out[0]["matched"] == "Mom"

    def test_matches_aliases_too(self):
        chapters = [{"position": 1, "title": "c", "text": "Diane spoke. Mom nodded."}]
        out = census.find_mentions(chapters, ["Diane Fitzgerald", "Diane", "Mom"])
        assert {m["matched"] for m in out} == {"Diane", "Mom"}

    def test_respects_the_limit(self):
        chapters = [{"position": 1, "title": "c", "text": "Mom " * 50}]
        assert len(census.find_mentions(chapters, ["Mom"], limit=4)) == 4

    def test_whole_words_only(self):
        chapters = [{"position": 1, "title": "c", "text": "Mommy went home."}]
        assert census.find_mentions(chapters, ["Mom"]) == []


class TestMergePersistence:
    """Merging touches the database, so these use a throwaway one."""

    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        from app import characters_store, db
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite3")
        db.init_db()
        with db.connect() as conn:
            conn.execute("INSERT INTO books (id, title, slug) VALUES (1, 'B', 'b')")
            conn.executemany(
                "INSERT INTO chapters (book_id, position, title, text) VALUES (1,?,?,'x')",
                [(i, f"c{i}") for i in range(1, 41)])
        return characters_store

    def _person(self, name, mentions, chapters, dialogue=0, aliases=None):
        return {"name": name, "aliases": aliases or [], "note": "",
                "mentions": mentions, "dialogue_hits": dialogue,
                "chapters": chapters, "chapter_count": len(chapters),
                "first_chapter": min(chapters), "last_chapter": max(chapters)}

    def test_merge_unions_chapters_rather_than_adding_counts(self, store):
        store.upsert(1, [self._person("Diane Fitzgerald", 150, list(range(1, 25)), 17),
                         self._person("Subject Diane Fitzgerald", 3, [24, 25])], 40)
        rows = {c["name"]: c for c in store.list_characters(1)}
        merged = store.merge(1, rows["Diane Fitzgerald"]["id"],
                             rows["Subject Diane Fitzgerald"]["id"])
        # 24 chapters ∪ {24, 25} = 25, NOT 24 + 2
        assert merged["chapter_count"] == 25
        assert merged["mentions"] == 153

    def test_absorbed_name_becomes_an_alias(self, store):
        store.upsert(1, [self._person("Diane Fitzgerald", 150, [1, 2], 17),
                         self._person("Mom", 9, [3])], 40)
        rows = {c["name"]: c for c in store.list_characters(1)}
        merged = store.merge(1, rows["Diane Fitzgerald"]["id"], rows["Mom"]["id"])
        assert "Mom" in merged["aliases"]
        assert len(store.list_characters(1)) == 1

    def test_label_prefixed_name_does_not_become_the_display_name(self, store):
        store.upsert(1, [self._person("Diane Fitzgerald", 150, [1], 17),
                         self._person("Subject Diane Fitzgerald", 3, [2])], 40)
        rows = {c["name"]: c for c in store.list_characters(1)}
        merged = store.merge(1, rows["Diane Fitzgerald"]["id"],
                             rows["Subject Diane Fitzgerald"]["id"])
        assert merged["name"] == "Diane Fitzgerald"

    def test_merge_survives_a_later_census(self, store):
        """The stickiness guarantee: a re-census must not resurrect the merged row."""
        store.upsert(1, [self._person("Diane Fitzgerald", 150, [1], 17),
                         self._person("Mom", 9, [3])], 40)
        rows = {c["name"]: c for c in store.list_characters(1)}
        store.merge(1, rows["Diane Fitzgerald"]["id"], rows["Mom"]["id"])

        # The census runs again and once more proposes "Mom" as her own person.
        store.upsert(1, [self._person("Mom", 9, [3])], 40)
        after = store.list_characters(1)
        assert len(after) == 1
        assert after[0]["name"] == "Diane Fitzgerald"
        assert "Mom" in after[0]["aliases"]

    def test_merge_rejects_self(self, store):
        store.upsert(1, [self._person("A", 5, [1])], 40)
        cid = store.list_characters(1)[0]["id"]
        assert store.merge(1, cid, cid) is None

    def test_locked_tier_is_not_recomputed_by_a_merge(self, store):
        store.upsert(1, [self._person("A", 5, [1]), self._person("B", 200, list(range(1, 35)), 30)], 40)
        rows = {c["name"]: c for c in store.list_characters(1)}
        store.set_tier(rows["A"]["id"], "filler")
        merged = store.merge(1, rows["A"]["id"], rows["B"]["id"])
        assert merged["tier"] == "filler"
