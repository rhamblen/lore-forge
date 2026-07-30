"""Persistence for the character census.

Same contract as the other stores, with one addition specific to characters: **a human
tier override is permanent.** `tier_locked` survives a re-census, because the whole point
of showing the tier and its reasoning is that you can correct it — and a correction that
gets overwritten on the next run is not a correction.

Counts, by contrast, are always refreshed: they are measurements of the text, and if the
text was re-parsed the old numbers are simply wrong.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import census, db


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _hydrate(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["aliases"] = json.loads(d.pop("aliases_json") or "[]")
    except json.JSONDecodeError:
        d["aliases"] = []
    try:
        d["chapters"] = json.loads(d.pop("chapters_json", None) or "[]")
    except json.JSONDecodeError:
        d["chapters"] = []
    d["key_count"] = 1 + len(d["aliases"])
    return d


def _find_by_any_name(conn: Any, book_id: int, names: list[str]) -> Any:
    """Locate a character by its name OR any of its aliases.

    This is what makes a manual merge **stick**. Once you fold "Subject Diane
    Fitzgerald" into "Diane Fitzgerald", that string lives on as an alias — so when the
    next census proposes it as a separate person again, it lands on the existing
    character instead of resurrecting the row you just merged away.
    """
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    if not wanted:
        return None
    for row in conn.execute("SELECT * FROM characters WHERE book_id = ?", (book_id,)):
        try:
            aliases = json.loads(row["aliases_json"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        forms = {row["name"].lower(), *(a.lower() for a in aliases)}
        if forms & wanted:
            return row
    return None


def upsert(book_id: int, people: list[dict[str, Any]], total_chapters: int) -> tuple[int, int]:
    """Insert or refresh characters. Returns `(inserted, updated)`."""
    inserted = updated = 0
    with db.connect() as conn:
        for person in people:
            key = _key(person["name"])
            if not key:
                continue
            tier, reason = census.assign_tier(person, total_chapters)
            row = conn.execute(
                "SELECT * FROM characters WHERE book_id = ? AND char_key = ?",
                (book_id, key)).fetchone()
            if row is None:
                # Not found by key — but this name may already be an alias of someone,
                # because a human merged it there. Respect that over creating a duplicate.
                row = _find_by_any_name(
                    conn, book_id, [person["name"], *(person.get("aliases") or [])])

            if row is None:
                conn.execute(
                    "INSERT INTO characters (book_id, char_key, name, aliases_json, note,"
                    " tier, tier_reason, mentions, dialogue_hits, chapter_count,"
                    " first_chapter, last_chapter, chapters_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, key, person["name"], json.dumps(person.get("aliases") or []),
                     person.get("note", ""), tier, reason, person["mentions"],
                     person["dialogue_hits"], person["chapter_count"],
                     person["first_chapter"], person["last_chapter"],
                     json.dumps(person.get("chapters") or [])),
                )
                inserted += 1
                continue

            try:
                aliases = json.loads(row["aliases_json"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            known = {a.lower() for a in aliases} | {row["name"].lower()}
            for a in person.get("aliases") or []:
                if a.lower() not in known:
                    known.add(a.lower())
                    aliases.append(a)

            fields: dict[str, Any] = {
                "aliases_json": json.dumps(aliases),
                # Counts are measurements — always refresh them.
                "mentions": person["mentions"],
                "dialogue_hits": person["dialogue_hits"],
                "chapter_count": person["chapter_count"],
                "chapters_json": json.dumps(person.get("chapters") or []),
                "first_chapter": person["first_chapter"],
                "last_chapter": person["last_chapter"],
                "tier_reason": reason,
            }
            if not row["tier_locked"]:
                fields["tier"] = tier
            if not row["edited"] and person.get("note"):
                fields["note"] = person["note"]

            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE characters SET {sets} WHERE id = ?",
                         (*fields.values(), row["id"]))
            updated += 1
    return inserted, updated


_TIER_ORDER = {"primary": 0, "secondary": 1, "filler": 2}


def list_characters(book_id: int, tier: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM characters WHERE book_id = ?"
    args: list[Any] = [book_id]
    if tier:
        sql += " AND tier = ?"
        args.append(tier)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += (" ORDER BY CASE tier WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 ELSE 2 END,"
            " dialogue_hits DESC, mentions DESC")
    with db.connect() as conn:
        return [_hydrate(r) for r in conn.execute(sql, args).fetchall()]


def merge(book_id: int, keep_id: int, absorb_id: int) -> dict[str, Any] | None:
    """Fold `absorb_id` into `keep_id`. Returns the surviving character.

    Manual pairing exists because no heuristic catches every case. A relational
    reference — "Mom" for Diane Fitzgerald — shares no tokens with the real name, so
    containment matching can never propose it, and only a reader who has seen the
    context can say for certain.

    The absorbed name becomes an alias, which does two jobs at once: it keeps the
    lorebook trigger (that spelling appears in the text, so it must fire), and it makes
    the merge stick against a future census via `_find_by_any_name`.
    """
    if keep_id == absorb_id:
        return None
    with db.connect() as conn:
        keep = conn.execute(
            "SELECT * FROM characters WHERE id = ? AND book_id = ?",
            (keep_id, book_id)).fetchone()
        absorb = conn.execute(
            "SELECT * FROM characters WHERE id = ? AND book_id = ?",
            (absorb_id, book_id)).fetchone()
        if keep is None or absorb is None:
            return None

        def _json(row: Any, field: str) -> list[Any]:
            try:
                return json.loads(row[field] or "[]")
            except (json.JSONDecodeError, IndexError, KeyError):
                return []

        aliases = _json(keep, "aliases_json")
        known = {a.lower() for a in aliases} | {keep["name"].lower()}
        for form in [absorb["name"], *_json(absorb, "aliases_json")]:
            if form.lower() not in known:
                known.add(form.lower())
                aliases.append(form)

        # Chapters union — counts cannot be added, since the two may overlap.
        chapters = sorted(set(_json(keep, "chapters_json")) | set(_json(absorb, "chapters_json")))
        chapter_count = len(chapters) or max(keep["chapter_count"], absorb["chapter_count"])

        merged = {
            "name": census.preferred_name([keep["name"], absorb["name"]]),
            "aliases": aliases,
            "mentions": keep["mentions"] + absorb["mentions"],
            "dialogue_hits": keep["dialogue_hits"] + absorb["dialogue_hits"],
            "chapter_count": chapter_count,
            "first_chapter": min(keep["first_chapter"] or 10**6,
                                 absorb["first_chapter"] or 10**6),
            "last_chapter": max(keep["last_chapter"], absorb["last_chapter"]),
        }
        # The combined evidence may well change the tier — a "filler" absorbed into a
        # "secondary" can push it to primary — unless a human has pinned it.
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM chapters WHERE book_id = ?", (book_id,)
        ).fetchone()["n"]
        tier, reason = census.assign_tier(merged, total)

        fields: dict[str, Any] = {
            "name": merged["name"],
            "char_key": _key(merged["name"]),
            "aliases_json": json.dumps(aliases),
            "chapters_json": json.dumps(chapters),
            "mentions": merged["mentions"],
            "dialogue_hits": merged["dialogue_hits"],
            "chapter_count": merged["chapter_count"],
            "first_chapter": merged["first_chapter"],
            "last_chapter": merged["last_chapter"],
            "tier_reason": reason + "; merged",
            # A merge is a human judgement — protect it from the next census.
            "edited": 1,
        }
        if not keep["tier_locked"]:
            fields["tier"] = tier
        if not keep["note"] and absorb["note"]:
            fields["note"] = absorb["note"]

        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE characters SET {sets} WHERE id = ?",
                     (*fields.values(), keep_id))
        conn.execute("DELETE FROM characters WHERE id = ?", (absorb_id,))
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (keep_id,)).fetchone()
    return _hydrate(row) if row else None


def set_tier(char_id: int, tier: str) -> dict[str, Any] | None:
    """Human override. Locks the tier so a re-census cannot move it back."""
    if tier not in census.TIERS:
        return None
    with db.connect() as conn:
        conn.execute(
            "UPDATE characters SET tier = ?, tier_locked = 1,"
            " tier_reason = 'set by hand' WHERE id = ?", (tier, char_id))
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
    return _hydrate(row) if row else None


def set_status(char_id: int, status: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        conn.execute("UPDATE characters SET status = ? WHERE id = ?", (status, char_id))
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
    return _hydrate(row) if row else None


def edit(char_id: int, name: str | None = None, note: str | None = None,
         aliases: list[str] | None = None) -> dict[str, Any] | None:
    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name
    if note is not None:
        fields["note"] = note
    if aliases is not None:
        cleaned, seen = [], set()
        for a in aliases:
            a = str(a).strip()
            if a and a.lower() not in seen:
                seen.add(a.lower())
                cleaned.append(a)
        fields["aliases_json"] = json.dumps(cleaned)
    if not fields:
        return None
    fields["edited"] = 1
    with db.connect() as conn:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE characters SET {sets} WHERE id = ?", (*fields.values(), char_id))
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (char_id,)).fetchone()
    return _hydrate(row) if row else None


def clear(book_id: int, only_proposed: bool = True) -> int:
    with db.connect() as conn:
        if only_proposed:
            cur = conn.execute(
                "DELETE FROM characters WHERE book_id = ? AND status = 'proposed'"
                " AND edited = 0 AND tier_locked = 0", (book_id,))
        else:
            cur = conn.execute("DELETE FROM characters WHERE book_id = ?", (book_id,))
        return cur.rowcount


def counts(book_id: int) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT tier, COUNT(*) AS n FROM characters WHERE book_id = ?"
            " AND status != 'discarded' GROUP BY tier", (book_id,)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM characters WHERE book_id = ?", (book_id,)
        ).fetchone()["n"]
    out = {t: 0 for t in census.TIERS}
    for r in rows:
        out[r["tier"]] = r["n"]
    out["total"] = total
    return out
