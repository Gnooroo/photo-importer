import shutil
from datetime import datetime
from unittest.mock import patch

from photo_importer.importer import run_import


def _mock_dates(paths, when):
    return {p: when for p in paths}


def test_import_copies_and_sorts_by_date(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"

    with patch(
        "photo_importer.importer.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        summary = run_import(str(source), str(local_root), {".jpg"})

    dest = local_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.is_file()
    assert dest.read_bytes() == b"aaa"
    assert summary.scanned == 1
    assert summary.imported == 1
    assert summary.skipped_duplicate == 0
    assert list(dest.parent.glob(".*.tmp")) == []


def test_second_run_skips_already_imported(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"

    with patch(
        "photo_importer.importer.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        run_import(str(source), str(local_root), {".jpg"})
        summary = run_import(str(source), str(local_root), {".jpg"})

    assert summary.imported == 0
    assert summary.skipped_duplicate == 1


def test_dry_run_does_not_copy_or_persist_index(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"

    with patch(
        "photo_importer.importer.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        summary = run_import(str(source), str(local_root), {".jpg"}, dry_run=True)

    assert summary.imported == 1
    assert not (local_root / "2024" / "03" / "15" / "IMG_0001.jpg").exists()
    assert not (local_root / ".photo_importer_index.json").exists()


def test_same_name_different_content_gets_suffixed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"
    dest_dir = local_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"different-content-different-size")

    with patch(
        "photo_importer.importer.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        summary = run_import(str(source), str(local_root), {".jpg"})

    assert summary.imported == 1
    assert (dest_dir / "IMG_0001 (1).jpg").read_bytes() == b"aaa"
    assert list(dest_dir.glob(".*.tmp")) == []


def test_copy_uses_temp_name_then_atomic_rename(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"
    dest = local_root / "2024" / "03" / "15" / "IMG_0001.jpg"

    real_copy2 = shutil.copy2

    def spying_copy2(src, dst):
        # A concurrent reader of local_root must never see a file at its real
        # (non-temp) name until the copy is fully done and renamed into place.
        assert not dest.exists()
        return real_copy2(src, dst)

    with patch(
        "photo_importer.importer.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ), patch("photo_importer.importer.shutil.copy2", side_effect=spying_copy2):
        run_import(str(source), str(local_root), {".jpg"})

    assert dest.is_file()
    assert dest.read_bytes() == b"aaa"
    assert list(dest.parent.glob(".*.tmp")) == []
