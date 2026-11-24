# Upgrading from v1

v2 is a rebuild: settings live in DockLlama's database and are changed from the web page, not in
`config.yaml`. Your v1 setup can be brought over.

1. Start v2 with the [compose file](../README.md#install) (a new data volume; v1's isn't reused) and
   go through the setup guide, or skip ahead.
2. Bring over v1's setup, either way:
   - **Settings → Account → Bring over a DockLlama v1 setup**: pick your old `config.yaml`, check the
     preview, **Import**.
   - Or, to include the notes and alert URLs you saved in v1's web page (they're in v1's database):
     ```bash
     docker cp /path/to/config.yaml dockllama:/tmp/config.yaml
     docker cp /path/to/old/dockllama.db dockllama:/tmp/old.db
     docker exec -it dockllama dockllama import-v1 /tmp/config.yaml --db /tmp/old.db --dry-run
     docker exec -it dockllama dockllama import-v1 /tmp/config.yaml --db /tmp/old.db
     ```

What comes across: which containers are watched, notes ("context prompts"), ignore and known
patterns, model overrides, restart groups (`dependency_groups` in order, otherwise `compose_group`),
alert URLs (Apprise URLs work unchanged), the check interval, restart limits and the digest time.

What doesn't: v1's Ollama address and model (connect it in Settings → AI, which checks it and suggests
a model), containers on other Docker hosts (add those machines in Settings → Machines), and history.

v2 starts in **Watch only**. If v1 was restarting containers (`dry_run: false`), those containers are
allowed to restart, so switching to Auto-restart restores what v1 did.

You no longer need `--group-add`, a mounted `config.yaml`, or `TZ`.
