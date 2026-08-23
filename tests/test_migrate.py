import errno
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from photo_importer import migrate


def _mock_dates(paths, when):
    return {p: when for p in paths}


def _with_dates(when=datetime(2024, 3, 15, 10, 0, 0)):
    return patch(
        "photo_importer.migrate.metadata.get_capture_dates",
        side_effect=lambda paths: _mock_dates(paths, when),
    )


# ---------- run_copy ----------

def test_copy_new_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=200)

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.copied == 1
    assert summary.already_present == 0
    assert summary.scanned_total == 1
    assert summary.batch_size == 1
    assert list(dest.parent.glob(".*.tmp")) == []
    # source untouched
    assert (source / "IMG_0001.jpg").exists()


def test_copy_skips_file_already_in_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"aaa")

    with _with_dates():
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.copied == 0
    assert summary.already_present == 1


def test_copy_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=200, dry_run=True)

    assert summary.copied == 1  # reported as "would copy"
    assert not (dest_root / "2024" / "03" / "15" / "IMG_0001.jpg").exists()
    assert not dest_root.exists() or not any(dest_root.rglob("*"))


def test_copy_batch_size_limits_and_resumes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"IMG_{i:04d}.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        first = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=2)
        second = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=2)
        third = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=2)

    assert first.scanned_total == 5
    assert first.batch_size == 2
    assert first.copied == 2
    assert first.remaining == 3  # 5 total, 2 processed this batch
    assert second.batch_size == 2
    assert second.copied == 2
    assert second.remaining == 1
    # third call: only 1 file remains pending (5 - 2 - 2), and already_present
    # reflects the whole-catalog count (the 4 copied by the first two calls)
    assert third.batch_size == 1
    assert third.copied == 1
    assert third.already_present == 4
    assert third.remaining == 0
    total_copied = first.copied + second.copied + third.copied
    assert total_copied == 5


def test_copy_records_failure_without_aborting_batch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    (source / "IMG_0002.jpg").write_bytes(b"bbb")
    dest_root = tmp_path / "archive"

    real_safe_copy = migrate.safe_copy

    def flaky_copy(src, dst):
        if "0001" in str(src):
            raise OSError("simulated failure")
        return real_safe_copy(src, dst)

    with _with_dates(), patch("photo_importer.migrate.safe_copy", side_effect=flaky_copy):
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.failed == 1
    assert summary.copied == 1
    assert len(summary.failed_files) == 1
    assert "IMG_0001.jpg" in summary.failed_files[0][0]
    assert (dest_root / "2024" / "03" / "15" / "IMG_0002.jpg").exists()


@pytest.mark.parametrize("overlap_case", ["equal", "source_contains_dest", "dest_contains_source"])
def test_copy_refuses_overlapping_source_and_dest(tmp_path, overlap_case):
    if overlap_case == "equal":
        source = dest = tmp_path / "shared"
        source.mkdir()
    elif overlap_case == "source_contains_dest":
        source = tmp_path / "outer"
        dest = source / "inner"
        dest.mkdir(parents=True)
    else:
        dest = tmp_path / "outer"
        source = dest / "inner"
        source.mkdir(parents=True)

    with pytest.raises(migrate.MigrateError, match="overlap"):
        migrate.run_copy(str(source), str(dest), {".jpg"}, batch_size=200)


# ---------- run_purge ----------

def test_purge_deletes_source_when_archived(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"aaa")

    with _with_dates():
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.purged == 1
    assert summary.not_yet_archived == 0
    assert not (source / "IMG_0001.jpg").exists()


