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
    assert result.sync_ok is False


def test_background_sync_failure_does_not_abort_import(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()), \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync", side_effect=one_shot.nas_sync.NasSyncError("boom")), \
         patch("photo_importer.one_shot.nas_sync.count_synced", return_value=(0, 0)):
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )

    assert result.summary.imported == 1
    assert result.nas_available is True
    assert result.sync_ok is False


def test_background_sync_success(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()), \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync"), \
         patch("photo_importer.one_shot.nas_sync.count_synced", return_value=(5, 5)):
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )

    assert result.sync_ok is True


def test_background_loop_runs_multiple_passes_while_import_is_slow(tmp_path):
    """The core fix: sync must run repeatedly while import is still going,
    not just once before/after it -- otherwise a run with little backlog but
    a large new import gets no overlap benefit at all.
    """
    import threading
    import time

    sync_calls = []
    release_import = threading.Event()

    def fake_sync_with_heartbeat(local_root, mount_point, remote_subpath, label=None, workers=1, interval=15):
        sync_calls.append(label)
        return True

    def slow_import(*a, **k):
        # blocks until the releaser thread lets it through, giving the
        # background loop (0.05s interval) time to fire multiple passes
        release_import.wait(timeout=2)
        return _fake_summary()

    def releaser():
        time.sleep(0.15)  # let >=2 background passes fire while import is "still going"
        release_import.set()

    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=True), \
         patch("photo_importer.one_shot.run_import", side_effect=slow_import), \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync_with_heartbeat", side_effect=fake_sync_with_heartbeat), \
         patch("photo_importer.one_shot.nas_sync.count_synced", return_value=(1, 2)), \
         patch("photo_importer.one_shot.BACKGROUND_SYNC_INTERVAL_SECONDS", 0.05):
        releaser_thread = threading.Thread(target=releaser)
        releaser_thread.start()
        result = one_shot.run_one_shot(
            "src", str(tmp_path), {".jpg"}, "/Volumes/nas", "Photos", "smb://host/share", dry_run=False
        )
        releaser_thread.join(timeout=1)

    # at least an initial pass and a final pass -- proves the loop iterates
    # rather than running exactly once up front
    assert len(sync_calls) >= 2
    assert any("final pass" in (label or "") for label in sync_calls)
    assert result.sync_ok is True


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
    assert result.sync_ok is False


def test_non_camera_source_warns_and_prompts_before_anything_else(tmp_path):
    with patch("photo_importer.one_shot.source.looks_like_camera_card", return_value=False) as mock_check, \
         patch("photo_importer.one_shot.run_import", return_value=_fake_summary()) as mock_import, \
         patch("photo_importer.one_shot.nas_sync.ensure_mounted", return_value=True), \
         patch("photo_importer.one_shot.nas_sync.sync"), \
         patch("photo_importer.one_shot.nas_sync.count_synced", return_value=(0, 0)), \
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
