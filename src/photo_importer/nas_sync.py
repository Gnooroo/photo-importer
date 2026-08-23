"""Sync the local library to a NAS SMB share. Assumes the share is already
mounted/mapped (via Finder, `mount_smbfs`, a Linux cifs/GVFS mount, or a
Windows mapped drive/UNC path) -- this tool never stores or handles SMB
credentials.

This is a one-way, additive sync: the NAS is the archive of record and is
expected to accumulate files the local library no longer has (local files may
be deleted later to reclaim space). So this never passes --delete, and uses
--ignore-existing so a file already present at the destination path is never
re-transferred or overwritten -- only files missing from the NAS get copied.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from pathlib import Path

from .output import report
from .timing import timed


class NasSyncError(Exception):
    pass


def require_mounted(mount_point: str | None) -> None:
    if not mount_point:
        raise NasSyncError(
            "nas.mount_point is not set in your config.yaml."
        )
    if not os.path.ismount(mount_point):
        raise NasSyncError(
            f"{mount_point} is not currently mounted. Mount the NAS share first "
            "(Finder -> Go -> Connect to Server on macOS, a cifs/GVFS mount on "
            "Linux, or a mapped drive/UNC path on Windows), then re-run sync."
        )


def count_synced(local_root: str, mount_point: str, remote_subpath: str = "") -> tuple[int, int]:
    """Return (synced, total): the total number of files currently in
    local_root, and how many of them are also present at the NAS destination
    with a matching size (same filename:size check used elsewhere for
    dedup). This checks real, current on-disk state directly -- it doesn't
    rely on any sync command having just run or reported success -- so it's
    a trustworthy answer to "how much of my local library is actually on
    the NAS right now" independent of anything else.
    """
    dest_root = Path(mount_point) / remote_subpath if remote_subpath else Path(mount_point)
    total = 0
    synced = 0
    for path in Path(local_root).rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        total += 1
        dest_path = dest_root / path.relative_to(local_root)
        if dest_path.exists() and dest_path.stat().st_size == path.stat().st_size:
            synced += 1
    return synced, total


def ensure_mounted(mount_point: str, smb_url: str | None, timeout: int = 10) -> bool:
    """Return True if mount_point is (or becomes) mounted. On macOS, if not
    already mounted and an smb_url is configured, ask Finder to connect it
    (`open smb://...` -- uses Keychain-saved credentials if present,
    otherwise Finder prompts on its own; this tool never handles credentials
    directly) and poll briefly for the mount to appear. On Linux/Windows
    there's no equivalent single-command auto-mount, so this just reports
    current state -- the caller is expected to warn and let the user mount
    the share themselves (e.g. `mount -t cifs`, GVFS, or a mapped drive).
    """
    if os.path.ismount(mount_point):
        return True
    if not smb_url or platform.system() != "Darwin":
        return False

    subprocess.run(["open", smb_url], check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount(mount_point):
            return True
        time.sleep(1)
    return os.path.ismount(mount_point)


def sync(local_root: str, mount_point: str, remote_subpath: str = "", label: str = "Sync") -> subprocess.CompletedProcess:
    """One blocking rsync pass. Always quiet at the rsync level (no -v /
    --progress / --info=progress2): rsync's own verbose output prints a line
    per file INCLUDING every already-synced file it's skipping because of
    --ignore-existing ("Skip existing '<path>'") -- on a large, mostly-synced
    library that's a huge amount of noise for no information. Meaningful
    progress instead comes from count_synced() (an independent, trustworthy
    check of real on-disk state) via sync_with_heartbeat()/one_shot.py's
    background loop, not from rsync's own chatter.
    """
    require_mounted(mount_point)

    dest = os.path.join(mount_point, remote_subpath) if remote_subpath else mount_point
    os.makedirs(dest, exist_ok=True)

    src = local_root.rstrip("/") + "/"
    cmd = ["rsync", "-a", "--ignore-existing", "--inplace", "--exclude=.*", src, dest]

    with timed(label):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise NasSyncError(
                "rsync is not installed or not on PATH. On Windows, this typically means "
                "installing it via WSL or a port like cwrsync."
            ) from e
        if result.returncode != 0:
            raise NasSyncError(f"rsync failed (exit {result.returncode}): {result.stderr.strip()}")
        return result


def sync_with_heartbeat(
    local_root: str,
    mount_point: str,
    remote_subpath: str = "",
    label: str = "Sync",
    interval: int = 15,
) -> bool:
    """Run one sync() pass in the background while periodically reporting
    real progress (via count_synced(), not rsync's own output) so a
    long-running sync doesn't sit silent long enough to look hung. Returns
    True on success, False on failure (reported as a warning, not raised).
    """
    done = threading.Event()
    result: dict = {}

    def _run():
        try:
            sync(local_root, mount_point, remote_subpath, label=label)
            result["ok"] = True
        except NasSyncError as e:
            report(f"Warning: {label} failed: {e}")
            result["ok"] = False
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while not done.wait(timeout=interval):
        synced, total = count_synced(local_root, mount_point, remote_subpath)
        report(f"{label}: still running... ({synced}/{total} files on NAS so far)")
    thread.join()
    return result.get("ok", False)
