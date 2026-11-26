# Releasing

`v2` is the default branch. Every push to it builds `ghcr.io/o51r15/dockllama:v2-dev` (and
`v2-<short sha>`) for testing. A published GitHub release builds `latest` plus the version tags.

1. Make sure the last push to `v2` built cleanly (Actions) and the tests pass (`python -m pytest`,
   `ruff check .`).
2. Set `__version__` in `dockllama/__init__.py` to the new version (for example `2.0.1`) and push.
3. Publish a GitHub release from `v2` with the tag `v<version>` (for example `v2.0.1`). CI builds
   `latest`, `<version>` and `<major>.<minor>` for `linux/amd64` and `linux/arm64`.
4. DockLlama Remotes on other machines show "older than DockLlama" on the Machines page until they're
   updated (`docker compose pull && docker compose up -d` on each).
5. Refresh the [wiki](https://github.com/o51r15/DockLlama/wiki) from `docs/`:
   `git clone https://github.com/o51r15/DockLlama.wiki.git ../DockLlama.wiki`, then
   `python docs/make_wiki.py ../DockLlama.wiki`, commit and push the wiki.

**Database migrations ship in a release straight away.** `v2-dev` and `latest` share data volumes in
practice (people switch between them), and a DockLlama refuses to start on a database newer than it
knows. So a commit that adds a migration to `db.py` must be followed by a release before anyone is
told to use `latest` again. 2.0.1 exists because schema v12 reached `v2-dev` after 2.0.0, and a
container recreated on `latest` then stopped with "Database schema v12 is newer than this DockLlama
(v11)".

History: `v2.0.0` (November 2026) was the first release of the rewrite; `v2.0.1` followed the next day to
ship schema v12 (one model for every container). The original version is on the
locked `main` branch and the `v1-final` tag.
