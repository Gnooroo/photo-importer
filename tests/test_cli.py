from unittest.mock import patch

from photo_importer import cli
from photo_importer.importer import ImportSummary
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
         patch("photo_importer.cli.nas_sync.sync"), \
         patch("photo_importer.cli.nas_sync.count_synced", return_value=(2511, 2511)):
        mock_load.return_value.local_root = str(tmp_path)
        mock_load.return_value.nas_mount_point = "/Volumes/nas"
        mock_load.return_value.nas_remote_subpath = "Photos"
        args = cli._build_parser().parse_args(["sync"])
        cli._cmd_sync(args)

    out = capsys.readouterr().out
    assert "Synced to NAS: 2511/2511 files" in out


def test_one_shot_output_includes_synced_count_when_nas_available(tmp_path, capsys):
    result = OneShotResult(
        summary=_fake_summary(imported=0, skipped=983),
        nas_available=True,
        background_sync_ok=True,
        catchup_sync_ok=True,
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


def test_one_shot_output_skips_synced_count_when_nas_unavailable(tmp_path, capsys):
    result = OneShotResult(
        summary=_fake_summary(imported=1, skipped=0),
        nas_available=False,
        background_sync_ok=False,
        catchup_sync_ok=False,
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
