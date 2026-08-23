"""Determine the capture date of a photo/video: EXIF/metadata date first, file
mtime as a fallback (missing exiftool, unsupported format, no date tags, etc).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def get_capture_dates(paths: list[Path], workers: int | None = None) -> dict[Path, datetime]:
    """Return a mapping of path -> capture date, using exiftool where possible
    and falling back to file mtime for every path exiftool couldn't resolve.

    Batches run concurrently across `workers` threads (default
    DEFAULT_WORKERS) -- each batch is an independent exiftool subprocess, so
    this is real multi-core parallelism, not just concurrency.
    """
    dates: dict[Path, datetime] = {}
    unresolved = list(paths)

    if exiftool_available() and paths:
        total = len(paths)
        progress = Progress(total)
        by_source_file: dict[str, dict] = {}
        batches = [paths[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        worker_count = max(1, workers or DEFAULT_WORKERS)
        done = 0
        with timed("Reading metadata"):
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {pool.submit(_run_exiftool_batch, batch): len(batch) for batch in batches}
                # Progress updates happen here, on the main thread, as each
                # future completes -- not inside the worker threads. Progress
                # routes through a contextvar-based output region that isn't
                # inherited by new threads, so updating from a worker would
                # silently drop out of an active one-shot region.
                for future in as_completed(futures):
                    by_source_file.update(future.result())
                    done += futures[future]
                    progress.update(f"Reading metadata: {done}/{total} ({done * 100 // total}%)", done)
            progress.done()

        unresolved = []
        for path in paths:
            entry = by_source_file.get(str(path))
            date = None
            if entry:
                date = _parse_exif_date(entry.get("DateTimeOriginal")) or _parse_exif_date(
                    entry.get("CreateDate")
                )
            if date:
                dates[path] = date
            else:
                unresolved.append(path)

    for path in unresolved:
        dates[path] = datetime.fromtimestamp(path.stat().st_mtime)

    return dates
