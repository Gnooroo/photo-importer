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

    def fake_run(cmd, check):
        captured_cmd["cmd"] = cmd

        class Result:
            returncode = 0

        return Result()

    with patch("os.path.ismount", return_value=True), patch("subprocess.run", side_effect=fake_run):
        nas_sync.sync(str(local_root), str(mount_point), remote_subpath="Shared_Photos")

    cmd = captured_cmd["cmd"]
    assert cmd[0] == "rsync"
    assert "--delete" not in cmd
    assert "--delete-after" not in cmd
    assert "--delete-before" not in cmd
    assert "--ignore-existing" in cmd
    assert cmd[-1] == str(mount_point / "Shared_Photos")
    assert cmd[-2] == str(local_root) + "/"
