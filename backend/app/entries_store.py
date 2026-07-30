"""Persistence for world entities (the L3 lorebook's source rows).

Same contract as `rules_store`: re-running an extraction **merges into** what exists
rather than replacing it, so curation always outranks extraction. The one difference that
matters is aliases — they are unioned on every merge, because a dropped alias is a
lorebook entry that silently never fires.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, extract


def upsert(book_id: int, entities: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new entities, union aliases and citations into existing ones."""
    inserted = updated = 0
    with db.connect() as conn:
        for ent in entities:
            key = extract.entity_key(ent)
            row = conn.execute(
                "SELECT * FROM lore_entries WHERE book_id = ? AND entry_key = ?",
                (book_id, key)).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO lore_entries (book_id, entry_key, kind, name, aliases_json,"
                    " summary, citations_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (book_id, key, ent["kind"], ent["name"], json.dumps(ent["aliases"]),
                     ent["summary"], json.dumps(ent["citations"])),
                )
                inserted += 1
                continue

            try:
                aliases = json.loads(row["aliases_json"]) or []
            except json.JSONDecodeError:
                aliases = []
            try:
                cites = json.loads(row["citations_json"]) or []
            except json.JSONDecodeError:
                cites = []

            # Aliases union even on an edited row: a human curating the *prose* has not
            # asked to lose a trigger the book actually uses.
            seen = {a.lower() for a in aliases} | {row["name"].lower()}
            for a in ent["aliases"]:
                if a.lower() not in seen:
                    seen.add(a.lower())
                    aliases.append(a)

            cited = {(c.get("chunk_id"), c.get("chapter")) for c in cites}
            for c in ent["citations"]:
                if (c.get("chunk_id"), c.get("chapter")) not in cited:
                    cites.append(c)

            fields: dict[str, Any] = {"aliases_json": json.dumps(aliases),
                                      "citations_json": json.dumps(cites)}
            if not row["edited"] and len(ent["summary"]) > len(row["summary"] or ""):
                fields["summary"] = ent["summary"]

            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE lore_entries SET {sets} WHERE id = ?",
                         (*fields.values(), row["id"]))
            updated += 1
    return inserted, updated


def _hydrate(row: Any) -> dict[str, Any]:
    d = dict(row)
    for src, dst in (("aliases_json", "aliases"), ("citations_json", "citations")):
        try:
            d[dst] = json.loads(d.pop(src) or "[]")
        except json.JSONDecodeError:
            d[dst] = []
    d["citation_count"] = len(d["citations"])
    d["key_count"] = 1 + len(d["aliases"])      # the name is a key too
    return d


def list_entries(book_id: int, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM lore_entries WHERE book_id = ?"
    args: list[Any] = [book_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += (" ORDER BY json_array_length(citations_json) DESC, kind,"
            " name COLLATE NOCASE")
    with db.connect() as conn:
        return [_hydrate(r) for r in conn.execute(sql, args).fetchall()]


def set_status(entry_id: int, status: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        conn.execute("UPDATE lore_entries SET status = ? WHERE id = ?", (status, entry_id))
        row = conn.execute("SELECT * FROM lore_entries WHERE id = ?", (entry_id,)).fetchone()
    return _hydrate(row) if row else None


def edit(entry_id: int, name: str | None = None, summary: str | None = None,
         kind: str | None = None, aliases: list[str] | None = None) -> dict[str, Any] | None:
    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name
    if summary:
        fields["summary"] = summary
    if kind:
        fields["kind"] = kind
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
        conn.execute(f"UPDATE lore_entries SET {sets} WHERE id = ?",
                     (*fields.values(), entry_id))
        row = conn.execute("SELECT * FROM lore_entries WHERE id = ?", (entry_id,)).fetchone()
    return _hydrate(row) if row else None


def clear(book_id: int, only_proposed: bool = True) -> int:
    with db.connect() as conn:
        if only_proposed:
            cur = conn.execute(
                "DELETE FROM lore_entries WHERE book_id = ? AND status = 'proposed'"
                " AND edited = 0", (book_id,))
        else:
            cur = conn.execute("DELETE FROM lore_entries WHERE book_id = ?", (book_id,))
        return cur.rowcount


def counts(book_id: int) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM lore_entries WHERE book_id = ?"
            " GROUP BY status", (book_id,)).fetchall()
    out = {"proposed": 0, "kept": 0, "discarded": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out
