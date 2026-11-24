"""FastAPI application: startup, the sign-in gate, pages and static files."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, db, paths
from .api import ai as ai_api
from .api import auth as auth_api
from .api import containers as containers_api
from .api import tests as tests_api
from .model_tests import RUNNER

log = logging.getLogger("dockllama")

# Reachable without signing in.
PUBLIC_API = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login"}
PAGES = {"/": "index.html", "/login": "login.html", "/setup": "setup.html", "/settings": "settings.html"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    version = await run_in_threadpool(db.migrate)
    log.info("DockLlama %s started, database schema v%s at %s", __version__, version, paths.DB_PATH)
    await RUNNER.start()
    try:
        yield
    finally:
        await RUNNER.stop()


def _gate(path: str, request: Request) -> str:
    """Decide what an unauthenticated-or-not request gets: 'ok', 'setup', 'login'."""
    with db.session() as conn:
        if not auth_api.auth_configured(conn):
            return "ok" if path == "/setup" else "setup"
        if path == "/setup":
            return "home"  # already set up
        if auth_api.is_signed_in(conn, request):
            return "home" if path == "/login" else "ok"
        return "ok" if path == "/login" else "login"


def create_app() -> FastAPI:
    app = FastAPI(title="DockLlama", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def sign_in_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in PUBLIC_API:
            return await call_next(request)
        if path.startswith("/api/"):
            with_session = await run_in_threadpool(_api_allowed, request)
            if not with_session:
                return JSONResponse({"detail": "Please sign in."}, status_code=401)
            return await call_next(request)
        if path in PAGES:
            decision = await run_in_threadpool(_gate, path, request)
            if decision == "setup":
                return RedirectResponse("setup", status_code=303)
            if decision == "login":
                return RedirectResponse("login", status_code=303)
            if decision == "home":
                return RedirectResponse("./", status_code=303)
        return await call_next(request)

    @app.get("/api/health", include_in_schema=False)
    def health():
        return {"status": "ok", "version": __version__}

    app.include_router(auth_api.router)
    app.include_router(containers_api.router)
    app.include_router(ai_api.router)
    app.include_router(tests_api.router)

    for route, filename in PAGES.items():
        app.add_api_route(route, _page(filename), methods=["GET"], include_in_schema=False)

    app.mount("/static", StaticFiles(directory=paths.FRONTEND_DIR / "static"), name="static")
    return app


def _api_allowed(request: Request) -> bool:
    with db.session() as conn:
        return auth_api.auth_configured(conn) and auth_api.is_signed_in(conn, request)


def _page(filename: str):
    def serve():
        return FileResponse(
            paths.FRONTEND_DIR / filename, headers={"Cache-Control": "no-store"}
        )

    return serve
