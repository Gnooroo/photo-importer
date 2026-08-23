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


class NasSyncError(Exception):
    pass


def sync(local_root: str, mount_point: str, remote_subpath: str = "") -> subprocess.CompletedProcess:
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
    cmd = ["rsync", "-av", "--ignore-existing", src, dest]
    return subprocess.run(cmd, check=True)
