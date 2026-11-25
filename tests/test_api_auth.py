from dockllama import auth, db, settings


def _setup(client, password="hunter2hunter2", tz="America/New_York"):
    return client.post("/api/auth/setup", json={"password": password, "timezone": tz})


def test_fresh_install_sends_everything_to_setup(client):
    for page in ("/", "/login"):
        r = client.get(page, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "setup"
    assert client.get("/setup").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "setup_needed": True, "login_required": True, "login_off_by_compose": False, "signed_in": False,
    }


def test_public_endpoints_work_without_session(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/alpine-3.17.5.min.js").status_code == 200


def test_setup_signs_in_and_saves_timezone(client):
    r = _setup(client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    assert client.get("/settings", follow_redirects=False).status_code == 200
    assert client.get("/setup", follow_redirects=False).headers["location"] == "./"
    assert client.get("/login", follow_redirects=False).headers["location"] == "./"
    with db.session() as conn:
        assert settings.get(conn, "general.timezone") == "America/New_York"


def test_bad_timezone_from_browser_falls_back_to_utc(client):
    assert _setup(client, tz="Not/AZone").status_code == 200
    with db.session() as conn:
        assert settings.get(conn, "general.timezone") == "UTC"


def test_setup_only_once(client):
    _setup(client)
    client.cookies.clear()
    r = _setup(client, password="attacker-password")
    assert r.status_code == 409
    with db.session() as conn:
        assert auth.verify_password("hunter2hunter2", settings.get(conn, "auth.password_hash"))


def test_setup_rejects_short_password(client):
    r = _setup(client, password="short")
    assert r.status_code == 400 and "8 characters" in r.json()["detail"]
    assert client.get("/api/auth/status").json()["setup_needed"] is True


def test_signed_out_user_is_sent_to_login_and_api_is_closed(client):
    _setup(client)
    client.cookies.clear()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"
    assert client.post("/api/auth/password", json={"current_password": "x", "new_password": "y" * 9}).status_code == 401
    assert client.get("/api/anything").status_code == 401


def test_login_logout(client):
    _setup(client)
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "wrong-one"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "hunter2hunter2"}).status_code == 200
    assert client.get("/settings", follow_redirects=False).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()  # TestClient keeps the deleted cookie's old value otherwise
    assert client.get("/", follow_redirects=False).headers["location"] == "login"


def test_login_throttled_after_five_failures(client):
    _setup(client)
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope-nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": "hunter2hunter2"})
    assert r.status_code == 429 and "Try again in" in r.json()["detail"]


def test_the_page_cant_turn_sign_in_off(client):
    r = client.post("/api/auth/setup", json={"skip_password": True, "timezone": "UTC"})
    assert r.status_code == 400 and "DOCKLLAMA_LOGIN=off" in r.json()["detail"]
    assert client.get("/api/auth/status").json()["setup_needed"] is True


def test_sign_in_off_in_the_compose_file(client, monkeypatch):
    monkeypatch.setenv("DOCKLLAMA_LOGIN", "off")
    assert client.get("/api/auth/status").json() == {
        "setup_needed": False, "login_required": False, "login_off_by_compose": True, "signed_in": True,
    }
    assert client.get("/settings", follow_redirects=False).status_code == 200
    assert client.get("/api/fixing").status_code == 200
    # the setup guide saves the browser's time zone once, since there was no password step
    assert client.post("/api/setup/timezone", json={"timezone": "America/New_York"}).json()["timezone"] == "America/New_York"
    assert client.post("/api/setup/timezone", json={"timezone": "Europe/Paris"}).json()["timezone"] == "America/New_York"


def test_an_install_that_skipped_the_password_is_asked_for_one(client):
    """Before this change the page could turn sign-in off; such installs now need a password
    (or DOCKLLAMA_LOGIN=off in the compose file)."""
    from dockllama import db, settings
    db.migrate()
    with db.session() as conn:
        settings.set(conn, "auth.login_required", False)
    assert client.get("/api/auth/status").json()["setup_needed"] is True
    assert client.get("/api/fixing").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"].endswith("setup")


def test_change_password_keeps_me_signed_in_and_signs_out_others(client):
    _setup(client)
    other = client.cookies.get(auth.COOKIE_NAME)
    r = client.post("/api/auth/password", json={"current_password": "hunter2hunter2", "new_password": "a-new-password"})
    assert r.status_code == 200
    assert client.get("/settings", follow_redirects=False).status_code == 200
    with db.session() as conn:
        assert not auth.session_valid(conn, other)


def test_change_password_requires_current(client):
    _setup(client)
    r = client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "a-new-password"})
    assert r.status_code == 400 and "current password" in r.json()["detail"]
    assert client.get("/settings", follow_redirects=False).status_code == 200  # still signed in


def test_home_goes_to_the_setup_guide_until_it_is_finished(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "welcome"
    assert client.get("/welcome", follow_redirects=False).status_code == 200
    st = client.get("/api/setup/state").json()
    assert st["complete"] is False and st["ai_connected"] is False and st["per_check_source"] == "no_ai"
    assert client.post("/api/setup/finish", json={}).json()["ok"]
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/welcome", follow_redirects=False).status_code == 200  # can be re-run from Settings


def test_setup_guide_needs_sign_in(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    client.post("/api/auth/logout")
    r = client.get("/welcome", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"
    assert client.post("/api/setup/finish", json={}).status_code == 401
