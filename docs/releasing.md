# Releasing 2.0

Not done yet: this is the checklist for when v2 is ready to replace v1. Publishing a release moves
`:latest` to v2, so anyone running v1 with `:latest` gets v2 on their next pull (with a new data
volume layout and none of v1's `config.yaml`).

1. **Test on real machines first.**
   - On Thor: Settings → AI → Test all, compared with `ollama_bench3.py --apps dockllama`
     (the scoring was checked against 233 stored answers; the new prompt still needs a live run).
   - On a Raspberry Pi (arm64): the image starts, lists containers, runs a check.
   - On greenmachine with DockLlama Remote on the Optiplex: containers from both show up.
   - **Fresh-machine test** (plan phase 12): a machine with only Docker, following only the README,
     gets alerts working in under 10 minutes.
2. **Switch the image tag** from `v2-dev` to `latest` in `docker-compose.yml`, `README.md`,
   `docs/other-machines.md` and `REMOTE_IMAGE` in `dockllama/api/containers.py`; set `__version__`
   in `dockllama/__init__.py` to `2.0.0`.
3. **Merge `v2` into `main`** (v1 stays under the `v1-final` tag).
4. **Publish a GitHub release** `v2.0.0` from `main`. CI builds `latest`, `2.0.0` and `2.0`.
5. Update the GitHub wiki from `docs/` (or point the wiki at it).
