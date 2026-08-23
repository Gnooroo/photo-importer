import os

from photo_importer import config


def test_default_locations_includes_appdata_when_set(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")

    locations = config._default_config_locations()

    assert any("AppData" in str(p) and "photo-importer" in str(p) for p in locations)


def test_default_locations_omits_appdata_when_unset(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)

    locations = config._default_config_locations()

    assert not any("AppData" in str(p) for p in locations)
    # cwd config.yaml and the ~/.config fallback are always present
    assert len(locations) == 2


def test_load_config_finds_appdata_location(tmp_path, monkeypatch):
    appdata_dir = tmp_path / "AppData" / "Roaming"
    config_dir = appdata_dir / "photo-importer"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("local_root: /some/path\n")

    monkeypatch.setenv("APPDATA", str(appdata_dir))
    monkeypatch.chdir(tmp_path)  # so a stray ./config.yaml can't shadow this

    loaded = config.load_config()

    assert loaded.local_root == "/some/path"
