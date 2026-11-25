import os

import pytest
from fastapi.testclient import TestClient

from dockllama import db, paths
from dockllama.api import auth as auth_api


os.environ["DOCKLLAMA_NO_EVENT_WATCH"] = "1"  # tests don't listen to the real Docker


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "dockllama.db")
    monkeypatch.setattr(paths, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(paths, "USER_BACKUP_DIR", tmp_path / "user-backups")
    auth_api.throttle._state.clear()
    return tmp_path


@pytest.fixture
def conn(data_dir):
    db.migrate()
    with db.session() as c:
        yield c


@pytest.fixture
def client(data_dir):
    from dockllama.app import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c
