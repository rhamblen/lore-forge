"""Persistence for character-sheet facts (L2 pass 2).

Same contract as the other stores — a re-run merges rather than replaces, and curation
outranks extraction — with one rule specific to a chapter-stamped fact:

**The earliest chapter wins.** A claim restated in chapters 4, 12 and 30 is one fact, and
its stamp is 4. Keeping the latest would make an "as of chapter 10" export withhold
something the reader learned in chapter 4, which is the failure mode the whole scheme
exists to avoid. Withholding what the reader already knows is as wrong as spoiling what
they don't.
"""

from __future__ import annotations

from typing import Any

from . import db, sheets


def upsert(book_id: int, char_id: int, facts: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new facts; fold repeats into the existing row. Returns `(inserted, updated)`."""
    inserted = updated = 0
    with db.connect() as conn:
        for fact in facts:
            row = conn.execute(
                "SELECT * FROM character_facts WHERE char_id = ? AND fact_key = ?",
                (char_id, fact["fact_key"])).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO character_facts (book_id, char_id, field, text, subject,"
                    " chapter, citation, chunk_id, fact_key)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (book_id, char_id, fact["field"], fact["text"], fact["subject"],
                     fact["chapter"], fact["citation"], fact["chunk_id"],
                     fact["fact_key"]))
                inserted += 1
                continue

            # A repeat is evidence of nothing new except that the claim is visible
            # earlier than we thought. Only the stamp can move, and only downwards.
            new_chapter = fact.get("chapter") or 0
            if new_chapter and (not row["chapter"] or new_chapter < row["chapter"]):
                conn.execute(
                    "UPDATE character_facts SET chapter = ?, citation = ?, chunk_id = ?"
                    " WHERE id = ?",
                    (new_chapter, fact["citation"], fact["chunk_id"], row["id"]))
                updated += 1
    return inserted, updated


def list_facts(book_id: int, char_id: int | None = None, status: str | None = None,
               chapter: int | None = None) -> list[dict[str, Any]]:
    """Facts, optionally for one character, one status, and `as of` a chapter."""
    sql = "SELECT * FROM character_facts WHERE book_id = ?"
    args: list[Any] = [book_id]
    if char_id is not None:
        sql += " AND char_id = ?"
        args.append(char_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    if chapter is not None:
        sql += " AND chapter <= ?"
        args.append(chapter)
    sql += " ORDER BY char_id, field, chapter, id"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def set_status(fact_id: int, status: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        conn.execute("UPDATE character_facts SET status = ? WHERE id = ?",
                     (status, fact_id))
        row = conn.execute("SELECT * FROM character_facts WHERE id = ?",
                           (fact_id,)).fetchone()
    return dict(row) if row else None


def edit(fact_id: int, text: str | None = None, field: str | None = None,
         subject: str | None = None, chapter: int | None = None) -> dict[str, Any] | None:
    """Human edit. Sets `edited`, and re-derives `fact_key` so a corrected fact still
    dedupes against a later run that re-extracts the same claim.

    The chapter is editable because it is the one stamp a human can genuinely know
    better: a reader who recognises a trait as established earlier can move it back.
    """
    allowed: dict[str, Any] = {}
    if text:
        allowed["text"] = text
    if field in sheets.FIELDS:
        allowed["field"] = field
    if subject is not None:
        allowed["subject"] = subject
    if chapter is not None and chapter >= 0:
        allowed["chapter"] = chapter
    if not allowed:
        return None

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM character_facts WHERE id = ?",
                           (fact_id,)).fetchone()
        if row is None:
            return None
        allowed["fact_key"] = sheets.fact_key(
            allowed.get("field", row["field"]),
            allowed.get("text", row["text"]),
            allowed.get("subject", row["subject"]))
        allowed["edited"] = 1
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE character_facts SET {sets} WHERE id = ?",
                     (*allowed.values(), fact_id))
        row = conn.execute("SELECT * FROM character_facts WHERE id = ?",
                           (fact_id,)).fetchone()
    return dict(row) if row else None


def clear(book_id: int, char_id: int | None = None, only_proposed: bool = True) -> int:
    with db.connect() as conn:
        sql = "DELETE FROM character_facts WHERE book_id = ?"
        args: list[Any] = [book_id]
        if char_id is not None:
            sql += " AND char_id = ?"
            args.append(char_id)
        if only_proposed:
            sql += " AND status = 'proposed' AND edited = 0"
        return conn.execute(sql, args).rowcount


def counts(book_id: int, char_id: int | None = None) -> dict[str, int]:
    sql = ("SELECT status, COUNT(*) AS n FROM character_facts WHERE book_id = ?")
    args: list[Any] = [book_id]
    if char_id is not None:
        sql += " AND char_id = ?"
        args.append(char_id)
    sql += " GROUP BY status"
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = {"proposed": 0, "kept": 0, "discarded": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out


def counts_by_character(book_id: int) -> dict[int, int]:
    """Facts per character, for the list view — so you can see at a glance which sheets
    are written and which are still empty."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT char_id, COUNT(*) AS n FROM character_facts"
            " WHERE book_id = ? AND status != 'discarded' GROUP BY char_id",
            (book_id,)).fetchall()
    return {r["char_id"]: r["n"] for r in rows}
