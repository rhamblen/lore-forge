"""Persistence for quests — the journey.

Same contract as the other stores: re-running merges into existing rows, curation
outranks extraction, aliases union.

The one behaviour specific to quests: fields **fill in** rather than overwrite. Chapter 3
may name the reward and chapter 9 the penalty, and the merged quest must end up holding
both. `first_chapter` keeps the earliest sighting, because that is the quest's place in
the journey.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, extract

_TEXT_FIELDS = ("objective", "giver", "requirements", "reward", "penalty", "deadline")


def _hydrate(row: Any) -> dict[str, Any]:
    d = dict(row)
    for src, dst in (("aliases_json", "aliases"), ("citations_json", "citations")):
        try:
            d[dst] = json.loads(d.pop(src) or "[]")
        except json.JSONDecodeError:
            d[dst] = []
    d["citation_count"] = len(d["citations"])
    d["key_count"] = 1 + len(d["aliases"])
    return d


def upsert(book_id: int, quests: list[dict[str, Any]]) -> tuple[int, int]:
    inserted = updated = 0
    with db.connect() as conn:
        for q in quests:
            key = extract.quest_key(q)
            row = conn.execute(
                "SELECT * FROM quests WHERE book_id = ? AND quest_key = ?",
                (book_id, key)).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO quests (book_id, quest_key, name, aliases_json, kind,"
                    " objective, giver, requirements, reward, penalty, deadline, outcome,"
                    " first_chapter, citations_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (book_id, key, q["name"], json.dumps(q["aliases"]), q["kind"],
                     q["objective"], q["giver"], q["requirements"], q["reward"],
                     q["penalty"], q["deadline"], q["outcome"], q["first_chapter"],
                     json.dumps(q["citations"])),
                )
                inserted += 1
                continue

            try:
                aliases = json.loads(row["aliases_json"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            try:
                cites = json.loads(row["citations_json"] or "[]")
            except json.JSONDecodeError:
                cites = []

            known = {a.lower() for a in aliases} | {row["name"].lower()}
            for a in q["aliases"]:
                if a.lower() not in known:
                    known.add(a.lower())
                    aliases.append(a)
            cited = {(c.get("chunk_id"), c.get("chapter")) for c in cites}
            for c in q["citations"]:
                if (c.get("chunk_id"), c.get("chapter")) not in cited:
                    cites.append(c)

            fields: dict[str, Any] = {
                "aliases_json": json.dumps(aliases),
                "citations_json": json.dumps(cites),
                "first_chapter": min(row["first_chapter"] or 10**6,
                                     q["first_chapter"] or 10**6),
            }
            if not row["edited"]:
                for f in _TEXT_FIELDS:
                    new = q.get(f, "")
                    if new and len(new) > len(row[f] or ""):
                        fields[f] = new
                if q["kind"] != "unknown" and row["kind"] == "unknown":
                    fields["kind"] = q["kind"]
                rank = extract._OUTCOME_RANK
                if rank.get(q["outcome"], 0) > rank.get(row["outcome"], 0):
                    fields["outcome"] = q["outcome"]

            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE quests SET {sets} WHERE id = ?",
                         (*fields.values(), row["id"]))
            updated += 1
    return inserted, updated


def list_quests(book_id: int, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM quests WHERE book_id = ?"
    args: list[Any] = [book_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    # Journey order, not popularity order — this list is meant to be read start to end.
    sql += " ORDER BY first_chapter, name COLLATE NOCASE"
    with db.connect() as conn:
        return [_hydrate(r) for r in conn.execute(sql, args).fetchall()]


def set_status(quest_id: int, status: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        conn.execute("UPDATE quests SET status = ? WHERE id = ?", (status, quest_id))
        row = conn.execute("SELECT * FROM quests WHERE id = ?", (quest_id,)).fetchone()
    return _hydrate(row) if row else None


def edit(quest_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {k: v for k, v in fields.items()
               if k in {"name", "kind", "outcome", *_TEXT_FIELDS} and v is not None}
    if fields.get("aliases") is not None:
        allowed["aliases_json"] = json.dumps(
            [a for a in (str(x).strip() for x in fields["aliases"]) if a])
    if not allowed:
        return None
    allowed["edited"] = 1
    with db.connect() as conn:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE quests SET {sets} WHERE id = ?", (*allowed.values(), quest_id))
        row = conn.execute("SELECT * FROM quests WHERE id = ?", (quest_id,)).fetchone()
    return _hydrate(row) if row else None


def clear(book_id: int, only_proposed: bool = True) -> int:
    with db.connect() as conn:
        if only_proposed:
            cur = conn.execute(
                "DELETE FROM quests WHERE book_id = ? AND status = 'proposed' AND edited = 0",
                (book_id,))
        else:
            cur = conn.execute("DELETE FROM quests WHERE book_id = ?", (book_id,))
        return cur.rowcount


def counts(book_id: int) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM quests WHERE book_id = ? GROUP BY status",
            (book_id,)).fetchall()
    out = {"proposed": 0, "kept": 0, "discarded": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out
