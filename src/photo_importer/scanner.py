"""Walk a source directory and yield files matching the configured extensions."""

from __future__ import annotations

import os
from pathlib import Path


def scan(source_dir: str, extension_set: set[str]) -> list[Path]:
    """Recursively find files under source_dir whose suffix is in extension_set.

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
    return sorted(matches)
