import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Every test gets its own fake $HOME (and no $APPDATA), so anything
    that resolves a per-user config/state directory -- config.py's
    config.yaml search, nas_sync.py's sync-state cache -- never touches the
    real developer machine's actual home directory.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("APPDATA", raising=False)
    yield fake_home
