"""Detect and validate the source volume (e.g. an inserted SD card)."""

from __future__ import annotations

import ctypes
import os
import platform
import string

from .output import report

MACOS_VOLUMES_DIR = "/Volumes"
MACOS_EXCLUDED_VOLUMES = {"Macintosh HD"}

# Common desktop-Linux removable-media mount roots (varies by distro/desktop
# environment -- GNOME/GVFS favors /run/media/$USER, older setups /media/$USER
# or /media directly). We check whichever of these actually exist.
LINUX_MEDIA_DIRS = ["/run/media/{user}", "/media/{user}", "/media"]

WINDOWS_DRIVE_REMOVABLE = 2  # DRIVE_REMOVABLE, per GetDriveTypeW


class SourceError(Exception):
    pass


def _creation_time(path: str) -> float:
    """Best-available "when did this volume/directory show up" timestamp.
    st_birthtime (macOS/BSD) isn't available on Linux; st_ctime means
    something different there (metadata-change time, not creation), but it's
    the closest cross-platform proxy stdlib os.stat() offers. On Windows,
    st_ctime is the actual creation time.
    """
    st = os.stat(path)
    if hasattr(st, "st_birthtime"):
        return st.st_birthtime
    return st.st_ctime


def _macos_candidate_volumes() -> list[str]:
    if not os.path.isdir(MACOS_VOLUMES_DIR):
        return []
    candidates = []
    for name in os.listdir(MACOS_VOLUMES_DIR):
        if name in MACOS_EXCLUDED_VOLUMES or name.startswith("."):
            continue
        full_path = os.path.join(MACOS_VOLUMES_DIR, name)
        if os.path.isdir(full_path):
            candidates.append(full_path)
    return candidates


def _linux_candidate_volumes() -> list[str]:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    candidates = []
    seen_dirs = set()
    for template in LINUX_MEDIA_DIRS:
        media_dir = template.format(user=user)
        real = os.path.realpath(media_dir)
        if not os.path.isdir(media_dir) or real in seen_dirs:
            continue
        seen_dirs.add(real)
        for name in os.listdir(media_dir):
            if name.startswith("."):
                continue
            full_path = os.path.join(media_dir, name)
            if os.path.isdir(full_path):
                candidates.append(full_path)
    return candidates


def _windows_candidate_volumes() -> list[str]:
    candidates = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        if ctypes.windll.kernel32.GetDriveTypeW(drive) == WINDOWS_DRIVE_REMOVABLE:
            candidates.append(drive)
    return candidates


def _candidate_volumes(exclude_paths: set[str]) -> list[str]:
    system = platform.system()
    if system == "Darwin":
        raw = _macos_candidate_volumes()
    elif system == "Windows":
        raw = _windows_candidate_volumes()
    else:
        raw = _linux_candidate_volumes()
    return [p for p in raw if os.path.realpath(p) not in exclude_paths]


def detect_source_volume(nas_mount_point: str | None = None) -> str:
    """Pick the most recently mounted removable volume, excluding the boot
    volume (macOS) and the configured NAS mount point. Raises SourceError if
    the result is ambiguous or there's nothing to import from.
    """
    exclude_paths = set()
    if nas_mount_point:
        exclude_paths.add(os.path.realpath(nas_mount_point))

    candidates = _candidate_volumes(exclude_paths)
    if not candidates:
        raise SourceError(
            "No candidate source volumes found. Insert the SD card, or pass --source explicitly."
        )
    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates: prefer ones that actually look like a camera card
    # (top-level DCIM folder) over e.g. an unrelated USB drive that merely
    # happens to be newer.
    camera_cards = [c for c in candidates if looks_like_camera_card(c)]
    if len(camera_cards) == 1:
        return camera_cards[0]
    if camera_cards:
        candidates = camera_cards
    else:
        report(
            f"None of the candidate volumes ({', '.join(candidates)}) have a DCIM "
            "folder, so the guess below isn't backed by the camera-card heuristic."
        )
        answer = input("Continue with the most-recently-mounted guess anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SourceError(
                "Aborted: no candidate volume looks like a camera card. Pass --source explicitly."
            )

    # Still ambiguous: pick the most recently mounted, but only if there's a
    # clear winner (avoids silently picking the wrong card among several).
    by_mtime = sorted(candidates, key=_creation_time, reverse=True)
    newest, second = by_mtime[0], by_mtime[1]
    if _creation_time(newest) == _creation_time(second):
        raise SourceError(
            f"Multiple candidate volumes found ({', '.join(candidates)}) "
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


def looks_like_camera_card(source_dir: str) -> bool:
    """Heuristic: virtually every digital camera (and phone) writes
    photos/videos under a top-level DCIM/ folder, per the decades-old DCF
    standard. A generic USB drive used for other files typically won't have
    one -- this is meant to catch "wrong drive" before it gets imported.
    """
    try:
        return any(
            name.upper() == "DCIM" and os.path.isdir(os.path.join(source_dir, name))
            for name in os.listdir(source_dir)
        )
    except OSError:
        return False
