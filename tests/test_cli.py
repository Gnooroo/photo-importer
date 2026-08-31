from unittest.mock import patch

import pytest

from photo_importer import cli
from photo_importer.importer import ImportSummary
from photo_importer.migrate import CopySummary
from photo_importer.one_shot import OneShotResult


def _fake_summary(imported=0, skipped=0, scanned=None):
    return ImportSummary(scanned=scanned if scanned is not None else imported + skipped, imported=imported, skipped_duplicate=skipped)


def test_import_output_uses_new_wording(tmp_path, capsys):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.resolve_source", return_value="src"), \
         patch("photo_importer.cli.run_import", return_value=_fake_summary(imported=0, skipped=983)):
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.source = "auto"
        args = cli._build_parser().parse_args(["import"])
        cli._cmd_import(args)

    out = capsys.readouterr().out
    assert "New: 0" in out
    assert "Already imported (skipped): 983" in out
    assert "Imported: 0" not in out  # old, ambiguous wording must be gone


def test_sync_output_includes_synced_count(tmp_path, capsys):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted"), \
         patch("photo_importer.cli.nas_sync.sync_with_heartbeat", return_value=True), \
         patch("photo_importer.cli.nas_sync.count_synced", return_value=(2511, 2511)):
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.nas_mount_point = "/Volumes/nas"
        mock_load.return_value.nas_remote_subpath = "Photos"
        args = cli._build_parser().parse_args(["sync"])
        cli._cmd_sync(args)

    out = capsys.readouterr().out
    assert "Synced to NAS: 2511/2511 files" in out


def test_sync_attempts_auto_mount_before_requiring(tmp_path):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted") as mock_ensure_mounted, \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.nas_sync.sync_with_heartbeat", return_value=True), \
         patch("photo_importer.cli.nas_sync.count_synced", return_value=(0, 0)):
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.nas_mount_point = "/Volumes/nas"
        mock_load.return_value.nas_remote_subpath = "Photos"
        mock_load.return_value.nas_smb_url = "smb://nas/share"
        args = cli._build_parser().parse_args(["sync"])
        cli._cmd_sync(args)

    mock_ensure_mounted.assert_called_once_with("/Volumes/nas", "smb://nas/share")
    mock_require_mounted.assert_called_once_with("/Volumes/nas")


def test_one_shot_output_includes_synced_count_when_nas_available(tmp_path, capsys):
    result = OneShotResult(
        summary=_fake_summary(imported=0, skipped=983),
        nas_available=True,
        sync_ok=True,
    )
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.resolve_source", return_value="src"), \
         patch("photo_importer.cli.one_shot.run_one_shot", return_value=result), \
         patch("photo_importer.cli.nas_sync.count_synced", return_value=(2511, 2511)):
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.source = "auto"
        mock_load.return_value.nas_mount_point = "/Volumes/nas"
        mock_load.return_value.nas_remote_subpath = "Photos"
        args = cli._build_parser().parse_args([])
        cli._cmd_one_shot(args)

    out = capsys.readouterr().out
    assert "New: 0" in out
    assert "Already imported (skipped): 983" in out
    assert "Synced to NAS: 2511/2511 files" in out


def test_migrate_dest_override_skips_nas_mount_check(tmp_path):
    dest = tmp_path / "archive"
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary()) as mock_run_copy:
        mock_load.return_value.migrate_source_path = None
        mock_load.return_value.extension_set = {".jpg"}
        args = cli._build_parser().parse_args(
            ["migrate", "copy", "--source", str(tmp_path / "src"), "--dest", str(dest)]
        )
        cli._cmd_migrate_copy(args)

    mock_require_mounted.assert_not_called()
    dest_root_arg = mock_run_copy.call_args[0][1]
    assert dest_root_arg == str(dest)


def test_migrate_without_dest_still_requires_nas_mount(tmp_path):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted") as mock_ensure_mounted, \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary()):
        mock_load.return_value.migrate_source_path = None
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/nas"
        mock_load.return_value.nas_remote_subpath = "Photos"
        mock_load.return_value.nas_smb_url = "smb://nas/share"
        args = cli._build_parser().parse_args(["migrate", "copy", "--source", str(tmp_path / "src")])
        cli._cmd_migrate_copy(args)

    mock_ensure_mounted.assert_called_once_with("/Volumes/nas", "smb://nas/share")
    mock_require_mounted.assert_called_once_with("/Volumes/nas")


def test_backup_sync_attempts_auto_mount_before_requiring(tmp_path):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted") as mock_ensure_mounted, \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary()):
        mock_load.return_value.backup_source_paths = ["/Volumes/personal_folder/Photos/MobileBackup/iPhone"]
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        mock_load.return_value.nas_smb_url = "smb://personal_folder/share"
        args = cli._build_parser().parse_args(["backup-sync"])
        cli._cmd_backup_sync(args)

    mock_ensure_mounted.assert_called_once_with("/Volumes/personal_folder", "smb://personal_folder/share")
    mock_require_mounted.assert_called_once_with("/Volumes/personal_folder")


