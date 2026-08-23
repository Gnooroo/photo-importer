"""Sync the local library to a NAS SMB share. Assumes the share is already
mounted (via Finder or `mount_smbfs`) -- this tool never stores or handles
SMB credentials.

This is a one-way, additive sync: the NAS is the archive of record and is
expected to accumulate files the local library no longer has (local files may
be deleted later to reclaim space). So this never passes --delete, and uses
--ignore-existing so a file already present at the destination path is never
re-transferred or overwritten -- only files missing from the NAS get copied.
"""

from __future__ import annotations

import os
import subprocess
import time


class NasSyncError(Exception):
    pass


def ensure_mounted(mount_point: str, smb_url: str | None, timeout: int = 10) -> bool:
    """Return True if mount_point is (or becomes) mounted. If not already
    mounted and an smb_url is configured, ask Finder to connect it (`open
    smb://...` -- uses Keychain-saved credentials if present, otherwise Finder
    prompts on its own; this tool never handles credentials directly) and poll
    briefly for the mount to appear.
    """
    if os.path.ismount(mount_point):
        return True
    if not smb_url:
        return False

    subprocess.run(["open", smb_url], check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount(mount_point):
            return True
        time.sleep(1)
    return os.path.ismount(mount_point)


def sync(
    local_root: str, mount_point: str, remote_subpath: str = "", verbose: bool = True
) -> subprocess.CompletedProcess:
    if not mount_point:
        raise NasSyncError(
            "nas.mount_point is not set in your config.yaml."
        )
    if not os.path.ismount(mount_point):
        raise NasSyncError(
            f"{mount_point} is not currently mounted. Mount the NAS share first "
            "(Finder -> Go -> Connect to Server, or `mount_smbfs`), then re-run sync."
        )

    dest = os.path.join(mount_point, remote_subpath) if remote_subpath else mount_point
    os.makedirs(dest, exist_ok=True)

    src = local_root.rstrip("/") + "/"
    cmd = ["rsync", "-a", "--ignore-existing", "--inplace", "--exclude=.*"]
    if verbose:
        # -v/--progress rather than --info=progress2: macOS ships openrsync
        # (protocol-29-era), which doesn't understand the newer --info= option.
        # Skipped when verbose=False (e.g. one-shot's background pass, which
        # runs concurrently with the import loop's own progress line -- two
        # live per-file streams fighting over the same terminal is just noise).
        cmd += ["-v", "--progress"]
    cmd += [src, dest]

    # Only stderr is captured (for a clean error message on failure) -- stdout
    # is left inherited so -v progress still streams live to the terminal.
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise NasSyncError(f"rsync failed (exit {result.returncode}): {result.stderr.strip()}")
    return result
