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
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import app_state_dir
from .output import report
from .timing import timed


class NasSyncError(Exception):
    pass


SYNC_STATE_FILENAME = "sync_state.json"


def _dest_path(mount_point: str, remote_subpath: str) -> str:
    return os.path.join(mount_point, remote_subpath) if remote_subpath else mount_point


def _is_sync_excluded(name: str) -> bool:
    """True for files that are never real archive content and must never be
    synced to the NAS, even though a hidden file is otherwise treated like
    any other file (matching scanner.py's import behavior -- see
    a7c07b5): macOS AppleDouble sidecars ("._name", resource-fork junk left
    behind on FAT/exFAT/SMB volumes) and importer.py's own in-progress
    atomic-copy temp files (".{name}.tmp", see safe_copy/os.replace) --
    a concurrent sync pass must never observe one of those mid-write.
    """
    return name.startswith("._") or (name.startswith(".") and name.endswith(".tmp"))


class _SyncStateCache:
    """Persists, per relative path, the (size, mtime) last confirmed present
    on the NAS with a matching size -- so a later diff pass can trust that an
    untouched file (same size and mtime as last time) is still there without
    stat-ing it on the NAS side again. This is what lets a "nothing changed"
    sync pass skip re-verifying the whole archive instead of walking and
    stat-ing every file on both ends every single time.

    Lives under app_state_dir() (next to config.yaml), not inside the photo
    library itself -- the library is meant to hold only real library
    content, since it gets rsynced to the NAS verbatim (any dotfile living
    in there would otherwise have to be specially excluded from every sync,
    which would also risk excluding a legitimately hidden photo/video).
    One shared file can hold state for multiple libraries, so entries are
    keyed by local_root's resolved absolute path; saving re-reads and merges
    rather than overwriting so concurrent libraries don't clobber each
    other's state.

    Scoped to a single destination (mount_point + remote_subpath): if that
    changes, previously-recorded verifications don't mean anything against
    the new destination, so a mismatched cache entry is discarded rather
    than trusted.

    A file's mtime changing is exactly the signal that it needs
    re-verification -- camera-imported photos/videos are effectively
    write-once, so in steady state mtimes are stable and this cache stays
    valid indefinitely; touching or re-writing a file naturally invalidates
    just that one entry.
    """

    def __init__(self, local_root: str, dest: str):
        self.path = app_state_dir() / SYNC_STATE_FILENAME
        self._root_key = str(Path(local_root).expanduser().resolve())
        self._dest = dest
        self._entries: dict[str, list] = {}
        self._dirty = False
        root_data = self._load_all().get(self._root_key, {})
        if root_data.get("dest") == dest:
            self._entries = root_data.get("entries", {})

    def _load_all(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def is_verified(self, rel_path: str, size: int, mtime: float) -> bool:
        entry = self._entries.get(rel_path)
        return entry is not None and entry[0] == size and entry[1] == mtime

    def mark_verified(self, rel_path: str, size: int, mtime: float) -> None:
        entry = [size, mtime]
        if self._entries.get(rel_path) != entry:
            self._entries[rel_path] = entry
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_all()
        data[self._root_key] = {"dest": self._dest, "entries": self._entries}
        with open(self.path, "w") as f:
            json.dump(data, f)
        self._dirty = False


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
    local_root: str, mount_point: str, remote_subpath: str = "", cache: "_SyncStateCache | None" = None
) -> tuple[int, list[tuple[str, int]]]:
    """Walk local_root once and resolve every file's status at the NAS
    destination. Returns (total, pending): total file count in local_root,
    and pending = [(relative_posix_path, size), ...] for files not yet
    present at the destination with a matching size. Both count_synced()
    (progress reporting) and sync()'s worker partitioning (below) are built
    on this single walk so there's one source of truth for "what needs
    transferring."

    If cache is given, a file whose (size, mtime) matches what was recorded
    the last time it was confirmed present on the NAS is trusted without
    stat-ing the NAS side again -- see _SyncStateCache. Anything newly
    confirmed present during this walk is recorded into the cache too
    (caller is responsible for cache.save()).
    """
    dest_root = Path(mount_point) / remote_subpath if remote_subpath else Path(mount_point)
    total = 0
    pending: list[tuple[str, int]] = []
    for path in Path(local_root).rglob("*"):
        if not path.is_file() or _is_sync_excluded(path.name):
            continue
        total += 1
        st = path.stat()
        size = st.st_size
        rel_posix = path.relative_to(local_root).as_posix()
        if cache is not None and cache.is_verified(rel_posix, size, st.st_mtime):
            continue
        dest_path = dest_root / rel_posix
        if dest_path.exists() and dest_path.stat().st_size == size:
            if cache is not None:
                cache.mark_verified(rel_posix, size, st.st_mtime)
        else:
            pending.append((rel_posix, size))
    return total, pending


