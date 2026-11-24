"""Filesystem locations. The only environment-driven settings DockLlama has.

Everything a user configures lives in the database; these just say where
things are inside the container.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DOCKLLAMA_DATA", "/app/data"))
DB_PATH = DATA_DIR / "dockllama.db"
BACKUP_DIR = DATA_DIR / "backups"
FRONTEND_DIR = Path(
    os.environ.get("DOCKLLAMA_FRONTEND", Path(__file__).resolve().parent.parent / "frontend")
)
PORT = int(os.environ.get("DOCKLLAMA_PORT", "8556"))
