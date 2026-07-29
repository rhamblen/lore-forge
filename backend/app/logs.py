"""Structured logging for Lore Forge.

Every record goes three places:
  1. stdout            — so `docker logs lore-forge` works normally
  2. an in-memory ring — what the Logs tab reads (fast, no disk hit per poll)
  3. a rolling JSONL   — the logs/ folder, so BOOT history survives a restart and a
                         crash loop can actually be diagnosed after the fact

Categories (same set as Persona Forge — merge-first rule 1):
  boot        startup: config, db init, mount checks
  integration outbound calls to Ollama
  process     pipeline steps: book/parse/index/extraction lifecycle
  local       local processing: files, folders, parsing, db
  api         inbound HTTP requests
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Iterable

CATEGORIES = ("boot", "integration", "process", "local", "api")
# Ordered least→most severe. `verbose` is the firehose (every handshake, each file
# copied, each poll); `debug` is step-level detail; `info` is milestones; then warn/error.
LEVELS = ("verbose", "debug", "info", "warn", "error")

_RING_MAX = int(os.getenv("LOG_RING_SIZE", "2000"))
_FILE_MAX_BYTES = int(os.getenv("LOG_FILE_MAX_BYTES", str(2 * 1024 * 1024)))
_LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
_LOG_FILE = _LOG_DIR / "lore-forge.jsonl"

_ring: Deque[dict[str, Any]] = deque(maxlen=_RING_MAX)
_lock = threading.Lock()
_seq = 0

_VERBOSE_NUM = 5  # below logging.DEBUG (10)
logging.addLevelName(_VERBOSE_NUM, "VERB")

_stdout = logging.getLogger("lore_forge")
if not _stdout.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    _stdout.addHandler(_h)
    _stdout.setLevel(_VERBOSE_NUM)  # let verbose reach `docker logs` too, not just the UI
    _stdout.propagate = False

_LEVEL_TO_PY = {"verbose": _VERBOSE_NUM, "debug": logging.DEBUG, "info": logging.INFO,
                "warn": logging.WARNING, "error": logging.ERROR}


def _write_file(rec: dict[str, Any]) -> None:
    """Append to the rolling JSONL. Never let logging break the app."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _FILE_MAX_BYTES:
            _LOG_FILE.replace(_LOG_FILE.with_suffix(".jsonl.1"))
        with _LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def log(level: str, category: str, message: str, **detail: Any) -> None:
    global _seq
    level = level if level in LEVELS else "info"
    category = category if category in CATEGORIES else "local"
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "category": category,
        "message": message,
        "detail": detail or None,
    }
    with _lock:
        _seq += 1
        rec["id"] = _seq
        _ring.append(rec)

    extra = " " + json.dumps(detail, default=str) if detail else ""
    _stdout.log(_LEVEL_TO_PY[level], f"[{category}] {message}{extra}")
    _write_file(rec)


# convenience wrappers -------------------------------------------------------
def verbose(category: str, message: str, **d: Any) -> None: log("verbose", category, message, **d)
def debug(category: str, message: str, **d: Any) -> None: log("debug", category, message, **d)
def info(category: str, message: str, **d: Any) -> None: log("info", category, message, **d)
def warn(category: str, message: str, **d: Any) -> None: log("warn", category, message, **d)
def error(category: str, message: str, **d: Any) -> None: log("error", category, message, **d)


class timed:
    """Context manager that logs how long a step took (and any exception).

    with logs.timed("integration", "ComfyUI submit", workflow="base-character"):
        ...
    """

    def __init__(self, category: str, message: str, level: str = "info", **detail: Any):
        self.category, self.message, self.level, self.detail = category, message, level, detail

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        ms = round((time.perf_counter() - self._t0) * 1000)
        if exc is None:
            log(self.level, self.category, f"{self.message} — ok", ms=ms, **self.detail)
        else:
            log("error", self.category, f"{self.message} — failed: {exc}", ms=ms, **self.detail)
        return False  # never swallow


def read(
    level: str | None = None,
    category: str | None = None,
    since_id: int = 0,
    limit: int = 300,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-last slice of the ring, filtered."""
    min_rank = LEVELS.index(level) if level in LEVELS else 0
    with _lock:
        items: Iterable[dict[str, Any]] = list(_ring)
    out = [
        r for r in items
        if r["id"] > since_id
        and LEVELS.index(r["level"]) >= min_rank
        and (category in (None, "", "all") or r["category"] == category)
        and (not search or search.lower() in r["message"].lower())
    ]
    return out[-limit:]


def load_persisted(limit: int = 500) -> list[dict[str, Any]]:
    """Read back the JSONL — used to show logs from BEFORE the current process."""
    if not _LOG_FILE.is_file():
        return []
    try:
        lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def stats() -> dict[str, Any]:
    with _lock:
        items = list(_ring)
    by_level = {lv: sum(1 for r in items if r["level"] == lv) for lv in LEVELS}
    by_cat = {c: sum(1 for r in items if r["category"] == c) for c in CATEGORIES}
    return {
        "buffered": len(items),
        "ring_max": _RING_MAX,
        "by_level": by_level,
        "by_category": by_cat,
        "file": str(_LOG_FILE),
        "file_exists": _LOG_FILE.is_file(),
        "file_bytes": _LOG_FILE.stat().st_size if _LOG_FILE.is_file() else 0,
    }
