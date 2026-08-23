import os
import threading
import time
from unittest.mock import patch

import pytest

from photo_importer import nas_sync


def test_raises_when_mount_point_not_set(tmp_path):
    with pytest.raises(nas_sync.NasSyncError, match="mount_point is not set"):
        nas_sync.sync(str(tmp_path), mount_point=None)


def test_raises_when_not_mounted(tmp_path):
    with patch("os.path.ismount", return_value=False):
        with pytest.raises(nas_sync.NasSyncError, match="not currently mounted"):
            nas_sync.sync(str(tmp_path), mount_point="/Volumes/does_not_exist")


def _fake_run_factory(returncode=0, stderr="", calls=None):
    """calls, if given, collects cmd lists -- but with the --files-from
    file's *contents* read eagerly (while it still exists, before sync()'s
    own cleanup deletes it) and appended as an extra list at the end of cmd,
    so tests can inspect what was listed without racing the cleanup.
    """
    def fake_run(cmd, capture_output=None, text=None):
        if calls is not None:
            files_from = next((a for a in cmd if a.startswith("--files-from=")), None)
            listed = []
            if files_from:
                with open(files_from.split("=", 1)[1]) as f:
                    listed = [line for line in f.read().splitlines() if line]
            calls.append((cmd, listed))

        class Result:
            pass

        Result.returncode = returncode
        Result.stderr = stderr
        return Result()

    return fake_run


def _make_pending_files(local_root, rels_and_sizes):
    """Creates real files under local_root with the given (rel_path, size)
    pairs and nothing at the destination -- i.e. everything is pending.
    """
    for rel, size in rels_and_sizes:
        p = local_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)


def _read_files_from_arg(cmd):
    """Only safe to call from *inside* a subprocess.run mock, before sync()'s
    own cleanup deletes the temp file after the (mocked) process returns."""
    files_from = next(a for a in cmd if a.startswith("--files-from="))
    with open(files_from.split("=", 1)[1]) as f:
        return [line for line in f.read().splitlines() if line]


