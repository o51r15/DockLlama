from dockllama import auth, db, settings


def _setup(client, password="hunter2hunter2", tz="America/New_York"):
    return client.post("/api/auth/setup", json={"password": password, "timezone": tz})


def test_fresh_install_sends_everything_to_setup(client):
    for page in ("/", "/login"):
        r = client.get(page, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "setup"
    assert client.get("/setup").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "setup_needed": True, "login_required": True, "signed_in": False,
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
    assert client.get("/", follow_redirects=False).status_code == 200
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
    assert client.get("/", follow_redirects=False).status_code == 200
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


def test_skip_password_opens_everything(client):
    r = client.post("/api/auth/setup", json={"skip_password": True, "timezone": "UTC"})
    assert r.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/auth/status").json() == {
        "setup_needed": False, "login_required": False, "signed_in": True,
    }


def test_change_password_keeps_me_signed_in_and_signs_out_others(client):
    _setup(client)
    other = client.cookies.get(auth.COOKIE_NAME)
    r = client.post("/api/auth/password", json={"current_password": "hunter2hunter2", "new_password": "a-new-password"})
    assert r.status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    with db.session() as conn:
        assert not auth.session_valid(conn, other)


def test_change_password_requires_current(client):
    _setup(client)
    r = client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "a-new-password"})
    assert r.status_code == 401