def test_purge_leaves_source_when_not_yet_archived(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"  # nothing copied there yet

    with _with_dates():
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.purged == 0
    assert summary.not_yet_archived == 1
    assert (source / "IMG_0001.jpg").exists()


def test_purge_never_copies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_purge(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert not dest_root.exists() or not any(dest_root.rglob("*"))


def test_purge_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"aaa")

    with _with_dates():
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"}, batch_size=200, dry_run=True)

    assert summary.purged == 1  # reported as "would purge"
    assert (source / "IMG_0001.jpg").exists()  # but not actually deleted


def test_purge_batch_size_limits(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    for i in range(5):
        name = f"IMG_{i:04d}.jpg"
        (source / name).write_bytes(b"aaa")
        (dest_dir / name).write_bytes(b"aaa")

    with _with_dates():
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"}, batch_size=2)

    assert summary.scanned_total == 5
    assert summary.batch_size == 2
    assert summary.purged == 2
    assert summary.remaining == 3
    assert len(list(source.iterdir())) == 3


@pytest.mark.parametrize("overlap_case", ["equal", "source_contains_dest", "dest_contains_source"])
def test_purge_refuses_overlapping_source_and_dest(tmp_path, overlap_case):
    if overlap_case == "equal":
        source = dest = tmp_path / "shared"
        source.mkdir()
    elif overlap_case == "source_contains_dest":
        source = tmp_path / "outer"
        dest = source / "inner"
        dest.mkdir(parents=True)
    else:
        dest = tmp_path / "outer"
        source = dest / "inner"
        source.mkdir(parents=True)

    with pytest.raises(migrate.MigrateError, match="overlap"):
        migrate.run_purge(str(source), str(dest), {".jpg"}, batch_size=200)


# ---------- run_move ----------

def test_move_new_file_renames_into_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=200)

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.moved == 1
    assert summary.already_present == 0
    assert summary.scanned_total == 1
    assert summary.batch_size == 1
    # source gone -- unlike copy, move deletes it
    assert not (source / "IMG_0001.jpg").exists()


def test_move_deletes_source_when_already_in_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG_0001.jpg").write_bytes(b"aaa")

    with _with_dates():
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.moved == 0
    assert summary.already_present == 1
    assert not (source / "IMG_0001.jpg").exists()
    assert (dest_dir / "IMG_0001.jpg").read_bytes() == b"aaa"


def test_move_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=200, dry_run=True)

    assert summary.moved == 1  # reported as "would move"
    assert (source / "IMG_0001.jpg").exists()
    assert not (dest_root / "2024" / "03" / "15" / "IMG_0001.jpg").exists()


def test_move_falls_back_to_copy_delete_across_filesystems(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst)

    with _with_dates(), patch("photo_importer.migrate.os.replace", side_effect=flaky_replace):
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=200)

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.moved == 1
    assert summary.failed == 0
    assert not (source / "IMG_0001.jpg").exists()


def test_move_batch_size_limits_and_resumes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"IMG_{i:04d}.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        first = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=2)
        second = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=2)
        third = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=2)

    assert first.scanned_total == 5
    assert first.moved == 2
    assert first.remaining == 3
    assert second.moved == 2
    assert second.remaining == 1
    assert third.moved == 1
    assert third.remaining == 0
    assert len(list(source.iterdir())) == 0


def test_move_records_failure_without_aborting_batch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    (source / "IMG_0002.jpg").write_bytes(b"bbb")
    dest_root = tmp_path / "archive"

    real_replace = os.replace

    def flaky_replace(src, dst):
        if "0001" in str(src):
            raise OSError("simulated failure")
        return real_replace(src, dst)

    with _with_dates(), patch("photo_importer.migrate.os.replace", side_effect=flaky_replace):
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, batch_size=200)

    assert summary.failed == 1
    assert summary.moved == 1
    assert len(summary.failed_files) == 1
    assert "IMG_0001.jpg" in summary.failed_files[0][0]
    assert (source / "IMG_0001.jpg").exists()  # left in place after failure
    assert (dest_root / "2024" / "03" / "15" / "IMG_0002.jpg").exists()


@pytest.mark.parametrize("overlap_case", ["equal", "source_contains_dest", "dest_contains_source"])
def test_move_refuses_overlapping_source_and_dest(tmp_path, overlap_case):
    if overlap_case == "equal":
        source = dest = tmp_path / "shared"
        source.mkdir()
    elif overlap_case == "source_contains_dest":
        source = tmp_path / "outer"
        dest = source / "inner"
        dest.mkdir(parents=True)
    else:
        dest = tmp_path / "outer"
        source = dest / "inner"
        source.mkdir(parents=True)

    with pytest.raises(migrate.MigrateError, match="overlap"):
        migrate.run_move(str(source), str(dest), {".jpg"}, batch_size=200)
