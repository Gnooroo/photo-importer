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


def test_load_config_reads_backup_source_paths_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "local_root: /some/path\n"
        "backup:\n"
        "  source_paths:\n"
        "    - /Volumes/personal_folder/Photos/MobileBackup/iPhone\n"
        "    - /Volumes/personal_folder/Photos/MobileBackup/OtherPhone\n"
    )

    loaded = config.load_config()

    assert loaded.backup_source_paths == [
        "/Volumes/personal_folder/Photos/MobileBackup/iPhone",
        "/Volumes/personal_folder/Photos/MobileBackup/OtherPhone",
    ]


def test_load_config_accepts_bare_string_backup_source_path(tmp_path, monkeypatch):
    """A single bare string is accepted (not just a one-item list) -- both
    for a config with just one backup source and for older configs written
    before multiple backup sources were supported.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "local_root: /some/path\n"
        "backup:\n"
        "  source_path: /Volumes/personal_folder/Photos/MobileBackup/iPhone\n"
    )

    loaded = config.load_config()

    assert loaded.backup_source_paths == ["/Volumes/personal_folder/Photos/MobileBackup/iPhone"]


def test_load_config_backup_source_paths_defaults_to_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("local_root: /some/path\n")

    loaded = config.load_config()

    assert loaded.backup_source_paths == []


def test_load_config_finds_appdata_location(tmp_path, monkeypatch):
    appdata_dir = tmp_path / "AppData" / "Roaming"
    config_dir = appdata_dir / "photo-importer"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("local_root: /some/path\n")

    monkeypatch.setenv("APPDATA", str(appdata_dir))
    monkeypatch.chdir(tmp_path)  # so a stray ./config.yaml can't shadow this

    loaded = config.load_config()

    assert loaded.local_root == "/some/path"
