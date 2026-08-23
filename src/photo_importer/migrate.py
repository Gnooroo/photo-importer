"""Consolidate an existing, messy catalog into the archive structure --
two independent, explicitly-run steps:

- copy: always safe, purely additive. Copies files into the archive
  (dest_root/YYYY/MM/DD/) if not already there. Never touches the source.
  Safe to run against any source folder, including one still actively
  receiving new uploads.
- purge: pure cleanup, no copying. Deletes a source file only if a matching
  copy (by resolved archive path + size) is already confirmed present in
  the archive. Meant to be run only against folders you know are no longer
  actively receiving new uploads -- the tool doesn't try to detect that,
  it's the caller's judgement call which folders to point this at.
- move: same "confirmed-inactive folder" precondition as purge, but skips
  the separate copy+purge dance. Source and dest_root are normally the same
  filesystem (dest_root is always a subpath of the NAS mount), so a rename
  is a metadata-only op -- no bytes need to move over SMB at all. Files
  already present in the archive are just deleted from source (like purge);
  pending files are moved straight into place. NOT a safe substitute for
  copy against an actively-uploading folder: unlike copy, every file in a
  move batch is gone from the source immediately, including ones just
  moved in -- the same active-upload risk purge is scoped to avoid.

None of the three steps are wired into one-shot/import/sync -- always an
explicit, separate `photo-importer migrate copy|purge|move` invocation.

Each step scans the *whole* source list once up front, then date-resolves and
acts on it in BATCH_SIZE-sized chunks (metadata.iter_capture_date_batches is
the expensive part, one exiftool pass per chunk) -- a chunk's files are
copied/purged/moved as soon as that chunk's dates resolve, while the
metadata thread pool keeps working ahead on later chunks in the background.
There used to be a batch_size cap here forcing repeated re-runs to drain a
large backlog, but each re-run paid for that whole scan+parse again even
though most of it hadn't changed since the previous run. For copy in
particular that cost never shrinks (copy never removes anything from
source), so it made every re-run more expensive than the last. One
invocation now clears everything the scan finds; it's still naturally
resumable if interrupted (just re-run), since each run figures out what's
still pending fresh rather than tracking a separate cursor/checkpoint file.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata, scanner
from .importer import resolve_dest_path, safe_copy
from .progress import Progress
from .timing import timed


class MigrateError(Exception):
    pass


@dataclass
class CopySummary:
    scanned_total: int = 0
    already_present: int = 0
    copied: int = 0
    failed: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


@dataclass
class PurgeSummary:
    scanned_total: int = 0
    not_yet_archived: int = 0
    purged: int = 0
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


@dataclass
class MoveSummary:
    scanned_total: int = 0
    moved: int = 0  # pending files renamed into the archive
    already_present: int = 0  # already-archived files just deleted from source
    failed: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


def _check_no_overlap(source_dir: str, dest_root: str) -> None:
    src = os.path.realpath(source_dir)
    dst = os.path.realpath(dest_root)
    if src == dst or src.startswith(dst + os.sep) or dst.startswith(src + os.sep):
        raise MigrateError(
            f"Source ({source_dir}) and archive destination ({dest_root}) overlap -- refusing to run."
        )


def _iter_archive_status_batches(
    source_dir: str, dest_root: str, extension_set: set[str], skipped_counts: dict[str, int]
) -> tuple[int, Iterator[list[tuple[Path, Path, bool]]]]:
    """Scan source_dir, then lazily resolve each file's archive destination
    in BATCH_SIZE-sized chunks (metadata.iter_capture_date_batches -- the
    expensive part -- one exiftool pass per chunk, chunks resolved in the
    background while the caller acts on earlier ones). Returns
    (scanned_total, batches): batches yields lists of
    (src_path, dest_path, is_archived) in scan order, where is_archived means
    dest_path already exists with matching size.
    """
    all_files = scanner.scan(source_dir, extension_set, skipped_counts)

    def _batches() -> Iterator[list[tuple[Path, Path, bool]]]:
        for batch_dates in metadata.iter_capture_date_batches(all_files):
            items = []
            for src_path, capture_date in batch_dates.items():
                size = src_path.stat().st_size
                dest_path = resolve_dest_path(dest_root, capture_date, src_path.name, size)
                is_archived = dest_path.exists() and dest_path.stat().st_size == size
                items.append((src_path, dest_path, is_archived))
            yield items

    return len(all_files), _batches()


def run_copy(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> CopySummary:
    _check_no_overlap(source_dir, dest_root)
    summary = CopySummary()

    total, batches = _iter_archive_status_batches(source_dir, dest_root, extension_set, summary.skipped_by_extension)
    summary.scanned_total = total

    if not total:
        return summary

    progress = Progress(total)
    label = "Migrate copy (dry-run)" if dry_run else "Migrate copy"
    i = 0

    with timed(label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                i += 1
                if is_archived:
                    summary.already_present += 1
                else:
                    try:
                        if not dry_run:
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
                            safe_copy(src_path, tmp_path)
                            os.replace(tmp_path, dest_path)
                        summary.copied += 1
                    except OSError as e:
                        summary.failed += 1
                        summary.failed_files.append((str(src_path), str(e)))

                verb = "Would copy" if dry_run else "Copying"
                progress.update(
                    f"{verb}: {i}/{total} ({i * 100 // total}%) copied={summary.copied} failed={summary.failed}",
                    i,
                )
        progress.done()

    return summary


def run_purge(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> PurgeSummary:
    _check_no_overlap(source_dir, dest_root)
    summary = PurgeSummary()

    total, batches = _iter_archive_status_batches(source_dir, dest_root, extension_set, summary.skipped_by_extension)
    summary.scanned_total = total

    if not total:
        return summary

    progress = Progress(total)
    label = "Migrate purge (dry-run)" if dry_run else "Migrate purge"
    i = 0

    with timed(label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                i += 1
                if not is_archived:
                    summary.not_yet_archived += 1
                else:
                    # Re-verify immediately before deleting -- cheap, and
                    # guards against the archive copy having changed since
                    # the scan above.
                    size = src_path.stat().st_size
                    still_archived = dest_path.exists() and dest_path.stat().st_size == size
                    if still_archived:
                        if not dry_run:
                            os.remove(src_path)
                        summary.purged += 1

                verb = "Would purge" if dry_run else "Purging"
                progress.update(f"{verb}: {i}/{total} ({i * 100 // total}%) purged={summary.purged}", i)
        progress.done()

    return summary


def _move_file(src_path: Path, dest_path: Path) -> None:
    """Rename src_path into dest_path -- atomic and metadata-only when both
    are on the same filesystem (the normal case). Falls back to copy+delete
    only if they turn out to be on different filesystems.
    """
    try:
        os.replace(src_path, dest_path)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
        safe_copy(src_path, tmp_path)
        os.replace(tmp_path, dest_path)
        os.remove(src_path)


def run_move(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> MoveSummary:
    _check_no_overlap(source_dir, dest_root)
    summary = MoveSummary()

    total, batches = _iter_archive_status_batches(source_dir, dest_root, extension_set, summary.skipped_by_extension)
    summary.scanned_total = total

    if not total:
        return summary

    # Pending renames happen as each batch resolves; already-archived deletes
    # are buffered and processed last -- preserves the existing "every
    # pending move happens before any archived delete" ordering (source and
    # dest are normally the same filesystem, so there's no correctness
    # reason for this order, but it keeps output/summary behavior stable).
    deferred_archived: list[tuple[Path, Path]] = []
    progress = Progress(total)
    label = "Migrate move (dry-run)" if dry_run else "Migrate move"
    i = 0

    def _report() -> None:
        nonlocal i
        i += 1
        verb = "Would move" if dry_run else "Moving"
        progress.update(
            f"{verb}: {i}/{total} ({i * 100 // total}%) moved={summary.moved} "
            f"already_present={summary.already_present} failed={summary.failed}",
            i,
        )

    with timed(label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                if is_archived:
                    deferred_archived.append((src_path, dest_path))
                    continue
                try:
                    if not dry_run:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        _move_file(src_path, dest_path)
                    summary.moved += 1
                except OSError as e:
                    summary.failed += 1
                    summary.failed_files.append((str(src_path), str(e)))
                _report()

        for src_path, dest_path in deferred_archived:
            try:
                # Already archived -- re-verify immediately before deleting,
                # same as purge.
                size = src_path.stat().st_size
                if dest_path.exists() and dest_path.stat().st_size == size:
                    if not dry_run:
                        os.remove(src_path)
                    summary.already_present += 1
            except OSError as e:
                summary.failed += 1
                summary.failed_files.append((str(src_path), str(e)))
            _report()

        progress.done()

    return summary
