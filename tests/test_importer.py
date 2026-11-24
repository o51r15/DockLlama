import json
import sqlite3

import pytest

from dockllama import cli, db, importer, settings

V1 = """
ollama:
  base_url: "http://192.168.1.125:11434"
  default_model: "gemma4:latest"
monitoring:
  poll_interval_seconds: 300
  log_lines_per_check: 200
  dry_run: false
containers:
  - name: "gluetun"
    enabled: true
    ignore_patterns: ["healthcheck ok"]
    compose_group: "media"
  - name: "qbittorrent"
    enabled: true
    compose_group: "media"
    context_prompt: "Torrent client behind the VPN."
    known_patterns:
      - {pattern: "tracker.*timed out", tag: "[ROUTINE: tracker timeout]"}
  - name: "karakeep_chrome"
    enabled: false
    model_override: "llama3.1:8b"
  - name: "far-away"
    node: "optiplex"
cooldowns:
  initial_minutes: 10
  max_restarts_per_hour: 2
alerts:
  urls: ["ntfys://ntfy.sh/mytopic", "discord://123/abc"]
digest:
  enabled: true
  schedule_cron: "30 7 * * *"
dependency_groups:
  vpn_stack: [gluetun, qbittorrent]
"""


def test_plan():
    p = importer.plan(V1, {"qbittorrent": {"context_prompt": "Saved in the web page: VPN torrent client.",
                                           "known_patterns": None}}, ["discord://123/abc", "pover://u@t"])
    by = {c["name"]: c for c in p.containers}
    assert set(by) == {"gluetun", "qbittorrent", "karakeep_chrome"}  # far-away is on another node
    assert by["qbittorrent"]["notes_for_ai"] == "Saved in the web page: VPN torrent client."  # old DB wins, like v1
    assert by["qbittorrent"]["known_patterns"][0]["tag"] == "[ROUTINE: tracker timeout]"
    assert (by["gluetun"]["restart_group"], by["gluetun"]["restart_order"]) == ("vpn_stack", 0)
    assert (by["qbittorrent"]["restart_group"], by["qbittorrent"]["restart_order"]) == ("vpn_stack", 1)
    assert by["karakeep_chrome"]["monitored"] is False and by["karakeep_chrome"]["model_override"] == "llama3.1:8b"
    assert by["gluetun"]["allow_auto_restart"] and not by["karakeep_chrome"]["allow_auto_restart"]
    assert p.alert_urls == ["ntfys://ntfy.sh/mytopic", "discord://123/abc", "pover://u@t"]
    assert p.settings == {"checks.interval_minutes": 5, "checks.log_lines": 200, "fixing.max_per_hour": 2,
                          "fixing.cooldown_minutes": 10, "summary.enabled": True, "summary.time": "07:30"}
    joined = " ".join(p.notes)
    assert "192.168.1.125" in joined and "optiplex" in joined and "Watch only" in joined


def test_bad_files():
    for text, words in [("not: [valid", "valid YAML"), ("just: text", "containers list"), ("", "containers list")]:
        with pytest.raises(importer.ImportError_, match=words):
            importer.plan(text)


def test_apply_is_repeatable(conn):
    conn.execute("INSERT INTO containers (machine_id, name, first_seen, last_seen) VALUES (1, 'gluetun', 0, 0)")
    p = importer.plan(V1)
    r = importer.apply(conn, p)
    assert (r["containers_added"], r["containers_updated"], r["notify_targets_added"]) == (2, 1, 2)
    r = importer.apply(conn, p)
    assert (r["containers_added"], r["containers_updated"], r["notify_targets_added"]) == (0, 3, 0)
    row = conn.execute("SELECT * FROM containers WHERE name = 'qbittorrent'").fetchone()
    assert row["notes_for_ai"] == "Torrent client behind the VPN." and row["restart_group"] == "vpn_stack"
    assert settings.get(conn, "summary.time") == "07:30" and settings.get(conn, "fixing.mode") == "watch"


def make_v1_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE container_prompts (container TEXT PRIMARY KEY, context_prompt TEXT, examples TEXT, "
                "known_patterns TEXT, base_prompt_override TEXT, updated_at TEXT)")
    patterns = json.dumps([{"pattern": "reconnect", "tag": "[ROUTINE: reconnect]"}])
    con.execute("INSERT INTO container_prompts VALUES ('gluetun', 'The VPN. Reconnects are normal.', '[]', ?, NULL, '')",
                (patterns,))
    con.execute("CREATE TABLE alert_urls (id INTEGER PRIMARY KEY, url TEXT, created_at TEXT)")
    con.execute("INSERT INTO alert_urls (url) VALUES ('tgram://1:abc/2')")
    con.commit()
    con.close()


def test_cli(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(V1)
    old = tmp_path / "old.db"
    make_v1_db(old)
    assert cli.main(["import-v1", str(cfg), "--db", str(old), "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM containers").fetchone()["n"] == 0
    assert cli.main(["import-v1", str(cfg), "--db", str(old)]) == 0
    out = capsys.readouterr().out
    assert "3 new" in out and "3 notification destinations" in out and "192.168.1.125" in out
    with db.session() as conn:
        g = conn.execute("SELECT * FROM containers WHERE name = 'gluetun'").fetchone()
        assert g["notes_for_ai"] == "The VPN. Reconnects are normal." and "reconnect" in g["known_patterns"]
    assert cli.main(["import-v1", str(tmp_path / "missing.yaml")]) == 1


def test_api(client):
    client.post("/api/auth/setup", json={"password": "hunter2hunter2", "timezone": "UTC"})
    pv = client.post("/api/import/v1", json={"config_text": V1}).json()["preview"]
    assert len(pv["containers"]) == 3 and pv["alert_urls"] == ["ntfys://…", "discord://…"]  # tokens not echoed
    res = client.post("/api/import/v1", json={"config_text": V1, "apply": True}).json()["result"]
    assert res["containers_added"] == 3
    assert client.post("/api/import/v1", json={"config_text": "x: 1"}).status_code == 400
    assert json.dumps(client.get("/api/notify/targets").json()).count("Apprise URL") == 2
