"""The on-disk contract.

Every book gets a folder under the lore-builds root, and that folder — not the
database — is what Persona Forge reads at merge time (merge-first rule 3: the handoff
is a file contract, never a shared database).

    <lore-builds>/<book-slug>/
      book.json          manifest — sources, hashes, models used, run config
      sources/           uploaded original + extracted text, chapter-structured
      index/             retrieval index reports (vectors live in SQLite)
      review/            extraction report, citations, coreference conflicts

      st-import/         MIRRORS SILLYTAVERN'S OWN TREE, VERBATIM
        worlds/          <Book>.json          lorebooks           (L3)
        characters/      <Name>.json          V3 cards, JSON only (L4)
        QuickReplies/                                             (deferred)

      campaign/          engine inputs, NOT for copying into ST
        dossiers/ rules/ story/ canon/ relationships/              (L2, L5)

`st-import/` mirroring ST's tree verbatim is the whole answer to "easy to integrate":
integration is *copy the contents of st-import/ into default-user/* — no path
translation, no renaming, no per-file instructions. `campaign/` sits deliberately
outside it so runtime state can never be hand-copied into ST by accident.

Staged, never automatic — the same rule as sprites and VRM, and it also sidesteps the
known appdata SMB write denial (reads work; writes from Windows are root-denied).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import logs
from .config import BUILD_GID, BUILD_UID, LORE_BUILDS_ROOT, VERSION

# Created for every book, so the shape is inspectable from day one even though the
# later folders stay empty until their phase lands.
SUBFOLDERS = (
    "sources",
    "sources/text",
    "index",
    "review",
    "st-import/worlds",
    "st-import/characters",
    "st-import/QuickReplies",
    "campaign/dossiers",
    "campaign/rules",
    "campaign/story",
    "campaign/canon",
    "campaign/relationships",
)


def slugify(name: str) -> str:
    """Folder-safe slug. ASCII-folded because this becomes a path on a Linux share
    read over SMB from Windows, and accented folder names have bitten this project
    before."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:64] or "book"


def _chown(path: Path) -> None:
    """Hand a created folder to the shared-mount owner (Unraid nobody:users), so
    another container reading the tree isn't locked out. A no-op on Windows dev."""
    try:
        os.chown(path, BUILD_UID, BUILD_GID)
    except (AttributeError, PermissionError, OSError):
        pass


def book_dir(slug: str) -> Path:
    return LORE_BUILDS_ROOT / slug


def ensure_book_dir(slug: str) -> Path:
    root = book_dir(slug)
    for sub in ("",) + SUBFOLDERS:
        d = root / sub if sub else root
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            _chown(d)
    logs.verbose("local", "book folder ready", slug=slug, path=str(root))
    return root


def safe_path(slug: str, *parts: str) -> Path | None:
    """Resolve parts inside a book folder, refusing anything that escapes it."""
    root = book_dir(slug).resolve()
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        logs.warn("local", "refusing a path outside the book folder", path=str(target))
        return None
    return target


def write_manifest(book: dict[str, Any], chapters: list[dict[str, Any]] | None = None) -> Path | None:
    """Write `book.json`.

    The manifest is the self-describing half of the file contract: it records which
    models and settings produced what, so an index can be judged (or invalidated)
    without reading the database. Mirrors PF's `persona.json` sidecar convention.
    """
    slug = book["slug"]
    root = ensure_book_dir(slug)
    manifest = {
        "written_by": f"lore-forge {VERSION}",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "book": {
            "title": book.get("title", ""),
            "slug": slug,
            "author": book.get("author", ""),
        },
        "source": {
            "kind": book.get("source_kind", ""),
            "file": book.get("source_file", ""),
            "bytes": book.get("source_bytes", 0),
            "sha256": book.get("source_sha", ""),
        },
        "parse": {
            "status": book.get("parse_status", "none"),
            "chapters": book.get("chapter_count", 0),
            "words": book.get("word_count", 0),
        },
        "index": {
            "status": book.get("index_status", "none"),
            "embed_model": book.get("embed_model", ""),
            "dims": book.get("embed_dims", 0),
            "chunks": book.get("chunk_count", 0),
            "chunk_chars": book.get("chunk_chars", 0),
            "chunk_overlap": book.get("chunk_overlap", 0),
        },
    }
    if chapters is not None:
        manifest["chapters"] = [
            {"position": c["position"], "title": c["title"],
             "words": c["word_count"], "source_ref": c["source_ref"]}
            for c in chapters
        ]
    path = root / "book.json"
    try:
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        _chown(path)
        return path
    except OSError as exc:
        logs.error("local", f"could not write book.json: {exc}", slug=slug)
        return None


def write_chapter_text(slug: str, position: int, title: str, text: str) -> Path | None:
    """Drop a chapter as a plain .txt so the parse is checkable by eye — reading the
    text is how L0 is proven, and that must not require a database client."""
    stem = f"{position:03d}-{slugify(title) if title else 'chapter'}"
    path = safe_path(slug, "sources", "text", f"{stem}.txt")
    if path is None:
        return None
    try:
        path.write_text(text, encoding="utf-8")
        _chown(path)
        return path
    except OSError as exc:
        logs.error("local", f"could not write chapter text: {exc}", slug=slug, position=position)
        return None


def write_report(slug: str, folder: str, name: str, payload: dict[str, Any]) -> Path | None:
    path = safe_path(slug, folder, name)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        _chown(path)
        return path
    except OSError as exc:
        logs.error("local", f"could not write {folder}/{name}: {exc}", slug=slug)
        return None


def probe_writable(root: Path) -> tuple[bool, str]:
    """Actually write a file, don't just check the mode bits — a bind mount can be
    present and still root-owned. PF learned this the hard way."""
    if not root.is_dir():
        return False, "not mounted"
    probe = root / ".lf-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def disk_usage(slug: str) -> int:
    root = book_dir(slug)
    if not root.is_dir():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