def count_synced(
    local_root: str, mount_point: str, remote_subpath: str = "", use_cache: bool = False
) -> tuple[int, int]:
    """Return (synced, total): the total number of files currently in
    local_root, and how many of them are also present at the NAS destination
    with a matching size (same filename:size check used elsewhere for
    dedup). By default this checks real, current on-disk state directly --
    it doesn't rely on any sync command having just run or reported success,
    or on the sync-state cache -- so it's a trustworthy answer to "how much
    of my local library is actually on the NAS right now" independent of
    anything else.

    Pass use_cache=True to instead trust the same sync-state cache sync()
    uses, for a much faster (but cache-dependent) count -- only safe to do
    right after a sync() pass has just brought the cache up to date, and
    never while a sync() pass might be concurrently writing to it (e.g. from
    within sync_with_heartbeat()'s own polling loop).
    """
    cache = _SyncStateCache(local_root, _dest_path(mount_point, remote_subpath)) if use_cache else None
    total, pending = _diff_local_vs_nas(local_root, mount_point, remote_subpath, cache=cache)
    if cache is not None:
        cache.save()
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
            "rsync", "-a", "--ignore-existing", "--inplace",
            "--exclude=._*", "--exclude=.*.tmp",
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


def _mark_chunk_verified(cache: _SyncStateCache, local_root: str, rel_paths: list[str]) -> None:
    """After a chunk's rsync succeeds, its files are now on the NAS -- record
    each one's current local (size, mtime) into the cache so the next sync
    pass can skip re-verifying it. Re-stats locally (cheap, same disk that
    was just walked) rather than threading size/mtime through
    _balanced_chunks, which stays a plain list[tuple[str, int]] contract.
    """
    for rel_path in rel_paths:
        try:
            st = (Path(local_root) / rel_path).stat()
        except OSError:
            continue
        cache.mark_verified(rel_path, st.st_size, st.st_mtime)


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

    The diff walk is accelerated by a per-destination sync-state cache (see
    _SyncStateCache) so a file already confirmed present on the NAS with a
    matching size, and untouched (same mtime) since then, doesn't get
    stat-ed on the NAS side again -- this is what keeps a steady-state
    "nothing new" sync pass fast even against a huge archive.
    """
    require_mounted(mount_point)
    workers = max(1, workers)

    dest = _dest_path(mount_point, remote_subpath)
    os.makedirs(dest, exist_ok=True)

    with timed(label):
        cache = _SyncStateCache(local_root, dest)
        _total, pending = _diff_local_vs_nas(local_root, mount_point, remote_subpath, cache=cache)
        cache.save()
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
                pool.submit(_run_rsync_files_from, local_root, dest, chunk, f"{label} worker {i + 1}"): (i + 1, chunk)
                for i, chunk in enumerate(chunks)
            }
            for future, (worker_num, chunk) in futures.items():
                try:
                    future.result()
                except NasSyncError as e:
                    failures.append(str(e))
                else:
                    _mark_chunk_verified(cache, local_root, chunk)
        cache.save()

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
