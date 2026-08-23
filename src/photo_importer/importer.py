"""Orchestrate the import: scan source, resolve capture dates, dedupe against
the local index, and copy new files into local_root/YYYY/MM/DD/.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata, scanner
from .index import ImportIndex


@dataclass
class ImportSummary:
    scanned: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    imported_files: list[str] = field(default_factory=list)


def _unique_dest_path(dest_path: Path, size: int) -> Path:
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


def run_import(
    source_dir: str,
    local_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> ImportSummary:
    summary = ImportSummary()
    index = ImportIndex(local_root)

    files = scanner.scan(source_dir, extension_set)
    summary.scanned = len(files)
    if not files:
        return summary

    dates = metadata.get_capture_dates(files)

    total = len(files)
    for i, src_path in enumerate(files, start=1):
        size = src_path.stat().st_size
        filename = src_path.name

        if index.contains(filename, size):
            summary.skipped_duplicate += 1
        else:
            capture_date = dates[src_path]
            dest_dir = (
                Path(local_root)
                / capture_date.strftime("%Y")
                / capture_date.strftime("%m")
                / capture_date.strftime("%d")
            )
            dest_path = _unique_dest_path(dest_dir / filename, size)

            # Same content already on disk but missing from the index (e.g.
            # index file was deleted) -- treat as already imported rather
            # than re-copying.
            already_on_disk = dest_path.exists() and dest_path.stat().st_size == size

            if not already_on_disk and not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                # Copy to a hidden temp name, then atomically rename into
                # place. A concurrent reader (e.g. a background NAS sync)
                # must never observe a partially-written file at its real name.
                tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
                shutil.copy2(src_path, tmp_path)
                os.replace(tmp_path, dest_path)

            index.record(filename, size, str(dest_path), capture_date.isoformat())

            if already_on_disk:
                summary.skipped_duplicate += 1
            else:
                summary.imported += 1
                summary.imported_files.append(str(dest_path))

        verb = "Would import" if dry_run else "Importing"
        print(
            f"\r{verb}: {i}/{total} ({i * 100 // total}%) "
            f"imported={summary.imported} skipped={summary.skipped_duplicate}",
            end="", flush=True,
        )
    print()

    if not dry_run:
        index.save()

    return summary
