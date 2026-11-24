"""Command line: `dockllama` (run the server) and `dockllama reset-password`.

Inside the container:  docker exec -it dockllama dockllama reset-password
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import __version__, auth, db, paths


def _serve(_args) -> int:
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=paths.PORT, proxy_headers=False, log_level="info")
    return 0


def _reset_password(args) -> int:
    db.migrate()
    password = args.password
    if password is None:
        if not sys.stdin.isatty():
            print("Run this with -it so you can type the new password:", file=sys.stderr)
            print("  docker exec -it dockllama dockllama reset-password", file=sys.stderr)
            return 2
        password = getpass.getpass("New password: ")
        if getpass.getpass("Type it again: ") != password:
            print("The two passwords didn't match. Nothing changed.", file=sys.stderr)
            return 1
    problem = auth.check_new_password(password)
    if problem:
        print(problem + " Nothing changed.", file=sys.stderr)
        return 1
    with db.session() as conn, db.transaction(conn):
        auth.set_password(conn, password)
        from . import settings

        settings.set(conn, "auth.login_required", True)
    print("Password changed. Everyone has been signed out; sign in with the new password.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockllama", description=f"DockLlama {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web app (default)")
    reset = sub.add_parser("reset-password", help="set a new sign-in password")
    reset.add_argument("--password", help=argparse.SUPPRESS)  # for tests and scripted resets
    args = parser.parse_args(argv)
    if args.command == "reset-password":
        return _reset_password(args)
    return _serve(args)
