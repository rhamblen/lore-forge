"""Tests for L2 pass 2 — the character sheets.

Nearly all of these guard one property: **the chapter stamp comes from the engine, not
the model.** That is what lets a sheet be exported "as of chapter N", and it is the only
reason the facts are stored one claim at a time instead of as prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import sheets  # noqa: E402

CHUNK = {"id": 7, "position": 3, "chapter_position": 12, "chapter_title": "Chapter 12",
         "citation": "ch. 12 · chars 0–900", "text": "…"}
LATER = {"id": 9, "position": 5, "chapter_position": 30, "chapter_title": "Chapter 30",
         "citation": "ch. 30 · chars 0–900", "text": "…"}
ALL = sheets.FIELDS


class TestTierPolicy:
    def test_filler_never_gets_a_sheet(self):
        """A lorebook line is all a filler character earns — pass 2 skips them."""
        assert not sheets.wants_sheet({"tier": "filler"})
        assert sheets.fields_for("filler") == ()
        assert sheets.passages_for("filler") == 0

    def test_primary_earns_more_than_secondary(self):
        assert len(sheets.fields_for("primary")) > len(sheets.fields_for("secondary"))
        assert sheets.passages_for("primary") > sheets.passages_for("secondary")

    def test_every_tier_field_is_a_known_field(self):
        for tier_fields in sheets.TIER_FIELDS.values():
            assert set(tier_fields) <= set(sheets.FIELDS)


class TestSelectPassages:
    def _chunk(self, cid, chapter, text, position=0):
        return {"id": cid, "position": position, "chapter_position": chapter,
                "chapter_title": f"Chapter {chapter}", "text": text}

    def test_passages_without_the_character_are_not_read(self):
        chunks = [self._chunk(1, 1, "Sunny walked on."), self._chunk(2, 2, "Rain fell.")]
        picked = sheets.select_passages(chunks, ["Sunny"], 5)
        assert [c["id"] for c in picked] == [1]

    def test_aliases_count_as_the_character(self):
        chunks = [self._chunk(1, 1, "Mom said nothing.")]
        assert sheets.select_passages(chunks, ["Diane Fitzgerald", "Mom"], 5)

    def test_density_beats_raw_count(self):
        """A short passage that is ABOUT them beats a long one that name-drops them."""
        dense = self._chunk(1, 5, "Sunny. Sunny. Sunny.")
        sprawling = self._chunk(2, 6, ("filler words " * 400) + "Sunny Sunny Sunny Sunny")
        picked = sheets.select_passages([sprawling, dense], ["Sunny"], 1)
        assert picked[0]["id"] == 1

    def test_returned_in_reading_order(self):
        late = self._chunk(1, 30, "Sunny Sunny")
        early = self._chunk(2, 4, "Sunny Sunny")
        picked = sheets.select_passages([late, early], ["Sunny"], 5)
        assert [c["chapter_position"] for c in picked] == [4, 30]

    def test_limit_is_respected(self):
        chunks = [self._chunk(i, i, "Sunny") for i in range(1, 20)]
        assert len(sheets.select_passages(chunks, ["Sunny"], 4)) == 4

    def test_whole_words_only(self):
        chunks = [self._chunk(1, 1, "Sunnyside was quiet.")]
        assert sheets.select_passages(chunks, ["Sunny"], 5) == []


class TestNormaliseFact:
    def test_the_chapter_comes_from_the_passage_not_the_model(self):
        """The guarantee the whole spoiler scheme rests on."""
        fact = sheets.normalise_fact(
            {"field": "role", "text": "A junior Sleeper.", "chapter": 99}, CHUNK, ALL)
        assert fact["chapter"] == 12
        assert fact["citation"] == CHUNK["citation"]

    def test_a_field_the_tier_did_not_earn_is_dropped(self):
        allowed = sheets.fields_for("secondary")
        assert "quirks" not in allowed
        assert sheets.normalise_fact(
            {"field": "quirks", "text": "Taps twice."}, CHUNK, allowed) is None

    def test_an_unknown_field_is_dropped(self):
        assert sheets.normalise_fact(
            {"field": "vibes", "text": "Mysterious."}, CHUNK, ALL) is None

    def test_empty_text_is_dropped(self):
        assert sheets.normalise_fact({"field": "role", "text": "  "}, CHUNK, ALL) is None

    def test_a_relationship_with_nobody_on_the_other_end_is_dropped(self):
        """Usually the model filing a personality trait under the wrong field."""
        assert sheets.normalise_fact(
            {"field": "relationship", "text": "Distrusts people."}, CHUNK, ALL) is None

    def test_subject_is_kept_only_for_relationships(self):
        rel = sheets.normalise_fact(
            {"field": "relationship", "text": "Fears her.", "subject": "Nephis"}, CHUNK, ALL)
        role = sheets.normalise_fact(
            {"field": "role", "text": "A Sleeper.", "subject": "Nephis"}, CHUNK, ALL)
        assert rel["subject"] == "Nephis"
        assert role["subject"] == ""

    def test_text_is_capped(self):
        fact = sheets.normalise_fact({"field": "role", "text": "x" * 900}, CHUNK, ALL)
        assert len(fact["text"]) <= sheets.MAX_FACT


class TestFactKey:
    def test_the_same_claim_phrased_twice_is_one_fact(self):
        a = sheets.fact_key("role", "He is the leader of the expedition.")
        b = sheets.fact_key("role", "The leader of the expedition, he is!")
        assert a == b

    def test_different_claims_stay_apart(self):
        assert sheets.fact_key("role", "Leader of the expedition") != \
               sheets.fact_key("role", "Cook for the expedition")

    def test_the_same_words_in_different_fields_stay_apart(self):
        assert sheets.fact_key("role", "Quiet and watchful") != \
               sheets.fact_key("personality", "Quiet and watchful")

    def test_relationships_to_different_people_stay_apart(self):
        assert sheets.fact_key("relationship", "Trusts them", "Nephis") != \
               sheets.fact_key("relationship", "Trusts them", "Cassie")


class TestAsOf:
    def _facts(self):
        return [sheets.normalise_fact({"field": "role", "text": "A junior Sleeper."}, CHUNK, ALL),
                sheets.normalise_fact({"field": "role", "text": "The First Sleeper, secretly."},
                                      LATER, ALL)]

    def test_a_later_reveal_is_withheld(self):
        visible = sheets.as_of(self._facts(), 20)
        assert [f["chapter"] for f in visible] == [12]

    def test_no_cutoff_means_the_whole_book(self):
        assert len(sheets.as_of(self._facts(), None)) == 2

    def test_the_cutoff_chapter_itself_is_included(self):
        assert len(sheets.as_of(self._facts(), 12)) == 1

    def test_group_orders_each_field_by_chapter(self):
        grouped = sheets.group(list(reversed(self._facts())))
        assert [f["chapter"] for f in grouped["role"]] == [12, 30]


class TestDossier:
    def _person(self):
        return {"name": "Sunny", "aliases": ["the Nightmare"], "tier": "primary",
                "note": "A Sleeper.", "mentions": 400, "dialogue_hits": 90,
                "chapter_count": 34, "first_chapter": 1, "last_chapter": 40}

    def _facts(self):
        return [sheets.normalise_fact({"field": "role", "text": "A junior Sleeper."}, CHUNK, ALL),
                sheets.normalise_fact({"field": "role", "text": "The First Sleeper, secretly."},
                                      LATER, ALL)]

    def test_withheld_facts_are_counted_not_silently_dropped(self):
        doc = sheets.build_dossier({"title": "T", "slug": "t"}, self._person(),
                                   self._facts(), "m", chapter=20)
        assert doc["as_of_chapter"] == 20
        assert doc["withheld_facts"] == 1
        assert len(doc["fields"]["role"]) == 1

    def test_a_full_dossier_says_so_rather_than_omitting_the_field(self):
        doc = sheets.build_dossier({"title": "T", "slug": "t"}, self._person(),
                                   self._facts(), "m")
        assert doc["as_of_chapter"] is None
        assert doc["withheld_facts"] == 0

    def test_the_tier_travels_to_persona_forge(self):
        """PF reads the tier to decide sprite counts; LF holds no expression logic."""
        doc = sheets.build_dossier({"title": "T", "slug": "t"}, self._person(),
                                   self._facts(), "m")
        assert doc["tier"] == "primary"
        assert doc["aliases"] == ["the Nightmare"]


class TestFactPersistence:
    """The store's one rule of its own: the EARLIEST chapter wins."""

    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        from app import db, facts_store
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite3")
        db.init_db()
        with db.connect() as conn:
            conn.execute("INSERT INTO books (id, title, slug) VALUES (1, 'B', 'b')")
            conn.execute("INSERT INTO characters (id, book_id, char_key, name)"
                         " VALUES (1, 1, 'sunny', 'Sunny')")
        return facts_store

    def _fact(self, chunk, text="He leads the expedition."):
        return sheets.normalise_fact({"field": "role", "text": text}, chunk, ALL)

    def test_restating_a_claim_later_does_not_move_the_stamp_forward(self, store):
        store.upsert(1, 1, [self._fact(CHUNK)])
        store.upsert(1, 1, [self._fact(LATER)])
        rows = store.list_facts(1, 1)
        assert len(rows) == 1
        assert rows[0]["chapter"] == 12

    def test_finding_a_claim_earlier_moves_the_stamp_back(self, store):
        """Otherwise an 'as of chapter 20' export withholds what the reader knew at 12."""
        store.upsert(1, 1, [self._fact(LATER)])
        store.upsert(1, 1, [self._fact(CHUNK)])
        rows = store.list_facts(1, 1)
        assert len(rows) == 1
        assert rows[0]["chapter"] == 12
        assert rows[0]["citation"] == CHUNK["citation"]

    def test_as_of_filters_in_sql_too(self, store):
        store.upsert(1, 1, [self._fact(CHUNK), self._fact(LATER, "He fears the dark.")])
        assert len(store.list_facts(1, 1, chapter=20)) == 1
        assert len(store.list_facts(1, 1)) == 2

    def test_an_edit_re_derives_the_dedupe_key(self, store):
        """A corrected fact must still dedupe against a later run re-extracting it."""
        store.upsert(1, 1, [self._fact(CHUNK)])
        fact_id = store.list_facts(1, 1)[0]["id"]
        store.edit(fact_id, text="He commands the expedition.")
        store.upsert(1, 1, [self._fact(CHUNK, "He commands the expedition!")])
        assert len(store.list_facts(1, 1)) == 1

    def test_clear_spares_curated_rows(self, store):
        store.upsert(1, 1, [self._fact(CHUNK), self._fact(LATER, "He fears the dark.")])
        keep = store.list_facts(1, 1)[0]["id"]
        store.set_status(keep, "kept")
        store.clear(1, only_proposed=True)
        rows = store.list_facts(1, 1)
        assert [r["id"] for r in rows] == [keep]
