"""Determine the capture date of a photo/video: EXIF/metadata date first, file
mtime as a fallback (missing exiftool, unsupported format, no date tags, etc).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .progress import Progress

EXIFTOOL_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
BATCH_SIZE = 200


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
    cmd = ["exiftool", "-j", "-DateTimeOriginal", "-CreateDate", *[str(p) for p in paths]]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 and not result.stdout:
        return {}
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return {entry["SourceFile"]: entry for entry in entries}


def get_capture_dates(paths: list[Path]) -> dict[Path, datetime]:
    """Return a mapping of path -> capture date, using exiftool where possible
    and falling back to file mtime for every path exiftool couldn't resolve.
    """
    dates: dict[Path, datetime] = {}
    unresolved = list(paths)

    if exiftool_available() and paths:
        total = len(paths)
        progress = Progress(total)
        by_source_file: dict[str, dict] = {}
        for i in range(0, total, BATCH_SIZE):
            batch = paths[i:i + BATCH_SIZE]
            by_source_file.update(_run_exiftool_batch(batch))
            done = min(i + BATCH_SIZE, total)
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
