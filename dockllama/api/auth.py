"""Login, logout, first-run password setup and password change."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, db, settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
throttle = auth.LoginThrottle()


def auth_configured(conn) -> bool:
    """True once the user has either set a password or chosen to run without one."""
    return auth.password_is_set(conn) or not settings.get(conn, "auth.login_required")


def is_signed_in(conn, request: Request) -> bool:
    if not settings.get(conn, "auth.login_required"):
        return True
    return auth.session_valid(conn, request.cookies.get(auth.COOKIE_NAME))


def _set_cookie(request: Request, response: Response, token: str) -> None:
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
        secure=https,
        path="/",
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class SetupBody(BaseModel):
    password: str | None = None
    skip_password: bool = False
    timezone: str | None = None


class LoginBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.get("/status")
def status(request: Request):
    with db.session() as conn:
        configured = auth_configured(conn)
        return {
            "setup_needed": not configured,
            "login_required": settings.get(conn, "auth.login_required"),
            "signed_in": configured and is_signed_in(conn, request),
        }


@router.post("/setup")
def first_run_setup(body: SetupBody, request: Request, response: Response):
    tz = body.timezone or "UTC"
    try:
        settings.REGISTRY["general.timezone"].validate(tz)
    except settings.SettingError:
        tz = "UTC"  # a browser reporting an odd zone shouldn't block setup
    if not body.skip_password:
        problem = auth.check_new_password(body.password or "")
        if problem:
            raise HTTPException(400, problem)
    with db.session() as conn, db.transaction(conn):
        if auth_configured(conn):
            raise HTTPException(409, "Setup has already been done. Sign in instead.")
        settings.set(conn, "general.timezone", tz)
        if body.skip_password:
            settings.set(conn, "auth.login_required", False)
            return {"ok": True}
        auth.set_password(conn, body.password)
        settings.set(conn, "auth.login_required", True)
        token = auth.create_session(conn, request.headers.get("user-agent"))
    _set_cookie(request, response, token)
    return {"ok": True}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    addr = _client(request)
    wait = throttle.wait_seconds(addr)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {wait} seconds.")
    with db.session() as conn:
        if not auth_configured(conn):
            raise HTTPException(409, "DockLlama isn't set up yet.")
        stored = settings.get(conn, "auth.password_hash")
    if not auth.verify_password(body.password, stored):
        throttle.failed(addr)
        raise HTTPException(401, "That password isn't right.")
    throttle.succeeded(addr)
    with db.session() as conn:
        token = auth.create_session(conn, request.headers.get("user-agent"))
    _set_cookie(request, response, token)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    with db.session() as conn:
        auth.end_session(conn, request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: ChangePasswordBody, request: Request, response: Response):
    problem = auth.check_new_password(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    with db.session() as conn:
        stored = settings.get(conn, "auth.password_hash")
    if stored and not auth.verify_password(body.current_password, stored):
        # 400, not 401: the page treats 401 as "your session ended" and goes to sign-in.
        raise HTTPException(400, "Your current password isn't right.")
    with db.session() as conn, db.transaction(conn):
        auth.set_password(conn, body.new_password)  # signs out every other session
        settings.set(conn, "auth.login_required", True)
        token = auth.create_session(conn, request.headers.get("user-agent"))
    _set_cookie(request, response, token)
    return {"ok": True}
