import os
from unittest.mock import MagicMock, patch

from photo_importer import source
from photo_importer.source import looks_like_camera_card


def test_true_when_dcim_folder_present(tmp_path):
    (tmp_path / "DCIM").mkdir()
    assert looks_like_camera_card(str(tmp_path)) is True


def test_true_when_dcim_folder_lowercase(tmp_path):
    (tmp_path / "dcim").mkdir()
    assert looks_like_camera_card(str(tmp_path)) is True


def test_false_when_no_dcim_folder(tmp_path):
    (tmp_path / "some_random_file.txt").write_bytes(b"data")
    assert looks_like_camera_card(str(tmp_path)) is False


def test_false_when_dcim_is_a_file_not_a_directory(tmp_path):
    (tmp_path / "DCIM").write_bytes(b"not a directory")
    assert looks_like_camera_card(str(tmp_path)) is False


def test_false_when_path_does_not_exist(tmp_path):
    assert looks_like_camera_card(str(tmp_path / "does_not_exist")) is False


def test_creation_time_uses_birthtime_when_available(tmp_path):
    class FakeStat:
        st_birthtime = 123.0
        st_ctime = 456.0

    with patch("os.stat", return_value=FakeStat()):
        assert source._creation_time(str(tmp_path)) == 123.0


def test_creation_time_falls_back_to_ctime_without_birthtime(tmp_path):
    class FakeStat:
        st_ctime = 456.0

    with patch("os.stat", return_value=FakeStat()):
        assert source._creation_time(str(tmp_path)) == 456.0


def test_candidate_volumes_dispatches_by_platform():
    with patch("platform.system", return_value="Darwin"), \
         patch.object(source, "_macos_candidate_volumes", return_value=["/Volumes/A"]) as m:
        assert source._candidate_volumes(set()) == ["/Volumes/A"]
        m.assert_called_once()

    with patch("platform.system", return_value="Linux"), \
         patch.object(source, "_linux_candidate_volumes", return_value=["/media/user/A"]) as m:
        assert source._candidate_volumes(set()) == ["/media/user/A"]
        m.assert_called_once()

    with patch("platform.system", return_value="Windows"), \
         patch.object(source, "_windows_candidate_volumes", return_value=["D:\\"]) as m:
        assert source._candidate_volumes(set()) == ["D:\\"]
        m.assert_called_once()


def test_candidate_volumes_excludes_given_paths():
    with patch("platform.system", return_value="Linux"), \
         patch.object(
             source, "_linux_candidate_volumes",
             return_value=["/media/user/A", "/media/user/B"],
         ):
        result = source._candidate_volumes({os.path.realpath("/media/user/A")})

    assert result == ["/media/user/B"]


def test_linux_candidate_volumes_finds_subdirs_under_media_user(tmp_path, monkeypatch):
    media_dir = tmp_path / "media" / "testuser"
    media_dir.mkdir(parents=True)
    (media_dir / "SDCARD").mkdir()
    (media_dir / ".hidden").mkdir()
    (media_dir / "afile.txt").write_bytes(b"x")

    monkeypatch.setenv("USER", "testuser")
    with patch.object(source, "LINUX_MEDIA_DIRS", [str(tmp_path / "media" / "{user}")]):
        result = source._linux_candidate_volumes()

    assert result == [str(media_dir / "SDCARD")]


def test_windows_candidate_volumes_checks_drive_type(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: p in ("C:\\", "D:\\"))
    fake_windll = MagicMock()
    fake_windll.kernel32.GetDriveTypeW.side_effect = lambda d: 3 if d == "C:\\" else 2  # C fixed, D removable

    with patch("ctypes.windll", fake_windll, create=True):
        result = source._windows_candidate_volumes()

    assert result == ["D:\\"]


def test_detect_source_volume_single_candidate(tmp_path):
    card = tmp_path / "SDCARD"
    card.mkdir()

    with patch.object(source, "_candidate_volumes", return_value=[str(card)]):
        assert source.detect_source_volume() == str(card)


def test_detect_source_volume_no_candidates_raises():
    with patch.object(source, "_candidate_volumes", return_value=[]):
        try:
            source.detect_source_volume()
            assert False, "expected SourceError"
        except source.SourceError as e:
            assert "No candidate source volumes" in str(e)
