import time

from dockllama import auth, cli, db, settings


def test_hash_and_verify():
    stored = auth.hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong horse", stored)
    assert auth.hash_password("correct horse") != stored  # salted


def test_verify_rejects_garbage():
    for bad in (None, "", "plain", "scrypt$1$2$3$!!$!!", "bcrypt$a$b$c$d$e"):
        assert not auth.verify_password("x", bad)


def test_new_password_rules():
    assert auth.check_new_password("short")
    assert auth.check_new_password("x" * 2000)
    assert auth.check_new_password("long enough") is None


def test_sessions_and_password_change_signs_everyone_out(conn):
    t1 = auth.create_session(conn)
    assert auth.session_valid(conn, t1)
    assert not auth.session_valid(conn, "not-a-token")
    assert not auth.session_valid(conn, None)
    auth.set_password(conn, "new password!")
    assert not auth.session_valid(conn, t1)


def test_expired_session_rejected(conn):
    token = auth.create_session(conn)
    conn.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
    assert not auth.session_valid(conn, token)


def test_session_stored_as_hash_only(conn):
    token = auth.create_session(conn)
    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()["token_hash"]
    assert token not in stored and len(stored) == 64


def test_throttle_backoff(monkeypatch):
    t = auth.LoginThrottle()
    for _ in range(4):
        t.failed("1.2.3.4")
    assert t.wait_seconds("1.2.3.4") == 0
    t.failed("1.2.3.4")
    assert 29 <= t.wait_seconds("1.2.3.4") <= 30
    assert t.wait_seconds("5.6.7.8") == 0
    t.succeeded("1.2.3.4")
    assert t.wait_seconds("1.2.3.4") == 0


def test_cli_reset_password(data_dir, capsys):
    assert cli.main(["reset-password", "--password", "brand new pw"]) == 0
    with db.session() as conn:
        assert auth.verify_password("brand new pw", settings.get(conn, "auth.password_hash"))
        assert settings.get(conn, "auth.login_required") is True
    assert cli.main(["reset-password", "--password", "short"]) == 1
