"""Orchestrate the import: scan source, resolve capture dates, dedupe against
the local index, and copy new files into local_root/YYYY/MM/DD/.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import metadata, scanner
from .index import ImportIndex
from .progress import Progress
from .timing import timed


@dataclass
class ImportSummary:
    scanned: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    imported_files: list[str] = field(default_factory=list)


def safe_copy(src: Path, dst: Path) -> None:
    """Copy file content and mtime only -- deliberately skips shutil.copy2 /
    shutil.copystat's flag-copying (chflags). Some SD-card-sourced files
    (e.g. from exFAT cards) carry a macOS-side "user immutable" flag; even
    when copying that flag onto the destination succeeds, it then makes the
    destination file immutable too, breaking the very next step (the atomic
    rename into place) -- so flags/mode are never propagated, only mtime
    (needed for metadata.py's mtime-fallback date detection), best-effort.
    """
    shutil.copyfile(src, dst)
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _cleanup_stale_temp_files(local_root: str) -> int:
    """Remove any leftover .{filename}.tmp files under local_root from a
    previous run that crashed mid-copy (before the atomic rename into
    place).

    These are always deleted, never "rescued" by renaming into place: a
    leftover .tmp file's content can't be trusted regardless of size --
    a crash could have truncated it (an incomplete write), but even a
    size-correct .tmp isn't proof against silent corruption (a bad byte
    during the copy, or something touching it while it sat orphaned). The
    source (e.g. the SD card) is the only thing that can supply a verified,
    correct copy, and a plain re-run of import will naturally re-copy
    anything that isn't at its real destination path yet -- so deleting the
    unverifiable leftover and letting that happen is strictly safer than
    trying to promote it. This is only actually lossy if the original
    source is no longer available by the time cleanup runs (caller should
    surface that risk to the user, not silently discard it).
    """
    removed = 0
    for tmp_path in Path(local_root).rglob(".*.tmp"):
        try:
            if hasattr(os, "chflags"):
                # Defense in depth: some crashes (see safe_copy) could in
                # principle leave a flag on the temp file that blocks removal.
                try:
                    os.chflags(tmp_path, 0)
                except OSError:
                    pass
            tmp_path.unlink()
            removed += 1
        except OSError as e:
            print(f"Warning: couldn't remove stale temp file {tmp_path}: {e}")
    return removed


def unique_dest_path(dest_path: Path, size: int) -> Path:
    """Resolve a filename collision at dest_path. If nothing exists there yet,
    or the existing file is the same size (treated as the same file), return it
    unchanged. Otherwise find a free "name (n).ext" path.
    """
    if not dest_path.exists() or dest_path.stat().st_size == size:
        return dest_path
    stem, suffix = dest_path.stem, dest_path.suffix
    n = 1
    while True:
        candidate = dest_path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() or candidate.stat().st_size == size:
            return candidate
        n += 1


def resolve_dest_path(root: str, capture_date: datetime, filename: str, size: int) -> Path:
    """Where a file with this capture date/filename/size belongs under root
    (root/YYYY/MM/DD/filename), resolving any collision the same way
    unique_dest_path does. Shared by run_import (root=local_root) and
    migrate.py (root=the NAS archive) so both agree on the same layout.
    """
    dest_dir = (
        Path(root)
        / capture_date.strftime("%Y")
        / capture_date.strftime("%m")
        / capture_date.strftime("%d")
    )
    return unique_dest_path(dest_dir / filename, size)


def run_import(
    source_dir: str,
    local_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> ImportSummary:
    summary = ImportSummary()
    label = "Import (dry-run)" if dry_run else "Import"

    with timed(label):
        if not dry_run and os.path.isdir(local_root):
            removed = _cleanup_stale_temp_files(local_root)
            if removed:
                print(
                    f"Removed {removed} incomplete leftover file(s) from a previous "
                    "interrupted run (their content couldn't be verified, so they were "
                    "discarded rather than kept). If the source for those files (e.g. "
                    "the SD card) is still available, this run will re-copy them "
                    "correctly below; if not, that content may be lost."
                )

        index = ImportIndex(local_root)

        files = scanner.scan(source_dir, extension_set)
        summary.scanned = len(files)
        if not files:
            return summary

        dates = metadata.get_capture_dates(files)

        total = len(files)
        progress = Progress(total)
        for i, src_path in enumerate(files, start=1):
            size = src_path.stat().st_size
            filename = src_path.name

            if index.contains(filename, size):
                summary.skipped_duplicate += 1
            else:
                capture_date = dates[src_path]
                dest_path = resolve_dest_path(local_root, capture_date, filename, size)

                # Same content already on disk but missing from the index (e.g.
                # index file was deleted) -- treat as already imported rather
                # than re-copying.
                already_on_disk = dest_path.exists() and dest_path.stat().st_size == size

                if not already_on_disk and not dry_run:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    # Copy to a hidden temp name, then atomically rename into
                    # place. A concurrent reader (e.g. a background NAS sync)
                    # must never observe a partially-written file at its real name.
                    tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
                    safe_copy(src_path, tmp_path)
                    os.replace(tmp_path, dest_path)

                index.record(filename, size, str(dest_path), capture_date.isoformat())

                if already_on_disk:
                    summary.skipped_duplicate += 1
                else:
                    summary.imported += 1
                    summary.imported_files.append(str(dest_path))

            verb = "Would import" if dry_run else "Importing"
            progress.update(
                f"{verb}: {i}/{total} ({i * 100 // total}%) "
                f"imported={summary.imported} skipped={summary.skipped_duplicate}",
                i,
            )
        progress.done()

        if not dry_run:
            index.save()

        return summary