def test_backup_sync_uses_configured_source_and_caches(tmp_path):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary(copied=3, already_present=1)) as mock_run_copy:
        mock_load.return_value.backup_source_paths = ["/Volumes/personal_folder/Photos/MobileBackup/iPhone"]
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        args = cli._build_parser().parse_args(["backup-sync"])
        cli._cmd_backup_sync(args)

    mock_require_mounted.assert_called_once_with("/Volumes/personal_folder")
    call_args = mock_run_copy.call_args
    assert call_args[0][0] == "/Volumes/personal_folder/Photos/MobileBackup/iPhone"
    assert call_args[0][1] == "/Volumes/personal_folder/Photos/Archive"
    assert call_args.kwargs["cache"] is True


def test_backup_sync_dest_override_skips_nas_mount_check(tmp_path):
    dest = tmp_path / "archive"
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.require_mounted") as mock_require_mounted, \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary()) as mock_run_copy:
        mock_load.return_value.backup_source_paths = []
        mock_load.return_value.extension_set = {".jpg"}
        args = cli._build_parser().parse_args(
            ["backup-sync", "--source", str(tmp_path / "src"), "--dest", str(dest)]
        )
        cli._cmd_backup_sync(args)

    mock_require_mounted.assert_not_called()
    assert mock_run_copy.call_args[0][1] == str(dest)


def test_backup_sync_without_source_raises_config_error():
    with patch("photo_importer.cli.load_config") as mock_load:
        mock_load.return_value.backup_source_paths = []
        args = cli._build_parser().parse_args(["backup-sync"])
        with pytest.raises(cli.ConfigError, match="Backup sync source is not set"):
            cli._cmd_backup_sync(args)


def test_backup_sync_output(tmp_path, capsys):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted"), \
         patch(
             "photo_importer.cli.migrate.run_copy",
             return_value=CopySummary(scanned_total=10, copied=3, already_present=7),
         ):
        mock_load.return_value.backup_source_paths = ["/Volumes/personal_folder/Photos/MobileBackup/iPhone"]
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        args = cli._build_parser().parse_args(["backup-sync"])
        cli._cmd_backup_sync(args)

    out = capsys.readouterr().out
    assert "Found in source: 10" in out
    assert "Copied: 3" in out
    assert "Already in archive: 7" in out


def test_backup_sync_multiple_sources_via_repeated_flag(tmp_path):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted"), \
         patch(
             "photo_importer.cli.migrate.run_copy",
             side_effect=[
                 CopySummary(scanned_total=5, copied=2, already_present=3),
                 CopySummary(scanned_total=8, copied=0, already_present=8),
             ],
         ) as mock_run_copy:
        mock_load.return_value.backup_source_paths = []
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        args = cli._build_parser().parse_args(
            ["backup-sync", "--source", "/phones/a", "--source", "/phones/b"]
        )
        cli._cmd_backup_sync(args)

    assert mock_run_copy.call_count == 2
    sources_called = [c[0][0] for c in mock_run_copy.call_args_list]
    assert sources_called == ["/phones/a", "/phones/b"]
    # each source gets its own dedicated cache/label rather than being merged
    labels = [c.kwargs["label"] for c in mock_run_copy.call_args_list]
    assert labels == ["Backup sync (/phones/a)", "Backup sync (/phones/b)"]


def test_backup_sync_multiple_sources_prints_per_source_and_total(tmp_path, capsys):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted"), \
         patch(
             "photo_importer.cli.migrate.run_copy",
             side_effect=[
                 CopySummary(scanned_total=5, copied=2, already_present=3),
                 CopySummary(scanned_total=8, copied=1, already_present=7),
             ],
         ):
        mock_load.return_value.backup_source_paths = ["/phones/a", "/phones/b"]
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        args = cli._build_parser().parse_args(["backup-sync"])
        cli._cmd_backup_sync(args)

    out = capsys.readouterr().out
    assert "== /phones/a ==" in out
    assert "== /phones/b ==" in out
    assert "Total across 2 sources -- found: 13, copied: 3, already in archive: 10, failed: 0" in out


def test_backup_sync_single_source_omits_per_source_headers(tmp_path, capsys):
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.nas_sync.ensure_mounted"), \
         patch("photo_importer.cli.nas_sync.require_mounted"), \
         patch("photo_importer.cli.migrate.run_copy", return_value=CopySummary(scanned_total=1, copied=1)):
        mock_load.return_value.backup_source_paths = ["/phones/a"]
        mock_load.return_value.extension_set = {".jpg"}
        mock_load.return_value.nas_mount_point = "/Volumes/personal_folder"
        mock_load.return_value.nas_remote_subpath = "Photos/Archive"
        args = cli._build_parser().parse_args(["backup-sync"])
        cli._cmd_backup_sync(args)

    out = capsys.readouterr().out
    assert "==" not in out
    assert "Total across" not in out


def test_one_shot_output_skips_synced_count_when_nas_unavailable(tmp_path, capsys):
    result = OneShotResult(
        summary=_fake_summary(imported=1, skipped=0),
        nas_available=False,
        sync_ok=False,
    )
    with patch("photo_importer.cli.load_config") as mock_load, \
         patch("photo_importer.cli.resolve_source", return_value="src"), \
         patch("photo_importer.cli.one_shot.run_one_shot", return_value=result), \
         patch("photo_importer.cli.nas_sync.count_synced") as mock_count:
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.source = "auto"
        mock_load.return_value.nas_mount_point = None
        args = cli._build_parser().parse_args([])
        cli._cmd_one_shot(args)

    mock_count.assert_not_called()
    out = capsys.readouterr().out
    assert "NAS sync: skipped (NAS not available)" in out
