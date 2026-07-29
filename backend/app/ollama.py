"""Ollama client — embeddings now, extraction later.

Bounded on purpose: this module speaks HTTP to Ollama and nothing else. No prompt
construction lives here beyond the embedding call, because at 0.1.x the model's only
job is to turn text into vectors. `generate()` is present for L2 and deliberately thin.

Ollama runs at `.32` on its own br0 macvlan — a different box from UR1's ComfyUI (.33)
precisely so embedding a book cannot contend with a LoRA build for the 3090.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import logs
from .config import EMBED_MODEL, OLLAMA_MODEL, OLLAMA_URL

# Embedding a batch is fast, but a cold model has to load off disk first.
EMBED_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
STATUS_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
GENERATE_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


async def status() -> dict[str, Any]:
    """Reachability + what's installed. Drives the sidebar status block."""
    out: dict[str, Any] = {
        "url": OLLAMA_URL,
        "reachable": False,
        "models": [],
        "embed_model": EMBED_MODEL,
        "embed_model_present": False,
        "generate_model": OLLAMA_MODEL,
        "generate_model_present": False,
        "error": "",
    }
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        logs.verbose("integration", f"ollama unreachable: {exc}", url=OLLAMA_URL)
        return out

    models = [
        {"name": m.get("name", ""), "size": m.get("size", 0),
         "parameters": (m.get("details") or {}).get("parameter_size", "")}
        for m in data.get("models", [])
    ]
    names = {m["name"] for m in models}
    # Ollama reports "nomic-embed-text:latest" for a plain "nomic-embed-text" pull.
    def _present(want: str) -> bool:
        return want in names or f"{want}:latest" in names

    out.update({
        "reachable": True,
        "models": sorted(models, key=lambda m: m["name"]),
        "embed_model_present": _present(EMBED_MODEL),
        "generate_model_present": _present(OLLAMA_MODEL),
    })
    return out


async def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed a batch. Raises on failure — the caller (a job handler) records the error
    on the job row, which is where the user can actually see it."""
    if not texts:
        return []
    model = model or EMBED_MODEL
    payload = {"model": model, "input": texts}
    async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
        r = await client.post(f"{OLLAMA_URL}/api/embed", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"ollama /api/embed {r.status_code}: {r.text[:300]}")
        data = r.json()

    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError(
            f"ollama returned {len(vectors) if isinstance(vectors, list) else 'no'} "
            f"embeddings for {len(texts)} inputs"
        )
    logs.verbose("integration", "embedded a batch", model=model, count=len(texts),
                 dims=len(vectors[0]) if vectors else 0)
    return vectors


async def embed_one(text: str, model: str | None = None) -> list[float]:
    return (await embed([text], model=model))[0]


async def probe_dims(model: str | None = None) -> int:
    """Ask the model its vector width by embedding a token. Discovered rather than
    configured, because a wrong hard-coded dimension fails as bad retrieval instead
    of as an error."""
    return len(await embed_one("dimension probe", model=model))


async def generate(prompt: str, system: str = "", model: str | None = None,
                   options: dict[str, Any] | None = None) -> str:
    """Single-shot completion. Unused at 0.1.x — the L2 extraction passes are its
    first caller. Kept minimal so the prompt-building stays with the phase that
    owns it (docs/design.md 1: the model extracts; it never arbitrates)."""
    body: dict[str, Any] = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
    }
    if system:
        body["system"] = system
    if options:
        body["options"] = options
    async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as client:
        r = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()
