"""Determine the capture date of a photo/video: EXIF/metadata date first, file
mtime as a fallback (missing exiftool, unsupported format, no date tags, etc).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .progress import Progress
from .timing import timed

EXIFTOOL_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
BATCH_SIZE = 200
# exiftool itself is single-threaded per invocation; running several batches
# concurrently (subprocess.run releases the GIL while waiting) is what
# actually uses more than one core. Capped well below os.cpu_count() on
# big machines -- past a handful of concurrent exiftool processes, source-IO
# contention (SD card / network share) dominates over any further CPU
# parallelism gained.
DEFAULT_WORKERS = min(8, os.cpu_count() or 4)


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _parse_exif_date(value: str | None) -> datetime | None:
    if not value:
        return None
    # exiftool sometimes appends a timezone or subsecond fraction; keep it simple
    # and just take the first 19 chars ("YYYY:MM:DD HH:MM:SS").
    try:
        return datetime.strptime(value[:19], EXIFTOOL_DATE_FORMAT)
    except ValueError:
        return None


def _run_exiftool_batch(paths: list[Path]) -> dict[str, dict]:
    # -fast2 skips scanning for trailing/embedded data we don't need (large
    # RAW preview images, MakerNotes, walking a video's full moov atom) --
    # DateTimeOriginal/CreateDate are both found without it.
    cmd = ["exiftool", "-j", "-fast2", "-DateTimeOriginal", "-CreateDate", *[str(p) for p in paths]]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 and not result.stdout:
        return {}
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return {entry["SourceFile"]: entry for entry in entries}


def iter_capture_date_batches(
    paths: list[Path], workers: int | None = None
) -> Iterator[dict[Path, datetime]]:
    """Like get_capture_dates, but yields one path -> capture date dict per
    BATCH_SIZE-sized chunk of `paths`, in the same order `paths` was given,
    as soon as that chunk resolves -- instead of blocking until every chunk
    is done. This lets a caller start acting on early files (copy/purge/move)
    while later batches are still being read, without giving up exiftool's
    per-batch subprocess amortization or reordering output relative to the
    input list.

    All batches are submitted to the pool up front, so it keeps working
    ahead in the background regardless of how slowly the caller drains
    yielded batches -- draining in submission order (not completion order,
    i.e. not as_completed) is what keeps this order-preserving.
    """
    if not paths:
        return

    if not exiftool_available():
        for i in range(0, len(paths), BATCH_SIZE):
            chunk = paths[i:i + BATCH_SIZE]
            yield {p: datetime.fromtimestamp(p.stat().st_mtime) for p in chunk}
        return

    total = len(paths)
    progress = Progress(total)
    batches = [paths[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    worker_count = max(1, workers or DEFAULT_WORKERS)
    done = 0
    with timed("Reading metadata"):
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_run_exiftool_batch, batch) for batch in batches]
            # Progress updates happen here, on the main (consuming) thread, as
            # each batch is drained -- not inside the worker threads. Progress
            # routes through a contextvar-based output region that isn't
            # inherited by new threads, so updating from a worker would
            # silently drop out of an active one-shot region.
            for batch, future in zip(batches, futures):
                by_source_file = future.result()
                batch_dates: dict[Path, datetime] = {}
                for path in batch:
                    entry = by_source_file.get(str(path))
                    date = None
                    if entry:
                        date = _parse_exif_date(entry.get("DateTimeOriginal")) or _parse_exif_date(
                            entry.get("CreateDate")
                        )
                    batch_dates[path] = date or datetime.fromtimestamp(path.stat().st_mtime)
                done += len(batch)
                progress.update(f"Reading metadata: {done}/{total} ({done * 100 // total}%)", done)
                yield batch_dates
        progress.done()


def get_capture_dates(paths: list[Path], workers: int | None = None) -> dict[Path, datetime]:
    """Return a mapping of path -> capture date, using exiftool where possible
    and falling back to file mtime for every path exiftool couldn't resolve.

    Batches run concurrently across `workers` threads (default
    DEFAULT_WORKERS) -- each batch is an independent exiftool subprocess, so
    this is real multi-core parallelism, not just concurrency. This is
    iter_capture_date_batches drained into a single dict, for callers that
    don't need to act until every path has resolved.
    """
    dates: dict[Path, datetime] = {}
    for batch_dates in iter_capture_date_batches(paths, workers=workers):
        dates.update(batch_dates)
    return dates
