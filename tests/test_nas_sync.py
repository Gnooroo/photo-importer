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


def test_sync_command_is_additive_never_deletes(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    captured_cmd = {}

    def fake_run(cmd, stderr=None, text=None):
        captured_cmd["cmd"] = cmd

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        nas_sync.sync(str(local_root), str(mount_point), remote_subpath="Shared_Photos")

    cmd = captured_cmd["cmd"]
    assert cmd[0] == "rsync"
    assert "--delete" not in cmd
    assert "--delete-after" not in cmd
    assert "--delete-before" not in cmd
    assert "--ignore-existing" in cmd
    assert "--exclude=.*" in cmd
    assert "--progress" in cmd
    assert cmd[-1] == str(mount_point / "Shared_Photos")
    assert cmd[-2] == str(local_root) + "/"


def test_sync_verbose_false_omits_progress_flags(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    captured_cmd = {}

    def fake_run(cmd, stderr=None, text=None):
        captured_cmd["cmd"] = cmd

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        nas_sync.sync(str(local_root), str(mount_point), verbose=False)

    cmd = captured_cmd["cmd"]
    assert "-v" not in cmd
    assert "--progress" not in cmd
    assert "--ignore-existing" in cmd  # safety flags stay regardless of verbosity


def test_sync_uses_progress2_flag_on_non_macos(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    captured_cmd = {}

    def fake_run(cmd, stderr=None, text=None):
        captured_cmd["cmd"] = cmd

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("platform.system", return_value="Linux"):
        nas_sync.sync(str(local_root), str(mount_point))

    cmd = captured_cmd["cmd"]
    assert "--info=progress2" in cmd
    assert "--progress" not in cmd


def test_sync_wraps_rsync_failure_as_nas_sync_error(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    def fake_run(cmd, stderr=None, text=None):
        class Result:
            returncode = 1
            stderr = "rsync: some failure\n"

        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(nas_sync.NasSyncError, match="rsync failed"):
            nas_sync.sync(str(local_root), str(mount_point))


def test_sync_wraps_missing_rsync_binary_as_nas_sync_error(tmp_path):
    mount_point = tmp_path / "nas"
    mount_point.mkdir()
    local_root = tmp_path / "library"
    local_root.mkdir()

    with patch("os.path.ismount", return_value=True), \
         patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
        with pytest.raises(nas_sync.NasSyncError, match="rsync is not installed"):
            nas_sync.sync(str(local_root), str(mount_point))


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
