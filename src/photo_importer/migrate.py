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

Neither step is wired into one-shot/import/sync -- always an explicit,
separate `photo-importer migrate copy|purge` invocation.

Both steps need to know each file's already-archived status before picking
a batch, not just take a blind prefix slice of the scan: copy never shrinks
the source, so a naive `files[:batch_size]` would re-confirm the same
already-copied prefix forever and never reach new files; a naive slice for
purge has the symmetric problem if a run of not-yet-archived files sits at
the front of sorted order, permanently blocking progress to purgeable files
further back. So both scan and date-resolve the *whole* source list up
front, split into already-done/pending, and only batch-limit the pending
half -- the actual per-file work (copying bytes, or deleting) stays bounded
by batch_size either way.
"""

from __future__ import annotations

import os
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
    batch_size: int = 0
    copied: int = 0
    failed: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    remaining: int = 0  # pending files not covered by this batch -- re-run to continue


@dataclass
class PurgeSummary:
    scanned_total: int = 0
    not_yet_archived: int = 0
    batch_size: int = 0
    purged: int = 0
    remaining: int = 0  # archived-but-not-yet-purged files not covered by this batch


def _check_no_overlap(source_dir: str, dest_root: str) -> None:
    src = os.path.realpath(source_dir)
    dst = os.path.realpath(dest_root)
    if src == dst or src.startswith(dst + os.sep) or dst.startswith(src + os.sep):
        raise MigrateError(
            f"Source ({source_dir}) and archive destination ({dest_root}) overlap -- refusing to run."
        )


def _split_by_archive_status(
    source_dir: str, dest_root: str, extension_set: set[str]
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Scan source_dir and resolve every file's archive destination (a
    single batched metadata.get_capture_dates() call for the whole scan, not
    per-file, since that's the expensive part). Returns (archived, pending):
    both are [(src_path, dest_path)] pairs -- archived's dest_path already
    exists with matching size, pending's doesn't (yet).
    """
    all_files = scanner.scan(source_dir, extension_set)
    if not all_files:
        return [], []

    dates = metadata.get_capture_dates(all_files)
    archived = []
    pending = []
    for src_path in all_files:
        size = src_path.stat().st_size
        dest_path = resolve_dest_path(dest_root, dates[src_path], src_path.name, size)
        if dest_path.exists() and dest_path.stat().st_size == size:
            archived.append((src_path, dest_path))
        else:
            pending.append((src_path, dest_path))
    return archived, pending


def run_copy(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    batch_size: int,
    dry_run: bool = False,
) -> CopySummary:
    _check_no_overlap(source_dir, dest_root)
    summary = CopySummary()

    archived, pending = _split_by_archive_status(source_dir, dest_root, extension_set)
    summary.scanned_total = len(archived) + len(pending)
    summary.already_present = len(archived)

    batch = pending[:batch_size]
    summary.batch_size = len(batch)
    summary.remaining = len(pending) - len(batch)
    if not batch:
        return summary

    total = len(batch)
    progress = Progress(total)
    label = "Migrate copy (dry-run)" if dry_run else "Migrate copy"

    with timed(label):
        for i, (src_path, dest_path) in enumerate(batch, start=1):
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
    batch_size: int,
    dry_run: bool = False,
) -> PurgeSummary:
    _check_no_overlap(source_dir, dest_root)
    summary = PurgeSummary()

    archived, pending = _split_by_archive_status(source_dir, dest_root, extension_set)
    summary.scanned_total = len(archived) + len(pending)
    summary.not_yet_archived = len(pending)

    batch = archived[:batch_size]
    summary.batch_size = len(batch)
    summary.remaining = len(archived) - len(batch)
    if not batch:
        return summary

    total = len(batch)
    progress = Progress(total)
    label = "Migrate purge (dry-run)" if dry_run else "Migrate purge"

    with timed(label):
        for i, (src_path, dest_path) in enumerate(batch, start=1):
            # Re-verify immediately before deleting -- cheap, and guards
            # against the archive copy having changed since the scan above.
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
