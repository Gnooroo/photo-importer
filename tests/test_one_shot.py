from unittest.mock import patch

from photo_importer import one_shot
from photo_importer.importer import ImportSummary


def _fake_summary():
    return ImportSummary(scanned=1, imported=1, skipped_duplicate=0)


def test_dry_run_never_touches_nas(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()) as mock_import, \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted") as mock_ensure, \
         patch("photo_importer.one_shot.nas_sync.sync") as mock_sync:
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=True
        )

    mock_import.assert_called_once()
    assert mock_import.call_args.kwargs["dry_run"] is True
    mock_ensure.assert_not_called()
    mock_sync.assert_not_called()
    assert result.nas_available is False
    assert result.background_sync_ok is False
    assert result.catchup_sync_ok is False


def test_background_sync_failure_does_not_abort_import(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()), \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync", side_effect=one_shot.nas_sync.NasSyncError("boom")):
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )

    assert result.summary.imported == 1
    assert result.nas_available is True
    assert result.background_sync_ok is False
    assert result.catchup_sync_ok is False


def test_catchup_sync_runs_after_import_completes(tmp_path):
    call_order = []

    def fake_import(*a, **k):
        call_order.append("import")
        return _fake_summary()

    def fake_sync(*a, **k):
        call_order.append("sync")

    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", side_effect=fake_import), \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync", side_effect=fake_sync):
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )

    assert result.catchup_sync_ok is True
    # background sync starts before import (first call), catch-up sync is the last call
    assert call_order[-1] == "sync"
    assert "import" in call_order


def test_nas_unmounted_prompts_and_continues_import_only(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()) as mock_import, \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=False) as mock_ensure, \
         patch("photo_importer.one_shot.nas_sync.sync") as mock_sync, \
         patch("builtins.input", return_value="") as mock_input:
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )

    mock_ensure.assert_called_once_with("/Volumes/nas", "smb://host/share")
    mock_input.assert_called_once()
    mock_sync.assert_not_called()
    mock_import.assert_called_once()
    assert result.nas_available is False
    assert result.background_sync_ok is False
    assert result.catchup_sync_ok is False


def test_non_camera_source_warns_and_prompts_before_anything_else(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=False) as mock_check, \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()) as mock_import, \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync"), \
         patch("builtins.input", return_value="") as mock_input:
        one_shot.run_one_shot(
            "/Volumes/RANDOM_USB", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos",
            "smb://host/share", dry_run=False,
        )

    mock_check.assert_called_once_with("/Volumes/RANDOM_USB")
    mock_input.assert_called_once()
    mock_import.assert_called_once()


def test_non_camera_source_warning_also_fires_on_dry_run(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=False), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()), \
         patch("builtins.input", return_value="") as mock_input:
        one_shot.run_one_shot(
            "/Volumes/RANDOM_USB", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos",
            "smb://host/share", dry_run=True,
        )

    mock_input.assert_called_once()


def test_camera_source_does_not_prompt_for_that_check(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()), \
         patch("builtins.input") as mock_input:
        one_shot.run_one_shot(
            "/Volumes/NIKON_ZF", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos",
            "smb://host/share", dry_run=True,
        )

    mock_input.assert_not_called()
