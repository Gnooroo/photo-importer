import os
import shutil
import stat
from datetime import datetime
from unittest.mock import patch

import pytest

from photo_importer.importer import _cleanup_stale_temp_files, resolve_dest_path, run_import


def _mock_dates(paths, when, workers=None):
    yield {p: when for p in paths}


def test_import_copies_and_sorts_by_date(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
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
        "photo_importer.importer.metadata.iter_capture_date_batches",
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
        "photo_importer.importer.metadata.iter_capture_date_batches",
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
        "photo_importer.importer.metadata.iter_capture_date_batches",
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

    real_copyfile = shutil.copyfile

    def spying_copyfile(src, dst):
        # A concurrent reader of local_root must never see a file at its real
        # (non-temp) name until the copy is fully done and renamed into place.
        assert not dest.exists()
        return real_copyfile(src, dst)

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ), patch("photo_importer.importer.shutil.copyfile", side_effect=spying_copyfile):
        run_import(str(source), str(local_root), {".jpg"})

    assert dest.is_file()
    assert dest.read_bytes() == b"aaa"
    assert list(dest.parent.glob(".*.tmp")) == []


def test_copy_survives_utime_permission_error(tmp_path):
    """If preserving mtime fails for any reason, content must still land
    correctly -- mtime preservation is best-effort, not required for success.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")

    local_root = tmp_path / "library"
    dest = local_root / "2024" / "03" / "15" / "IMG_0001.jpg"

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ), patch("photo_importer.importer.os.utime", side_effect=PermissionError("utime")):
        summary = run_import(str(source), str(local_root), {".jpg"})

    assert summary.imported == 1
    assert dest.read_bytes() == b"aaa"


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="chflags is macOS/BSD-only")
def test_copy_survives_source_file_with_immutable_flag(tmp_path):
    """Regression test for a real crash: some SD-card-sourced files (seen
    with exFAT-formatted camera cards) carry a macOS "user immutable" flag.
    Propagating that flag to the destination (as shutil.copy2/copystat would)
    makes the destination file immutable too, which then breaks the very
    next step -- the atomic rename into place. _safe_copy must never
    propagate flags, so this must succeed end-to-end.
    """
    source = tmp_path / "source"
    source.mkdir()
    src_file = source / "IMG_0001.jpg"
    src_file.write_bytes(b"aaa")
    os.chflags(str(src_file), stat.UF_IMMUTABLE)

    local_root = tmp_path / "library"
    dest = local_root / "2024" / "03" / "15" / "IMG_0001.jpg"

    try:
        with patch(
            "photo_importer.importer.metadata.iter_capture_date_batches",
            side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
        ):
            summary = run_import(str(source), str(local_root), {".jpg"})
    finally:
        os.chflags(str(src_file), 0)  # allow tmp_path cleanup

    assert summary.imported == 1
    assert dest.read_bytes() == b"aaa"


def test_cleanup_stale_temp_files_removes_leftover_tmp(tmp_path):
    dest_dir = tmp_path / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    stale = dest_dir / ".IMG_0001.jpg.tmp"
    stale.write_bytes(b"partial")
    keep = dest_dir / "IMG_0002.jpg"
    keep.write_bytes(b"real file")

    removed = _cleanup_stale_temp_files(str(tmp_path))

    assert removed == 1
    assert not stale.exists()
    assert keep.exists()


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="chflags is macOS/BSD-only")
def test_cleanup_stale_temp_files_clears_immutable_flag_first(tmp_path):
    dest_dir = tmp_path / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    stale = dest_dir / ".IMG_0001.jpg.tmp"
    stale.write_bytes(b"partial")
    os.chflags(str(stale), stat.UF_IMMUTABLE)

    removed = _cleanup_stale_temp_files(str(tmp_path))

    assert removed == 1
    assert not stale.exists()


def test_run_import_cleans_up_stale_temp_files_automatically(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0002.jpg").write_bytes(b"bbb")

    local_root = tmp_path / "library"
    stale_dir = local_root / "2024" / "01" / "01"
    stale_dir.mkdir(parents=True)
    (stale_dir / ".SomeOldFile.jpg.tmp").write_bytes(b"partial")

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        run_import(str(source), str(local_root), {".jpg"})

    assert not (stale_dir / ".SomeOldFile.jpg.tmp").exists()


def test_run_import_dry_run_does_not_clean_up_stale_temp_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0002.jpg").write_bytes(b"bbb")

    local_root = tmp_path / "library"
    stale_dir = local_root / "2024" / "01" / "01"
    stale_dir.mkdir(parents=True)
    stale = stale_dir / ".SomeOldFile.jpg.tmp"
    stale.write_bytes(b"partial")

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
        side_effect=lambda paths: _mock_dates(paths, datetime(2024, 3, 15, 10, 0, 0)),
    ):
        run_import(str(source), str(local_root), {".jpg"}, dry_run=True)

    assert stale.exists()


def test_resolve_dest_path_builds_date_folder(tmp_path):
    path = resolve_dest_path(str(tmp_path), datetime(2024, 3, 15, 10, 0, 0), "IMG_0001.jpg", size=100)

    assert path == tmp_path / "2024" / "03" / "15" / "IMG_0001.jpg"


def test_resolve_dest_path_matches_run_import_collision_behavior(tmp_path):
    dest_dir = tmp_path / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"different-content-different-size")

    path = resolve_dest_path(str(tmp_path), datetime(2024, 3, 15, 10, 0, 0), "IMG_0001.jpg", size=3)

    assert path == dest_dir / "IMG_0001 (1).jpg"


def _mock_dates_in_chunks(paths, when, chunk_size):
    for i in range(0, len(paths), chunk_size):
        yield {p: when for p in paths[i:i + chunk_size]}


def test_import_spans_multiple_metadata_batches(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"IMG_{i:04d}.jpg").write_bytes(f"content-{i}".encode())

    local_root = tmp_path / "library"
    when = datetime(2024, 3, 15, 10, 0, 0)

    with patch(
        "photo_importer.importer.metadata.iter_capture_date_batches",
        side_effect=lambda paths: _mock_dates_in_chunks(paths, when, chunk_size=2),
    ):
        summary = run_import(str(source), str(local_root), {".jpg"})

    assert summary.scanned == 5
    assert summary.imported == 5
    assert summary.skipped_duplicate == 0
    for i in range(5):
        dest = local_root / "2024" / "03" / "15" / f"IMG_{i:04d}.jpg"
        assert dest.read_bytes() == f"content-{i}".encode()
    assert sorted(summary.imported_files) == sorted(
        str(local_root / "2024" / "03" / "15" / f"IMG_{i:04d}.jpg") for i in range(5)
    )
