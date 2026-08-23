"""Detect and validate the source volume (e.g. an inserted SD card)."""

from __future__ import annotations

import os

VOLUMES_DIR = "/Volumes"
EXCLUDED_VOLUMES = {"Macintosh HD"}


class SourceError(Exception):
    pass


def _candidate_volumes(exclude_paths: set[str]) -> list[str]:
    if not os.path.isdir(VOLUMES_DIR):
        return []
    candidates = []
    for name in os.listdir(VOLUMES_DIR):
        if name in EXCLUDED_VOLUMES or name.startswith("."):
            continue
        full_path = os.path.join(VOLUMES_DIR, name)
        if not os.path.isdir(full_path):
            continue
        if os.path.realpath(full_path) in exclude_paths:
            continue
        candidates.append(full_path)
    return candidates


def detect_source_volume(nas_mount_point: str | None = None) -> str:
    """Pick the most recently mounted volume under /Volumes, excluding the boot
    volume and the configured NAS mount point. Raises SourceError if the result
    is ambiguous or there's nothing to import from.
    """
    exclude_paths = set()
    if nas_mount_point:
        exclude_paths.add(os.path.realpath(nas_mount_point))

    candidates = _candidate_volumes(exclude_paths)
    if not candidates:
        raise SourceError(
            "No candidate source volumes found under /Volumes. "
            "Insert the SD card, or pass --source explicitly."
        )
    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates: pick the most recently mounted, but only if there's a
    # clear winner (avoids silently picking the wrong card among several).
    by_mtime = sorted(candidates, key=lambda p: os.stat(p).st_birthtime, reverse=True)
    newest, second = by_mtime[0], by_mtime[1]
    if os.stat(newest).st_birthtime == os.stat(second).st_birthtime:
        raise SourceError(
            f"Multiple candidate volumes found under /Volumes ({', '.join(candidates)}) "
            "and none is clearly the newest. Pass --source explicitly."
        )
    return newest


def validate_source(path: str) -> str:
    if not os.path.isdir(path):
        raise SourceError(f"Source path does not exist or is not a directory: {path}")
    return path


def resolve_source(source: str, nas_mount_point: str | None = None) -> str:
    if source == "auto":
        return detect_source_volume(nas_mount_point)
    return validate_source(os.path.expanduser(source))
