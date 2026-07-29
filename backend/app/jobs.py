"""Generic background job engine.

Ported verbatim in behaviour from Persona Forge 0.7.0 — deliberately, under
merge-first rule 1 (same conventions as PF). A single in-process asyncio worker
drains a persisted FIFO of jobs, advancing the one running job stage-by-stage until
it finishes, so a long pipeline (parse a 600-page novel, then embed 4000 chunks)
completes **unattended, with the browser closed**.

Kinds registered here: `parse` (L0) and `index` (L1). Extraction (L2), lorebook
emit (L3) and card emit (L4) plug in as new handlers with zero engine changes.

Design notes:
- **Serial by design.** At most one job runs at a time. Parsing is CPU-bound and
  embedding contends for the same GPU as ComfyUI/training, so one-at-a-time is a
  feature, not a limitation. A future `lane` column can let the two run in parallel.
- **Resume-safe.** All progress lives in the row (`stage` + `state_json`), never in
  memory, so a container restart resumes rather than restarting.
- **Handlers are pluggable.** A handler is any object exposing
  ``async def tick(job: dict) -> tuple[str, str]`` returning ``(status, message)``
  where status is RUNNING / DONE / ERROR. On the FIRST tick the job's `stage` is ''.
  `tick` must be idempotent (it can be called again after a restart mid-stage).
  main.py registers handlers.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Protocol

from . import db, logs

RUNNING = "running"
DONE = "done"
ERROR = "error"
QUEUED = "queued"
CANCELED = "canceled"

# Faster than PF's 12s: these stages are local CPU/HTTP work with no
# ComfyUI queue to wait on, so a tighter loop makes the UI feel live.
POLL_SECONDS = float(os.getenv("JOBS_POLL_SECONDS", "3"))

_UPDATABLE = {"status", "stage", "message", "progress", "state_json", "result_json",
              "started_at", "finished_at"}


class Handler(Protocol):
    async def tick(self, job: dict[str, Any]) -> tuple[str, str]: ...


HANDLERS: dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    HANDLERS[kind] = handler
    logs.verbose("boot", "job handler registered", kind=kind)


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #

def enqueue(kind: str, book_id: int | None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (book_id, kind, status, params_json) VALUES (?, ?, 'queued', ?)",
            (book_id, kind, json.dumps(params or {})),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
    logs.info("process", f"job queued: {kind}", job_id=cur.lastrowid, book_id=book_id)
    return dict(row)


def get(job_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(book_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with db.connect() as conn:
        if book_id is None:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE book_id = ? ORDER BY id DESC LIMIT ?", (book_id, limit)
            ).fetchall()
    return [dict(r) for r in rows]


def update(job_id: int, **fields: Any) -> None:
    cols = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db.connect() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*cols.values(), job_id))


def params_of(job: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(job.get("params_json") or "{}")
    except json.JSONDecodeError:
        return {}


def state_of(job: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(job.get("state_json") or "{}")
    except json.JSONDecodeError:
        return {}


def set_state(job_id: int, state: dict[str, Any]) -> None:
    update(job_id, state_json=json.dumps(state))


def set_stage(job_id: int, stage: str, message: str = "", progress: float | None = None) -> None:
    fields: dict[str, Any] = {"stage": stage, "message": message}
    if progress is not None:
        fields["progress"] = progress
    update(job_id, **fields)


def set_message(job_id: int, message: str, progress: float | None = None) -> None:
    fields: dict[str, Any] = {"message": message}
    if progress is not None:
        fields["progress"] = progress
    update(job_id, **fields)


def set_result(job_id: int, result: dict[str, Any]) -> None:
    update(job_id, result_json=json.dumps(result))


def finish(job_id: int, status: str, message: str = "") -> None:
    fields: dict[str, Any] = {"status": status, "message": message, "finished_at": time.time()}
    if status == DONE:
        fields["progress"] = 1.0
    update(job_id, **fields)


def cancel(job_id: int) -> dict[str, Any] | None:
    """Cancel a queued job outright, or flag a running job so its handler can stop."""
    job = get(job_id)
    if job is None:
        return None
    if job["status"] == QUEUED:
        update(job_id, status=CANCELED, message="canceled before it started", finished_at=time.time())
    elif job["status"] == RUNNING:
        st = state_of(job)
        st["cancel_requested"] = True
        set_state(job_id, st)
        set_message(job_id, "cancellation requested…")
    return get(job_id)


def _active() -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _next_queued() -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #

async def _tick_once() -> None:
    job = _active()
    if job is None:
        job = _next_queued()
        if job is None:
            return
        update(job["id"], status=RUNNING, started_at=time.time())
        job = get(job["id"])
        logs.info("process", f"job started: {job['kind']}", job_id=job["id"], book_id=job["book_id"])

    # honour a cancellation of the running job
    if state_of(job).get("cancel_requested"):
        finish(job["id"], CANCELED, "canceled")
        logs.info("process", "job canceled", job_id=job["id"], kind=job["kind"])
        return

    handler = HANDLERS.get(job["kind"])
    if handler is None:
        finish(job["id"], ERROR, f"no handler registered for kind '{job['kind']}'")
        logs.error("process", "job has no handler", job_id=job["id"], kind=job["kind"])
        return

    try:
        status, message = await handler.tick(job)
    except Exception as exc:  # noqa: BLE001 — a handler bug must not kill the worker
        logs.error("process", f"job {job['id']} ({job['kind']}) tick raised: {exc}", job_id=job["id"])
        finish(job["id"], ERROR, f"internal error: {exc}")
        return

    if status == DONE:
        finish(job["id"], DONE, message or "done")
        logs.info("process", f"job done: {job['kind']} — {message}", job_id=job["id"])
    elif status == ERROR:
        finish(job["id"], ERROR, message or "failed")
        logs.error("process", f"job failed: {job['kind']} — {message}", job_id=job["id"])
    # RUNNING: the handler has already recorded stage/message/state


async def run_worker() -> None:
    """The background loop. Launched once at app startup; runs for the container's life."""
    logs.info("boot", f"job worker started (poll {POLL_SECONDS:.0f}s)", handlers=sorted(HANDLERS))
    while True:
        try:
            await _tick_once()
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logs.error("process", f"job worker loop error: {exc}")
        await asyncio.sleep(POLL_SECONDS)
