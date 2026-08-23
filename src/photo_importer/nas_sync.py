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

import contextvars
import os
import platform
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"  # unreachable, keeps linters happy


def _diff_local_vs_nas(
    local_root: str, mount_point: str, remote_subpath: str = ""
) -> tuple[int, list[tuple[str, int]]]:
    """Walk local_root once and resolve every file's status at the NAS
    destination. Returns (total, pending): total file count in local_root,
    and pending = [(relative_posix_path, size), ...] for files not yet
    present at the destination with a matching size. Both count_synced()
    (progress reporting) and sync()'s worker partitioning (below) are built
    on this single walk so there's one source of truth for "what needs
    transferring."
    """
    dest_root = Path(mount_point) / remote_subpath if remote_subpath else Path(mount_point)
    total = 0
    pending: list[tuple[str, int]] = []
    for path in Path(local_root).rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        total += 1
        size = path.stat().st_size
        rel = path.relative_to(local_root)
        dest_path = dest_root / rel
        if not (dest_path.exists() and dest_path.stat().st_size == size):
            pending.append((rel.as_posix(), size))
    return total, pending


def count_synced(local_root: str, mount_point: str, remote_subpath: str = "") -> tuple[int, int]:
    """Return (synced, total): the total number of files currently in
    local_root, and how many of them are also present at the NAS destination
    with a matching size (same filename:size check used elsewhere for
    dedup). This checks real, current on-disk state directly -- it doesn't
    rely on any sync command having just run or reported success -- so it's
    a trustworthy answer to "how much of my local library is actually on
    the NAS right now" independent of anything else.
    """
    total, pending = _diff_local_vs_nas(local_root, mount_point, remote_subpath)
    return total - len(pending), total


def _balanced_chunks(pending: list[tuple[str, int]], workers: int) -> list[list[str]]:
    """Longest-Processing-Time-first greedy scheduling: sort by size
    descending, assign each file to whichever bucket currently holds the
    fewest total bytes. Camera libraries mix small JPGs with huge
    video/RAW files -- naive round-robin-by-count could load one worker
    with all the big files while others sit idle; this simple heuristic is
    within 4/3 of optimal and costs nothing extra since we already have
    each file's size from the diff walk. Returns only non-empty buckets.
    """
    buckets: list[list[str]] = [[] for _ in range(workers)]
    bucket_bytes = [0] * workers
    for rel_path, size in sorted(pending, key=lambda item: item[1], reverse=True):
        i = bucket_bytes.index(min(bucket_bytes))
        buckets[i].append(rel_path)
        bucket_bytes[i] += size
    return [b for b in buckets if b]


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


def _run_rsync_files_from(local_root: str, dest: str, rel_paths: list[str], label: str) -> None:
    """One rsync invocation transferring exactly rel_paths (relative to
    local_root) via --files-from. Raises NasSyncError on failure; always
    cleans up its temp file list.
    """
    src = local_root.rstrip("/") + "/"
    fd, files_from_path = tempfile.mkstemp(prefix="photo-importer-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(rel_paths))
        cmd = [
            "rsync", "-a", "--ignore-existing", "--inplace", "--exclude=.*",
            f"--files-from={files_from_path}", src, dest,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise NasSyncError(
                f"{label}: rsync is not installed or not on PATH. On Windows, this "
                "typically means installing it via WSL or a port like cwrsync."
            ) from e
        if result.returncode != 0:
            raise NasSyncError(f"{label} (rsync exit {result.returncode}): {result.stderr.strip()}")
    finally:
        os.unlink(files_from_path)


def sync(
    local_root: str,
    mount_point: str,
    remote_subpath: str = "",
    label: str = "Sync",
    workers: int = 1,
) -> None:
    """Sync local_root to the NAS using up to `workers` concurrent rsync
    processes, each transferring a disjoint, byte-balanced slice of the
    files not yet on the NAS (computed ourselves via _diff_local_vs_nas,
    then partitioned by _balanced_chunks -- see there for why file-level
    partitioning rather than one whole-tree rsync invocation, and why
    balancing by bytes not file count).

    Always quiet at the rsync level (no -v/--progress per worker): with
    multiple workers, N independently-chattering per-file streams would be
    even noisier than the single-worker spam already removed once this
    session. Meaningful progress instead comes from count_synced() (an
    independent, trustworthy check of real on-disk state) via
    sync_with_heartbeat() / one_shot.py's background loop, not from rsync's
    own output.
    """
    require_mounted(mount_point)
    workers = max(1, workers)

    dest = os.path.join(mount_point, remote_subpath) if remote_subpath else mount_point
    os.makedirs(dest, exist_ok=True)

    with timed(label):
        _total, pending = _diff_local_vs_nas(local_root, mount_point, remote_subpath)
        if not pending:
            report(f"{label}: nothing to sync, already up to date.")
            return

        total_bytes = sum(size for _rel, size in pending)
        chunks = _balanced_chunks(pending, workers)
        report(
            f"{label}: {len(chunks)} worker(s), {len(pending)} files pending "
            f"({format_bytes(total_bytes)})"
        )

        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = {
                pool.submit(_run_rsync_files_from, local_root, dest, chunk, f"{label} worker {i + 1}"): i + 1
                for i, chunk in enumerate(chunks)
            }
            for future, worker_num in futures.items():
                try:
                    future.result()
                except NasSyncError as e:
                    failures.append(str(e))

        if failures:
            raise NasSyncError(f"{len(failures)} of {len(chunks)} sync workers failed: {'; '.join(failures)}")


def sync_with_heartbeat(
    local_root: str,
    mount_point: str,
    remote_subpath: str = "",
    label: str = "Sync",
    workers: int = 1,
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
            sync(local_root, mount_point, remote_subpath, label=label, workers=workers)
            result["ok"] = True
        except NasSyncError as e:
            report(f"Warning: {label} failed: {e}")
            result["ok"] = False
        finally:
            done.set()

    # threading.Thread does NOT inherit the calling thread's contextvars (the
    # active output region, if one-shot mode set one) -- without explicitly
    # copying it across, everything sync() itself reports on this new thread
    # would silently fall back to the no-region default and lose its
    # [sync]-style prefix. copy_context() carries it over; a no-op when no
    # region is active (standalone `sync`).
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=ctx.run, args=(_run,), daemon=True)
    thread.start()
    while not done.wait(timeout=interval):
        synced, total = count_synced(local_root, mount_point, remote_subpath)
        report(f"{label}: still running... ({synced}/{total} files on NAS so far)")
    thread.join()
    return result.get("ok", False)
