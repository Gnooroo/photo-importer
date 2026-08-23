"""Walk a source directory and yield files matching the configured extensions."""

from __future__ import annotations

import os
from pathlib import Path


def scan(
    source_dir: str,
    extension_set: set[str],
    skipped_counts: dict[str, int] | None = None,
) -> list[Path]:
    """Recursively find files under source_dir whose suffix is in extension_set.

    If skipped_counts is given, it's updated in place with a count of files
    seen but not matched, keyed by lowercased suffix ("(no extension)" for
    files without one).

    Returns a sorted list of Path objects for deterministic ordering.
    """
    matches = []
    for root, _dirs, files in os.walk(source_dir):
        for name in files:
            if name.startswith("."):
                continue
            suffix = Path(name).suffix.lower()
            if suffix in extension_set:
                matches.append(Path(root) / name)
            elif skipped_counts is not None:
                key = suffix or "(no extension)"
                skipped_counts[key] = skipped_counts.get(key, 0) + 1
    return sorted(matches)
