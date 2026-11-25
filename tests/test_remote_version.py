"""The Machines page says when a paired DockLlama Remote is older than DockLlama."""

from dockllama import machines


def test_version_order():
    k = machines.version_key
    assert k("2.0.0.dev0") < k("2.0.0") < k("2.0.1") < k("2.1.0") < k("10.0.0")
    assert k("2.1.0rc1") < k("2.1.0") and k("2.1.0b2") < k("2.1.0rc1")
    assert k("garbage") == (0, 0, 0, 0, 0)


def test_remote_is_older(monkeypatch):
    import dockllama
    monkeypatch.setattr(dockllama, "__version__", "2.0.0")
    assert machines.remote_is_older("2.0.0.dev0")
    assert machines.remote_is_older(None)  # Remotes from before versions were reported
    assert not machines.remote_is_older("2.0.0")
    assert not machines.remote_is_older("2.0.1")  # a newer Remote is fine
