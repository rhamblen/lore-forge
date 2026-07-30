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
    d["key_count"] = 1 + len(d["aliases"])
    return d


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
                conn.execute(
                    "INSERT INTO characters (book_id, char_key, name, aliases_json, note,"
                    " tier, tier_reason, mentions, dialogue_hits, chapter_count,"
                    " first_chapter, last_chapter) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, key, person["name"], json.dumps(person.get("aliases") or []),
                     person.get("note", ""), tier, reason, person["mentions"],
                     person["dialogue_hits"], person["chapter_count"],
                     person["first_chapter"], person["last_chapter"]),
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
