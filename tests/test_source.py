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
