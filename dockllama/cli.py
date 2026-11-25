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


def _import_v1(args) -> int:
    from . import importer
    db.migrate()
    try:
        with open(args.config, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print(f"Can't read {args.config}: {exc}", file=sys.stderr)
        return 1
    old, urls = ({}, [])
    if args.db:
        try:
            old, urls = importer.read_old_db(args.db)
        except Exception as exc:
            print(f"Can't read the old database {args.db}: {exc}", file=sys.stderr)
            return 1
    try:
        p = importer.plan(text, old, urls)
    except importer.ImportError_ as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{len(p.containers)} containers, {len(p.alert_urls)} alert destinations, settings: {p.settings}")
    for c in p.containers:
        print(f"  {'watch' if c['monitored'] else 'skip '}  {c['name']}" + (f"  [group {c['restart_group']}]" if c["restart_group"] else "")
              + ("  [has notes]" if c["notes_for_ai"] else ""))
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0
    with db.session() as conn, db.transaction(conn):
        result = importer.apply(conn, p)
    print(f"Imported: {result['containers_added']} new, {result['containers_updated']} updated containers, "
          f"{result['notify_targets_added']} notification destinations.")
    for note in result["notes"]:
        print("- " + note)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockllama", description=f"DockLlama {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web app (default)")
    sub.add_parser("remote", help="run DockLlama Remote, the helper for watching another machine")
    imp = sub.add_parser("import-v1", help="bring over a DockLlama v1 config.yaml (and optionally its database)")
    imp.add_argument("config", help="path to the v1 config.yaml")
    imp.add_argument("--db", help="path to the v1 dockllama.db (notes and alert URLs saved from v1's web page)")
    imp.add_argument("--dry-run", action="store_true", help="show what would be imported, change nothing")
    reset = sub.add_parser("reset-password", help="set a new sign-in password")
    reset.add_argument("--password", help=argparse.SUPPRESS)  # for tests and scripted resets
    args = parser.parse_args(argv)
    if args.command == "reset-password":
        return _reset_password(args)
    if args.command == "import-v1":
        return _import_v1(args)
    if args.command == "remote":
        from .remote import serve
        return serve()
    return _serve(args)
