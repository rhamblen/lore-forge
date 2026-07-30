"""Persistence for extracted rules.

Kept apart from `extract.py` so the extraction logic stays pure and testable without a
database, and apart from `main.py` so the job handler reads as a sequence of steps
rather than a wall of SQL.

Upsert semantics matter here: a re-run must **merge into** what already exists rather
than replacing it, because a human may have edited or discarded rows in between. An
extraction pass that silently reverts curation is worse than one that never ran.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, extract


def upsert(book_id: int, rules: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new rules, union citations into existing ones.

    Returns `(inserted, updated)`.

    A row a human has **edited** keeps its text — only its citations grow. A row a human
    has **discarded** stays discarded; a later run re-citing it does not resurrect it.
    Both are the same principle: curation outranks extraction.
    """
    inserted = updated = 0
    with db.connect() as conn:
        for rule in rules:
            key = extract.rule_key(rule)
            row = conn.execute(
                "SELECT * FROM rules WHERE book_id = ? AND rule_key = ?", (book_id, key)
            ).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO rules (book_id, rule_key, kind, name, statement, formula,"
                    " confidence, evidence_excerpt, citations_json, aliases_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (book_id, key, rule["kind"], rule["name"], rule["statement"],
                     rule["formula"], rule["confidence"], rule["evidence_excerpt"],
                     json.dumps(rule["citations"]),
                     json.dumps(rule.get("aliases") or [])),
                )
                inserted += 1
                continue

            try:
                cites = json.loads(row["citations_json"]) or []
            except json.JSONDecodeError:
                cites = []
            seen = {(c.get("chunk_id"), c.get("chapter")) for c in cites}
            for c in rule["citations"]:
                if (c.get("chunk_id"), c.get("chapter")) not in seen:
                    cites.append(c)
                    seen.add((c.get("chunk_id"), c.get("chapter")))

            # Aliases union unconditionally — even on an edited row. Curating the prose
            # is not a request to lose a trigger the book actually uses.
            try:
                aliases = json.loads(row["aliases_json"] or "[]")
            except (json.JSONDecodeError, IndexError, KeyError):
                aliases = []
            known = {a.lower() for a in aliases} | {row["name"].lower()}
            for a in rule.get("aliases") or []:
                if a.lower() not in known:
                    known.add(a.lower())
                    aliases.append(a)

            fields: dict[str, Any] = {"citations_json": json.dumps(cites),
                                      "aliases_json": json.dumps(aliases)}
            if not row["edited"]:
                promote = (rule["confidence"] == "stated" and row["confidence"] != "stated")
                longer = (rule["confidence"] == row["confidence"]
                          and len(rule["statement"]) > len(row["statement"] or ""))
                if promote or longer:
                    fields.update(statement=rule["statement"], confidence=rule["confidence"])
                    if rule["formula"]:
                        fields["formula"] = rule["formula"]
                    if rule["evidence_excerpt"]:
                        fields["evidence_excerpt"] = rule["evidence_excerpt"]

            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE rules SET {sets} WHERE id = ?",
                         (*fields.values(), row["id"]))
            updated += 1
    return inserted, updated


def list_rules(book_id: int, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM rules WHERE book_id = ?"
    args: list[Any] = [book_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    # Most-corroborated first: a mechanic the book restates is a load-bearing one.
    sql += " ORDER BY json_array_length(citations_json) DESC, kind, name COLLATE NOCASE"
    with db.connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for src, dst in (("citations_json", "citations"), ("aliases_json", "aliases")):
            try:
                d[dst] = json.loads(d.pop(src, None) or "[]")
            except json.JSONDecodeError:
                d[dst] = []
        d["citation_count"] = len(d["citations"])
        d["key_count"] = 1 + len(d["aliases"])   # the name is a key too
        out.append(d)
    return out


def set_status(rule_id: int, status: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        conn.execute("UPDATE rules SET status = ? WHERE id = ?", (status, rule_id))
        row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def edit(rule_id: int, **fields: Any) -> dict[str, Any] | None:
    """Human edit. Sets `edited`, which permanently protects the text from later runs."""
    allowed = {k: v for k, v in fields.items()
               if k in {"name", "statement", "formula", "kind", "confidence"} and v is not None}
    if not allowed:
        return None
    allowed["edited"] = 1
    with db.connect() as conn:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE rules SET {sets} WHERE id = ?", (*allowed.values(), rule_id))
        row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def clear(book_id: int, only_proposed: bool = True) -> int:
    """Drop extracted rules. Defaults to sparing anything a human has ruled on."""
    with db.connect() as conn:
        if only_proposed:
            cur = conn.execute(
                "DELETE FROM rules WHERE book_id = ? AND status = 'proposed' AND edited = 0",
                (book_id,))
        else:
            cur = conn.execute("DELETE FROM rules WHERE book_id = ?", (book_id,))
        return cur.rowcount


def counts(book_id: int) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM rules WHERE book_id = ? GROUP BY status",
            (book_id,)).fetchall()
    out = {"proposed": 0, "kept": 0, "discarded": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out
