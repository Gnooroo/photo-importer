import errno
import os
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from photo_importer import migrate


def _mock_dates(paths, when):
    yield {p: when for p in paths}


def _with_dates(when=datetime(2024, 3, 15, 10, 0, 0)):
    return patch(
        "photo_importer.migrate.metadata.iter_capture_date_batches",
        side_effect=lambda paths, workers=None: _mock_dates(paths, when),
    )


def _mock_dates_in_chunks(paths, when, chunk_size):
    for i in range(0, len(paths), chunk_size):
        yield {p: when for p in paths[i:i + chunk_size]}


def _with_dates_in_chunks(chunk_size, when=datetime(2024, 3, 15, 10, 0, 0)):
    return patch(
        "photo_importer.migrate.metadata.iter_capture_date_batches",
        side_effect=lambda paths, workers=None: _mock_dates_in_chunks(paths, when, chunk_size),
    )


# ---------- run_copy ----------

def test_copy_new_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"})

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.copied == 1
    assert summary.already_present == 0
    assert summary.scanned_total == 1
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
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"})

    assert summary.copied == 0
    assert summary.already_present == 1


def test_copy_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, dry_run=True)

    assert summary.copied == 1  # reported as "would copy"
    assert not (dest_root / "2024" / "03" / "15" / "IMG_0001.jpg").exists()
    assert not dest_root.exists() or not any(dest_root.rglob("*"))


def test_copy_processes_entire_backlog_in_one_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"IMG_{i:04d}.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates() as mock_dates:
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.copied == 5
    assert summary.already_present == 0
    # metadata is the expensive part -- one pass over the whole source list,
    # not once per file and not something a second call would redo.
    assert mock_dates.call_count == 1


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
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"})

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
        migrate.run_copy(str(source), str(dest), {".jpg"})


# ---------- run_copy cache=True (backup-sync) ----------

def test_copy_cache_second_pass_skips_metadata_entirely(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates() as mock_dates:
        summary1 = migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)
        assert summary1.copied == 1
        assert mock_dates.call_count == 1

        summary2 = migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    # second pass: the file is unchanged since being confirmed copied, so
    # it's trusted from cache -- no new exiftool batch is even submitted.
    assert mock_dates.call_count == 2
    assert mock_dates.call_args.args[0] == []
    assert summary2.copied == 0
    assert summary2.already_present == 1
    assert summary2.scanned_total == 1


def test_copy_cache_rechecks_file_whose_mtime_changed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = source / "IMG_0001.jpg"
    target.write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

        os.utime(target, (time.time() + 100, time.time() + 100))

        with _with_dates() as mock_dates:
            summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    assert mock_dates.call_args.args[0] == [target]
    assert summary.already_present == 1  # still the same content at the archive path


def test_copy_state_cache_not_updated_during_dry_run(tmp_path):
    """The archive-presence cache specifically must never be written during
    dry_run: if it were, the real run right after would wrongly trust the
    file as already archived and skip actually copying it.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, dry_run=True, cache=True)

        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    assert summary.copied == 1
    assert summary.already_present == 0
    assert (dest_root / "2024" / "03" / "15" / "IMG_0001.jpg").exists()


def test_copy_metadata_cache_populated_during_dry_run(tmp_path):
    """Unlike the archive-presence cache, the capture-date cache IS safe to
    (and does) update during dry_run -- a resolved date is a pure function
    of file content, so reusing it can never cause a file to be wrongly
    treated as already copied. A real run right after a preview dry run
    should skip exiftool entirely for anything the dry run already saw.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates() as mock_dates:
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, dry_run=True, cache=True)
        assert mock_dates.call_count == 1

        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    # second call (the real run): no new exiftool batch, since the dry run
    # already resolved and cached this file's capture date.
    assert mock_dates.call_count == 2
    assert mock_dates.call_args.args[0] == []
    assert summary.copied == 1  # still a real copy -- presence was rechecked for real


def test_copy_without_cache_flag_never_writes_state_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"})

    from photo_importer.config import app_state_dir

    assert not (app_state_dir() / migrate.BACKUP_SYNC_STATE_FILENAME).exists()


def test_copy_cache_state_file_lives_outside_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    from photo_importer.config import app_state_dir

    assert (app_state_dir() / migrate.BACKUP_SYNC_STATE_FILENAME).is_file()
    assert not any(p.name == migrate.BACKUP_SYNC_STATE_FILENAME for p in source.rglob("*"))


