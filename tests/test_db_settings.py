import sqlite3

import pytest

from dockllama import db, paths, settings


def test_fresh_migrate_creates_schema_without_backup(data_dir):
    assert db.migrate() == db.LATEST
    with db.session() as conn:
        assert db.current_version(conn) == db.LATEST
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "sessions", "schema_version"} <= tables
    assert not (data_dir / "backups").exists()


def test_migrate_is_idempotent(data_dir):
    db.migrate()
    assert db.migrate() == db.LATEST


def test_upgrade_takes_backup_first(data_dir, monkeypatch):
    db.migrate()
    with db.session() as conn:
        settings.set(conn, "general.timezone", "America/New_York")

    def _m002(conn):
        conn.execute("CREATE TABLE extra (x INTEGER)")

    before = db.LATEST
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_m002])
    monkeypatch.setattr(db, "LATEST", len(db.MIGRATIONS))
    assert db.migrate() == before + 1
    backups = list((data_dir / "backups").glob(f"dockllama-v{before}-*.db"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert old.execute("SELECT version FROM schema_version").fetchone()[0] == before
    old.close()


def test_failed_migration_rolls_back_completely(data_dir, monkeypatch):
    db.migrate()

    def _bad(conn):
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")

    before = db.LATEST
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_bad])
    monkeypatch.setattr(db, "LATEST", len(db.MIGRATIONS))
    with pytest.raises(RuntimeError):
        db.migrate()
    with db.session() as conn:
        assert db.current_version(conn) == before
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None


def test_newer_schema_is_refused(data_dir):
    db.migrate()
    with db.session() as conn:
        conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate()


def test_settings_defaults_roundtrip_and_validation(conn):
    assert settings.get(conn, "auth.login_required") is True
    assert settings.get(conn, "general.timezone") == "UTC"
    settings.set(conn, "general.timezone", "America/New_York")
    assert settings.get(conn, "general.timezone") == "America/New_York"
    with pytest.raises(settings.SettingError):
        settings.set(conn, "general.timezone", "Mars/Olympus")
    with pytest.raises(settings.SettingError):
        settings.set(conn, "auth.login_required", "yes")
    with pytest.raises(settings.SettingError):
        settings.get(conn, "no.such.key")


def test_export_never_includes_password_hash(conn):
    settings.set(conn, "auth.password_hash", "scrypt$x")
    assert "auth.password_hash" not in settings.get_all(conn)
    assert "auth.password_hash" in settings.get_all(conn, exportable_only=False)


def test_db_path_follows_paths_module(data_dir):
    db.migrate()
    assert paths.DB_PATH.exists()