def test_sync_nothing_pending_runs_no_rsync(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run") as mock_run:
        nas_sync.sync(str(local_root), str(mount_point))

    mock_run.assert_not_called()


def test_sync_command_is_additive_never_deletes_and_is_quiet(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    _make_pending_files(local_root, [("2024/01/01/a.jpg", 100)])

    calls = []
    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run_factory(calls=calls)):
        nas_sync.sync(str(local_root), str(mount_point), remote_subpath="Shared_Photos", workers=1)

    assert len(calls) == 1
    cmd, listed = calls[0]
    assert cmd[0] == "rsync"
    assert "--delete" not in cmd
    assert "--delete-after" not in cmd
    assert "--delete-before" not in cmd
    assert "--ignore-existing" in cmd
    assert "--exclude=.*" in cmd
    # no rsync-level verbosity flags -- rsync's own -v prints a line per
    # already-synced file ("Skip existing '<path>'"), which is pure noise on
    # a large, mostly-synced library; progress instead comes from
    # count_synced(), not from rsync's own chatter.
    assert "-v" not in cmd
    assert "--progress" not in cmd
    assert "--info=progress2" not in cmd
    assert any(a.startswith("--files-from=") for a in cmd)
    assert cmd[-1] == str(mount_point / "Shared_Photos")
    assert cmd[-2] == str(local_root) + "/"
    assert listed == ["2024/01/01/a.jpg"]


def test_sync_partitions_files_across_workers(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    _make_pending_files(local_root, [(f"2024/01/01/f{i}.jpg", 100) for i in range(6)])

    calls = []
    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run_factory(calls=calls)):
        nas_sync.sync(str(local_root), str(mount_point), workers=3)

    assert len(calls) == 3
    all_listed = [rel for _cmd, listed in calls for rel in listed]
    assert sorted(all_listed) == sorted(f"2024/01/01/f{i}.jpg" for i in range(6))
    assert len(set(all_listed)) == 6  # no file listed twice


def test_sync_balances_workers_by_bytes_not_count(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    # one huge file plus several tiny ones -- naive round-robin-by-count
    # would put the huge file alone in one bucket with tiny files piled
    # elsewhere; byte-balancing should spread the tiny files onto the
    # bucket(s) without the huge file.
    _make_pending_files(
        local_root,
        [("2024/01/01/huge.mov", 1_000_000)] + [(f"2024/01/01/tiny{i}.jpg", 100) for i in range(5)],
    )

    calls = []
    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run_factory(calls=calls)):
        nas_sync.sync(str(local_root), str(mount_point), workers=2)

    assert len(calls) == 2
    listed_per_worker = [listed for _cmd, listed in calls]
    huge_worker = next(files for files in listed_per_worker if "2024/01/01/huge.mov" in files)
    other_worker = next(files for files in listed_per_worker if files is not huge_worker)
    assert huge_worker == ["2024/01/01/huge.mov"]
    assert len(other_worker) == 5


def test_sync_temp_files_from_files_are_cleaned_up(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    _make_pending_files(local_root, [("2024/01/01/a.jpg", 100)])

    captured_paths = []

    def fake_run(cmd, capture_output=None, text=None):
        files_from = next(a for a in cmd if a.startswith("--files-from="))
        captured_paths.append(files_from.split("=", 1)[1])

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        nas_sync.sync(str(local_root), str(mount_point))

    assert captured_paths
    for p in captured_paths:
        assert not os.path.exists(p)


def test_sync_one_worker_fails_others_still_run_then_aggregated_error(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    _make_pending_files(local_root, [(f"2024/01/01/f{i}.jpg", 100) for i in range(4)])

    def fake_run(cmd, capture_output=None, text=None):
        files = _read_files_from_arg(cmd)

        class Result:
            pass

        if "2024/01/01/f0.jpg" in files:
            Result.returncode = 1
            Result.stderr = "boom"
        else:
            Result.returncode = 0
            Result.stderr = ""
        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(nas_sync.NasSyncError, match=r"1 of 2 sync workers failed"):
            nas_sync.sync(str(local_root), str(mount_point), workers=2)


def test_sync_wraps_missing_rsync_binary_as_nas_sync_error(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    _make_pending_files(local_root, [("2024/01/01/a.jpg", 100)])

    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
        with pytest.raises(nas_sync.NasSyncError, match="rsync is not installed"):
            nas_sync.sync(str(local_root), str(mount_point))


def test_balanced_chunks_distributes_by_bytes():
    pending = [("huge", 1000), ("a", 100), ("b", 100), ("c", 100)]
    chunks = nas_sync._balanced_chunks(pending, workers=2)

    assert len(chunks) == 2
    assert {"huge"} in (set(chunks[0]), set(chunks[1]))


def test_balanced_chunks_drops_empty_buckets():
    pending = [("a", 100)]
    chunks = nas_sync._balanced_chunks(pending, workers=5)

    assert chunks == [["a"]]


def test_format_bytes():
    assert nas_sync.format_bytes(500) == "500 B"
    assert nas_sync.format_bytes(2048) == "2.00 KB"
    assert nas_sync.format_bytes(5 * 1024 * 1024) == "5.00 MB"
    assert nas_sync.format_bytes(3 * 1024 * 1024 * 1024) == "3.00 GB"


def test_ensure_mounted_already_mounted_skips_open(tmp_path):
    with patch("os.path.ismount", return_value=True), patch("subprocess.run") as mock_run:
        assert nas_sync.ensure_mounted(str(tmp_path), "smb://host/share") is True
    mock_run.assert_not_called()


def test_ensure_mounted_no_smb_url_returns_false_without_open(tmp_path):
    with patch("os.path.ismount", return_value=False), patch("subprocess.run") as mock_run:
        assert nas_sync.ensure_mounted(str(tmp_path), None) is False
    mock_run.assert_not_called()


def test_ensure_mounted_skips_open_on_non_macos(tmp_path):
    with patch("os.path.ismount", return_value=False), \
         patch("subprocess.run") as mock_run, \
         patch("platform.system", return_value="Linux"):
        assert nas_sync.ensure_mounted(str(tmp_path), "smb://host/share") is False
    mock_run.assert_not_called()


def test_ensure_mounted_opens_and_succeeds(tmp_path):
    calls = {"ismount": 0}

    def fake_ismount(_path):
        calls["ismount"] += 1
        return calls["ismount"] >= 3  # mounted on the 3rd poll

    with patch("os.path.ismount", side_effect=fake_ismount), \
         patch("subprocess.run") as mock_run, \
         patch("time.sleep"):
        assert nas_sync.ensure_mounted(str(tmp_path), "smb://host/share", timeout=10) is True

    mock_run.assert_called_once_with(["open", "smb://host/share"], check=False)


def test_ensure_mounted_times_out(tmp_path):
    with patch("os.path.ismount", return_value=False), \
         patch("subprocess.run"), \
         patch("time.sleep"), \
         patch("time.monotonic", side_effect=[0, 1, 2, 100]):
        assert nas_sync.ensure_mounted(str(tmp_path), "smb://host/share", timeout=10) is False


def test_count_synced_all_present(tmp_path):
    local_root = tmp_path / "library"
    nas_root = tmp_path / "nas"
    for rel in ["2024/01/01/a.jpg", "2024/01/02/b.jpg"]:
        p = local_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"same content")
        dp = nas_root / rel
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(b"same content")

    synced, total = nas_sync.count_synced(str(local_root), str(nas_root))

    assert (synced, total) == (2, 2)


def test_count_synced_partial(tmp_path):
    local_root = tmp_path / "library"
    nas_root = tmp_path / "nas"
    (local_root / "2024/01/01").mkdir(parents=True)
    (local_root / "2024/01/01/a.jpg").write_bytes(b"content-a")
    (local_root / "2024/01/01/b.jpg").write_bytes(b"content-b")
    (nas_root / "2024/01/01").mkdir(parents=True)
    (nas_root / "2024/01/01/a.jpg").write_bytes(b"content-a")
    # b.jpg deliberately not present on the NAS side

    synced, total = nas_sync.count_synced(str(local_root), str(nas_root))

    assert (synced, total) == (1, 2)


def test_count_synced_size_mismatch_not_counted(tmp_path):
    local_root = tmp_path / "library"
    nas_root = tmp_path / "nas"
    (local_root / "2024/01/01").mkdir(parents=True)
    (local_root / "2024/01/01/a.jpg").write_bytes(b"full content here")
    (nas_root / "2024/01/01").mkdir(parents=True)
    (nas_root / "2024/01/01/a.jpg").write_bytes(b"short")  # different size

    synced, total = nas_sync.count_synced(str(local_root), str(nas_root))

    assert (synced, total) == (0, 1)


def test_count_synced_ignores_hidden_files(tmp_path):
    local_root = tmp_path / "library"
    (local_root).mkdir(parents=True)
    (local_root / ".photo_importer_index.json").write_bytes(b"{}")

    synced, total = nas_sync.count_synced(str(local_root), str(tmp_path / "nas"))

    assert (synced, total) == (0, 0)


def test_count_synced_with_remote_subpath(tmp_path):
    local_root = tmp_path / "library"
    mount_point = tmp_path / "nas_mount"
    (local_root / "2024/01/01").mkdir(parents=True)
    (local_root / "2024/01/01/a.jpg").write_bytes(b"content")
    dest = mount_point / "Shared_Photos" / "2024/01/01"
    dest.mkdir(parents=True)
    (dest / "a.jpg").write_bytes(b"content")

    synced, total = nas_sync.count_synced(str(local_root), str(mount_point), remote_subpath="Shared_Photos")

    assert (synced, total) == (1, 1)


def test_sync_with_heartbeat_success_returns_true(tmp_path):
    with patch("photo_importer.nas_sync.sync") as mock_sync:
        ok = nas_sync.sync_with_heartbeat(str(tmp_path), "/Volumes/nas", interval=10)

    assert ok is True
    mock_sync.assert_called_once()


def test_sync_with_heartbeat_failure_returns_false_and_reports(tmp_path):
    with patch("photo_importer.nas_sync.sync", side_effect=nas_sync.NasSyncError("boom")), \
         patch("photo_importer.nas_sync.report") as mock_report:
        ok = nas_sync.sync_with_heartbeat(str(tmp_path), "/Volumes/nas", interval=10, label="Test sync")

    assert ok is False
    assert any("Test sync failed" in str(c) for c in mock_report.call_args_list)


def test_sync_with_heartbeat_reports_periodic_progress(tmp_path):
    release = threading.Event()

    def slow_sync(*a, **k):
        release.wait(timeout=2)

    with patch("photo_importer.nas_sync.sync", side_effect=slow_sync), \
         patch("photo_importer.nas_sync.count_synced", return_value=(3, 10)), \
         patch("photo_importer.nas_sync.report") as mock_report:
        holder = {}
        t = threading.Thread(
            target=lambda: holder.update(ok=nas_sync.sync_with_heartbeat(str(tmp_path), "/Volumes/nas", interval=0.05))
        )
        t.start()
        time.sleep(0.15)  # let a couple of heartbeat intervals pass
        release.set()
        t.join(timeout=2)

    assert holder.get("ok") is True
    assert any("3/10 files on NAS" in str(c) for c in mock_report.call_args_list)
