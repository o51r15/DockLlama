"""Filesystem locations. The only environment-driven settings DockLlama has.

Everything a user configures lives in the database; these just say where
things are inside the container.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DOCKLLAMA_DATA", "/app/data"))
DB_PATH = DATA_DIR / "dockllama.db"
BACKUP_DIR = DATA_DIR / "backups"  # automatic copies taken before a database upgrade
# Backups the user makes or schedules (Settings → Backup). Map it to a folder on the host
# (./backups:/app/backups) so they survive the container being removed.
USER_BACKUP_DIR = Path(os.environ.get("DOCKLLAMA_BACKUPS", "/app/backups"))
FRONTEND_DIR = Path(
    os.environ.get("DOCKLLAMA_FRONTEND", Path(__file__).resolve().parent.parent / "frontend")
)
PORT = int(os.environ.get("DOCKLLAMA_PORT", "8556"))
