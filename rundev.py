#!/usr/bin/env python
"""Run Lore Forge locally against a `.devdata/` folder beside this file.

    python rundev.py            # http://127.0.0.1:8891
    PORT=9000 python rundev.py

Points DB/logs/output at `.devdata/` (gitignored) so a dev run never touches the
server's appdata, and talks to the real Ollama on the LAN. Override any of the env
vars below to point elsewhere.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV = HERE / ".devdata"

os.environ.setdefault("DB_DIR", str(DEV / "db"))
os.environ.setdefault("LOG_DIR", str(DEV / "logs"))
os.environ.setdefault("LORE_BUILDS_ROOT", str(DEV / "lore-builds"))
os.environ.setdefault("OLLAMA_URL", "http://192.168.1.32:11434")
os.environ.setdefault("EMBED_MODEL", "nomic-embed-text")

for sub in ("db", "logs", "lore-builds"):
    (DEV / sub).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(HERE / "backend"))

import uvicorn  # noqa: E402  — after sys.path is set

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8891")),
        reload=bool(os.environ.get("RELOAD")),
        log_level="info",
    )