def test_copy_cache_scoped_to_destination(tmp_path):
    """The archive-presence cache is scoped by destination, so a file
    confirmed copied to dest_a must still be really (re-)checked and
    copied against dest_b. The capture-date metadata cache is deliberately
    NOT scoped by destination (a date doesn't depend on where the file
    lands), so exiftool itself is still correctly skipped here.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_a = tmp_path / "archive_a"
    dest_b = tmp_path / "archive_b"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_a), {".jpg"}, cache=True)

        with _with_dates() as mock_dates:
            summary = migrate.run_copy(str(source), str(dest_b), {".jpg"}, cache=True)

    assert mock_dates.call_args.args[0] == []  # metadata cache crosses destinations
    assert summary.copied == 1  # but the archive-presence check was still real
    assert (dest_b / "2024" / "03" / "15" / "IMG_0001.jpg").exists()


def test_copy_metadata_cache_file_lives_outside_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    from photo_importer.config import app_state_dir

    assert (app_state_dir() / migrate.BACKUP_METADATA_CACHE_FILENAME).is_file()
    assert not any(p.name == migrate.BACKUP_METADATA_CACHE_FILENAME for p in source.rglob("*"))


def test_copy_without_cache_flag_never_writes_metadata_cache_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"})

    from photo_importer.config import app_state_dir

    assert not (app_state_dir() / migrate.BACKUP_METADATA_CACHE_FILENAME).exists()


def test_copy_metadata_cache_rechecks_file_whose_mtime_changed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = source / "IMG_0001.jpg"
    target.write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

        os.utime(target, (time.time() + 100, time.time() + 100))

        with _with_dates() as mock_dates:
            migrate.run_copy(str(source), str(dest_root), {".jpg"}, cache=True)

    # mtime changed -- re-resolved for real, not trusted from either cache.
    assert mock_dates.call_args.args[0] == [target]


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
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"})

    assert summary.purged == 1
    assert summary.not_yet_archived == 0
    assert not (source / "IMG_0001.jpg").exists()


def test_purge_leaves_source_when_not_yet_archived(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"  # nothing copied there yet

    with _with_dates():
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"})

    assert summary.purged == 0
    assert summary.not_yet_archived == 1
    assert (source / "IMG_0001.jpg").exists()


def test_purge_never_copies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        migrate.run_purge(str(source), str(dest_root), {".jpg"})

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
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"}, dry_run=True)

    assert summary.purged == 1  # reported as "would purge"
    assert (source / "IMG_0001.jpg").exists()  # but not actually deleted


def test_purge_processes_entire_backlog_in_one_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    for i in range(5):
        name = f"IMG_{i:04d}.jpg"
        (source / name).write_bytes(b"aaa")
        (dest_dir / name).write_bytes(b"aaa")

    with _with_dates() as mock_dates:
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.purged == 5
    assert len(list(source.iterdir())) == 0
    assert mock_dates.call_count == 1


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
        migrate.run_purge(str(source), str(dest), {".jpg"})


# ---------- run_move ----------

def test_move_new_file_renames_into_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates():
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.moved == 1
    assert summary.already_present == 0
    assert summary.scanned_total == 1
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
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

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
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"}, dry_run=True)

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
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

    dest = dest_root / "2024" / "03" / "15" / "IMG_0001.jpg"
    assert dest.read_bytes() == b"aaa"
    assert summary.moved == 1
    assert summary.failed == 0
    assert not (source / "IMG_0001.jpg").exists()


def test_move_processes_entire_backlog_in_one_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(5):
        (source / f"IMG_{i:04d}.jpg").write_bytes(b"aaa")
    dest_root = tmp_path / "archive"

    with _with_dates() as mock_dates:
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.moved == 5
    assert len(list(source.iterdir())) == 0
    assert mock_dates.call_count == 1


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
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

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
        migrate.run_move(str(source), str(dest), {".jpg"})


# ---------- multi-batch (BATCH_SIZE-spanning) coverage ----------
# Every test above fits in a single metadata batch, so none of them touch
# the streaming/buffering logic itself -- these force multiple batches with
# a mix of already-archived and pending files.

def _make_mixed_source(tmp_path, count, already_archived):
    source = tmp_path / "source"
    source.mkdir()
    dest_root = tmp_path / "archive"
    dest_dir = dest_root / "2024" / "03" / "15"
    dest_dir.mkdir(parents=True)
    for i in range(count):
        name = f"IMG_{i:04d}.jpg"
        (source / name).write_bytes(b"aaa")
        if i in already_archived:
            (dest_dir / name).write_bytes(b"aaa")
    return source, dest_root, dest_dir


def test_copy_spans_multiple_batches_with_mixed_archive_status(tmp_path):
    already_archived = {1, 3}
    source, dest_root, dest_dir = _make_mixed_source(tmp_path, 5, already_archived)

    with _with_dates_in_chunks(chunk_size=2):
        summary = migrate.run_copy(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.already_present == 2
    assert summary.copied == 3
    for i in range(5):
        assert (dest_dir / f"IMG_{i:04d}.jpg").exists()


def test_purge_spans_multiple_batches_with_mixed_archive_status(tmp_path):
    already_archived = {0, 2, 4}
    source, dest_root, _ = _make_mixed_source(tmp_path, 5, already_archived)

    with _with_dates_in_chunks(chunk_size=2):
        summary = migrate.run_purge(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.purged == 3
    assert summary.not_yet_archived == 2
    for i in range(5):
        exists = (source / f"IMG_{i:04d}.jpg").exists()
        assert exists == (i not in already_archived)


def test_move_spans_multiple_batches_and_still_moves_before_deleting(tmp_path):
    already_archived = {1, 3}
    source, dest_root, dest_dir = _make_mixed_source(tmp_path, 5, already_archived)

    move_order = []
    real_move_file = migrate._move_file
    real_remove = os.remove

    def spying_move_file(src, dst):
        move_order.append(("move", src))
        return real_move_file(src, dst)

    def spying_remove(path):
        move_order.append(("delete", path))
        return real_remove(path)

    with _with_dates_in_chunks(chunk_size=2), \
         patch("photo_importer.migrate._move_file", side_effect=spying_move_file), \
         patch("photo_importer.migrate.os.remove", side_effect=spying_remove):
        summary = migrate.run_move(str(source), str(dest_root), {".jpg"})

    assert summary.scanned_total == 5
    assert summary.moved == 3
    assert summary.already_present == 2
    for i in range(5):
        assert not (source / f"IMG_{i:04d}.jpg").exists()
        assert (dest_dir / f"IMG_{i:04d}.jpg").exists()
    # every move happens before any delete, even though moves and deletes
    # were produced across interleaved metadata batches.
    kinds = [kind for kind, _ in move_order]
    assert kinds == ["move"] * 3 + ["delete"] * 2
