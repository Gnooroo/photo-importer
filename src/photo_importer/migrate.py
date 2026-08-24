"""Consolidate an existing, messy catalog into the archive structure --
two independent, explicitly-run steps:

- copy: always safe, purely additive. Copies files into the archive
  (dest_root/YYYY/MM/DD/) if not already there. Never touches the source.
  Safe to run against any source folder, including one still actively
  receiving new uploads.
- purge: pure cleanup, no copying. Deletes a source file only if a matching
  copy (by resolved archive path + size) is already confirmed present in
  the archive. Meant to be run only against folders you know are no longer
  actively receiving new uploads -- the tool doesn't try to detect that,
  it's the caller's judgement call which folders to point this at.
- move: same "confirmed-inactive folder" precondition as purge, but skips
  the separate copy+purge dance. Source and dest_root are normally the same
  filesystem (dest_root is always a subpath of the NAS mount), so a rename
  is a metadata-only op -- no bytes need to move over SMB at all. Files
  already present in the archive are just deleted from source (like purge);
  pending files are moved straight into place. NOT a safe substitute for
  copy against an actively-uploading folder: unlike copy, every file in a
  move batch is gone from the source immediately, including ones just
  moved in -- the same active-upload risk purge is scoped to avoid.

None of the three steps are wired into one-shot/import/sync -- always an
explicit, separate `photo-importer migrate copy|purge|move` invocation.

Each step scans the *whole* source list once up front, then date-resolves and
acts on it in BATCH_SIZE-sized chunks (metadata.iter_capture_date_batches is
the expensive part, one exiftool pass per chunk) -- a chunk's files are
copied/purged/moved as soon as that chunk's dates resolve, while the
metadata thread pool keeps working ahead on later chunks in the background.
There used to be a batch_size cap here forcing repeated re-runs to drain a
large backlog, but each re-run paid for that whole scan+parse again even
though most of it hadn't changed since the previous run. For copy in
particular that cost never shrinks (copy never removes anything from
source), so it made every re-run more expensive than the last. One
invocation now clears everything the scan finds; it's still naturally
resumable if interrupted (just re-run), since each run figures out what's
still pending fresh rather than tracking a separate cursor/checkpoint file.

run_copy(cache=True) goes further for a source meant to be re-scanned
routinely, not just resumed once (see cli.py's `backup-sync` command, wired
to an active backup endpoint like a phone backup app's upload folder): a
persistent PathStateCache (sync_cache.py, the same mechanism nas_sync.py
uses) remembers which files were already confirmed copied, so a
steady-state re-run skips the exiftool pass and the archive-presence stat
for anything unchanged since last time, instead of paying the full
scan+parse cost again on every call.

cache=True also turns on a second, independent cache (_MetadataDateCache,
below) that memoizes exiftool's resolved capture date per (relative path,
size, mtime) -- the actual expensive part on a large source. Unlike the
PathStateCache above (which asserts "this file is confirmed present in the
archive" and must never be written during dry_run, since a false positive
there would make a real run silently skip copying something), a resolved
capture date is a pure function of a file's own bytes: an unchanged file's
previously-resolved date is still correct regardless of what dry_run did or
didn't do with it. So this cache is written unconditionally, including
during --dry-run -- a preview run still warms it, and a real run right
after doesn't pay for exiftool a second time. The archive-presence check
itself is never skipped by this cache; only the exiftool call is.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import metadata, scanner
from .config import app_state_dir
from .importer import resolve_dest_path, safe_copy
from .progress import Progress
from .sync_cache import PathStateCache
from .timing import timed

BACKUP_SYNC_STATE_FILENAME = "backup_sync_state.json"
BACKUP_METADATA_CACHE_FILENAME = "backup_metadata_cache.json"

# Not CPU-bound like metadata.DEFAULT_WORKERS (exiftool subprocesses): this
# pool just overlaps stat() network round trips, so a higher count than
# cpu_count() is fine and helps more.
_STAT_WORKERS = 16


class _MetadataDateCache:
    """Persists, per relative path (relative to source_dir), the (size,
    mtime) last seen alongside the capture date exiftool resolved for it --
    see the cache=True note above for why this is safe to write even during
    dry_run, unlike PathStateCache. Scoped only to source_dir (no
    destination scoping needed: a capture date doesn't depend on where it
    ends up archived), so its state file lives in app_state_dir(), one
    shared file across sources keyed by each source's resolved path -- same
    multi-root merge-on-save shape as PathStateCache.
    """

    def __init__(self, source_dir: str):
        self.path = app_state_dir() / BACKUP_METADATA_CACHE_FILENAME
        self._root_key = str(Path(source_dir).expanduser().resolve())
        self._dirty = False
        self._entries: dict[str, list] = self._load_all().get(self._root_key, {})

    def _load_all(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, rel_path: str, size: int, mtime: float) -> datetime | None:
        entry = self._entries.get(rel_path)
        if entry is None or entry[0] != size or entry[1] != mtime:
            return None
        return datetime.fromisoformat(entry[2])

    def set(self, rel_path: str, size: int, mtime: float, capture_date: datetime) -> None:
        entry = [size, mtime, capture_date.isoformat()]
        if self._entries.get(rel_path) != entry:
            self._entries[rel_path] = entry
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_all()
        data[self._root_key] = self._entries
        with open(self.path, "w") as f:
            json.dump(data, f)
        self._dirty = False


class MigrateError(Exception):
    pass


@dataclass
class CopySummary:
    scanned_total: int = 0
    already_present: int = 0
    copied: int = 0
    failed: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


@dataclass
class PurgeSummary:
    scanned_total: int = 0
    not_yet_archived: int = 0
    purged: int = 0
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


@dataclass
class MoveSummary:
    scanned_total: int = 0
    moved: int = 0  # pending files renamed into the archive
    already_present: int = 0  # already-archived files just deleted from source
    failed: int = 0
    failed_files: list[tuple[str, str]] = field(default_factory=list)
    skipped_by_extension: dict[str, int] = field(default_factory=dict)


def _check_no_overlap(source_dir: str, dest_root: str) -> None:
    src = os.path.realpath(source_dir)
    dst = os.path.realpath(dest_root)
    if src == dst or src.startswith(dst + os.sep) or dst.startswith(src + os.sep):
        raise MigrateError(
            f"Source ({source_dir}) and archive destination ({dest_root}) overlap -- refusing to run."
        )


def _iter_archive_status_batches(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    skipped_counts: dict[str, int],
    cache: "PathStateCache | None" = None,
    metadata_cache: "_MetadataDateCache | None" = None,
) -> tuple[int, Iterator[list[tuple[Path, Path | None, bool]]]]:
    """Scan source_dir, then lazily resolve each file's archive destination
    in BATCH_SIZE-sized chunks (metadata.iter_capture_date_batches -- the
    expensive part -- one exiftool pass per chunk, chunks resolved in the
    background while the caller acts on earlier ones). Returns
    (scanned_total, batches): batches yields lists of
    (src_path, dest_path, is_archived) in scan order, where is_archived means
    dest_path already exists with matching size.

    If cache is given (only ever passed by run_copy's backup-sync path --
    see there), a file whose (size, mtime) matches what was recorded the
    last time it was confirmed present in the archive is trusted without
    resolving its capture date via exiftool or stat-ing the archive side
    again -- yielded as (src_path, None, True) up front, ahead of anything
    that still needs real work. This is what keeps a repeat scan of a
    steady-state backup endpoint (mostly files already synced last time)
    from paying for a fresh exiftool pass over the whole thing every run.

    If metadata_cache is also given, anything cache didn't already dispose
    of is checked there next: a file whose (size, mtime) matches a
    previously-resolved capture date reuses it directly, skipping exiftool
    -- but still gets a real, fresh archive-presence check (unlike a `cache`
    hit above), since a resolved date says nothing about whether the file
    was ever actually copied. See _MetadataDateCache for why this one is
    safe to populate even during dry_run.
    """
    all_files = scanner.scan(source_dir, extension_set, skipped_counts)

    if cache is None:
        need_resolve = all_files
        cached_files: list[Path] = []
    else:
        need_resolve = []
        cached_files = []
        # backup-sync's whole point is a steady-state re-run where nearly
        # every file is already a cache hit -- but each hit still needs one
        # stat() to get the current (size, mtime) to check against the
        # cache, and against a network source (SMB, the actual backup-sync
        # case) that's a real per-call round trip. Doing those concurrently,
        # same as metadata.iter_capture_date_batches does for exiftool
        # calls, overlaps the round trips instead of paying for them one at
        # a time -- the only work left once nothing needs copying.
        with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
            stats = pool.map(lambda f: f.stat(), all_files)
            for f, st in zip(all_files, stats):
                rel = f.relative_to(source_dir).as_posix()
                if cache.is_verified(rel, st.st_size, st.st_mtime):
                    cached_files.append(f)
                else:
                    need_resolve.append(f)

    if metadata_cache is None:
        date_known: list[tuple[Path, datetime]] = []
        still_need_dates = need_resolve
    else:
        date_known = []
        still_need_dates = []
        # Same round-trip-overlap reasoning as the cache stat loop above --
        # need_resolve is everything the state cache didn't already dispose
        # of, which on a fresh dry-run (metadata_cache is written during
        # dry-run, state_cache never is -- see run_copy's docstring) can be
        # the whole source, one SMB round trip per file otherwise.
        with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
            stats = pool.map(lambda f: f.stat(), need_resolve)
            for f, st in zip(need_resolve, stats):
                rel = f.relative_to(source_dir).as_posix()
                cached_date = metadata_cache.get(rel, st.st_size, st.st_mtime)
                if cached_date is not None:
                    date_known.append((f, cached_date))
                else:
                    still_need_dates.append(f)

    def _resolve_item(src_path: Path, capture_date: datetime) -> tuple[Path, Path, bool, os.stat_result]:
        st = src_path.stat()
        dest_path = resolve_dest_path(dest_root, capture_date, src_path.name, st.st_size)
        is_archived = dest_path.exists() and dest_path.stat().st_size == st.st_size
        return (src_path, dest_path, is_archived, st)

    def _resolve_chunk(chunk: list[tuple[Path, datetime]]) -> list[tuple[Path, Path, bool, os.stat_result]]:
        # Each _resolve_item does up to 3 network round trips (src stat, dest
        # exists, dest stat -- dest_root is the NAS archive for backup-sync),
        # done one item at a time before this; overlapping them across a
        # whole batch is the same fix as the cache/metadata stat loops above.
        with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
            return list(pool.map(lambda item: _resolve_item(*item), chunk))

    def _batches() -> Iterator[list[tuple[Path, Path | None, bool]]]:
        for i in range(0, len(cached_files), metadata.BATCH_SIZE):
            chunk = cached_files[i:i + metadata.BATCH_SIZE]
            yield [(f, None, True) for f in chunk]
        for i in range(0, len(date_known), metadata.BATCH_SIZE):
            chunk = date_known[i:i + metadata.BATCH_SIZE]
            resolved = _resolve_chunk(chunk)
            yield [(src, dest, archived) for src, dest, archived, _st in resolved]
        for batch_dates in metadata.iter_capture_date_batches(still_need_dates):
            pairs = list(batch_dates.items())
            resolved = _resolve_chunk(pairs)
            items = []
            for (src_path, capture_date), (_src, dest_path, is_archived, st) in zip(pairs, resolved):
                items.append((src_path, dest_path, is_archived))
                if metadata_cache is not None:
                    # st came from _resolve_item's own stat() -- reusing it
                    # here instead of stat-ing src_path again avoids a second
                    # round trip per file for a value already in hand.
                    rel = src_path.relative_to(source_dir).as_posix()
                    metadata_cache.set(rel, st.st_size, st.st_mtime, capture_date)
            yield items

    return len(all_files), _batches()


def _mark_source_verified(cache: PathStateCache, source_dir: str, src_path: Path) -> None:
    """After src_path is confirmed present in the archive (freshly copied,
    or already there), record its current (size, mtime) into cache so the
    next run can skip re-resolving and re-verifying it entirely. Re-stats
    locally (cheap, same source tree just scanned) rather than threading
    size/mtime through the batch tuples.
    """
    try:
        st = src_path.stat()
    except OSError:
        return
    rel = src_path.relative_to(source_dir).as_posix()
    cache.mark_verified(rel, st.st_size, st.st_mtime)


def run_copy(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
    cache: bool = False,
    label: str = "Migrate copy",
) -> CopySummary:
    """cache=True (used by backup-sync -- see cli.py's backup-sync command)
    turns on two persistent caches, both under app_state_dir(): a
    PathStateCache (sync_cache.py) keyed by (source_dir, dest_root) that
    remembers files already confirmed copied -- skipping both exiftool and
    the archive-presence check for them on a later run, but never written
    during dry_run, which must not have that side effect -- and a
    _MetadataDateCache keyed by source_dir alone that remembers each file's
    resolved capture date, which *is* written during dry_run since reusing
    a date can never cause a file to be wrongly treated as copied (see its
    docstring). Together they mean a source that's scanned repeatedly (an
    active backup endpoint that only grows) doesn't pay the full scan+parse
    cost on every call, including a preview `--dry-run` before the real one.
    """
    _check_no_overlap(source_dir, dest_root)
    summary = CopySummary()

    state_cache = PathStateCache(BACKUP_SYNC_STATE_FILENAME, source_dir, dest_root) if cache else None
    metadata_cache = _MetadataDateCache(source_dir) if cache else None
    total, batches = _iter_archive_status_batches(
        source_dir, dest_root, extension_set, summary.skipped_by_extension,
        cache=state_cache, metadata_cache=metadata_cache,
    )
    summary.scanned_total = total

    if not total:
        return summary

    progress = Progress(total)
    run_label = f"{label} (dry-run)" if dry_run else label
    i = 0

    with timed(run_label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                i += 1
                if is_archived:
                    summary.already_present += 1
                    # dest_path is None exactly when this came from a
                    # state_cache hit (see _iter_archive_status_batches) --
                    # cache.is_verified() already confirmed this file's
                    # current (size, mtime) moments ago, so re-stat-ing and
                    # re-writing the identical entry here would just be a
                    # second SMB round trip per file for no new information.
                    if state_cache is not None and not dry_run and dest_path is not None:
                        _mark_source_verified(state_cache, source_dir, src_path)
                else:
                    try:
                        if not dry_run:
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
                            safe_copy(src_path, tmp_path)
                            os.replace(tmp_path, dest_path)
                        summary.copied += 1
                        if state_cache is not None and not dry_run:
                            _mark_source_verified(state_cache, source_dir, src_path)
                    except OSError as e:
                        summary.failed += 1
                        summary.failed_files.append((str(src_path), str(e)))

                verb = "Would copy" if dry_run else "Copying"
                progress.update(
                    f"{verb}: {i}/{total} ({i * 100 // total}%) copied={summary.copied} failed={summary.failed}",
                    i,
                )
        progress.done()

    if state_cache is not None:
        state_cache.save()
    if metadata_cache is not None:
        metadata_cache.save()

    return summary


def run_purge(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> PurgeSummary:
    _check_no_overlap(source_dir, dest_root)
    summary = PurgeSummary()

    total, batches = _iter_archive_status_batches(source_dir, dest_root, extension_set, summary.skipped_by_extension)
    summary.scanned_total = total

    if not total:
        return summary

    progress = Progress(total)
    label = "Migrate purge (dry-run)" if dry_run else "Migrate purge"
    i = 0

    with timed(label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                i += 1
                if not is_archived:
                    summary.not_yet_archived += 1
                else:
                    # Re-verify immediately before deleting -- cheap, and
                    # guards against the archive copy having changed since
                    # the scan above.
                    size = src_path.stat().st_size
                    still_archived = dest_path.exists() and dest_path.stat().st_size == size
                    if still_archived:
                        if not dry_run:
                            os.remove(src_path)
                        summary.purged += 1

                verb = "Would purge" if dry_run else "Purging"
                progress.update(f"{verb}: {i}/{total} ({i * 100 // total}%) purged={summary.purged}", i)
        progress.done()

    return summary


def _move_file(src_path: Path, dest_path: Path) -> None:
    """Rename src_path into dest_path -- atomic and metadata-only when both
    are on the same filesystem (the normal case). Falls back to copy+delete
    only if they turn out to be on different filesystems.
    """
    try:
        os.replace(src_path, dest_path)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
        safe_copy(src_path, tmp_path)
        os.replace(tmp_path, dest_path)
        os.remove(src_path)


def run_move(
    source_dir: str,
    dest_root: str,
    extension_set: set[str],
    dry_run: bool = False,
) -> MoveSummary:
    _check_no_overlap(source_dir, dest_root)
    summary = MoveSummary()

    total, batches = _iter_archive_status_batches(source_dir, dest_root, extension_set, summary.skipped_by_extension)
    summary.scanned_total = total

    if not total:
        return summary

    # Pending renames happen as each batch resolves; already-archived deletes
    # are buffered and processed last -- preserves the existing "every
    # pending move happens before any archived delete" ordering (source and
    # dest are normally the same filesystem, so there's no correctness
    # reason for this order, but it keeps output/summary behavior stable).
    deferred_archived: list[tuple[Path, Path]] = []
    progress = Progress(total)
    label = "Migrate move (dry-run)" if dry_run else "Migrate move"
    i = 0

    def _report() -> None:
        nonlocal i
        i += 1
        verb = "Would move" if dry_run else "Moving"
        progress.update(
            f"{verb}: {i}/{total} ({i * 100 // total}%) moved={summary.moved} "
            f"already_present={summary.already_present} failed={summary.failed}",
            i,
        )

    with timed(label):
        for batch in batches:
            for src_path, dest_path, is_archived in batch:
                if is_archived:
                    deferred_archived.append((src_path, dest_path))
                    continue
                try:
                    if not dry_run:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        _move_file(src_path, dest_path)
                    summary.moved += 1
                except OSError as e:
                    summary.failed += 1
                    summary.failed_files.append((str(src_path), str(e)))
                _report()

        for src_path, dest_path in deferred_archived:
            try:
                # Already archived -- re-verify immediately before deleting,
                # same as purge.
                size = src_path.stat().st_size
                if dest_path.exists() and dest_path.stat().st_size == size:
                    if not dry_run:
                        os.remove(src_path)
                    summary.already_present += 1
            except OSError as e:
                summary.failed += 1
                summary.failed_files.append((str(src_path), str(e)))
            _report()

        progress.done()

    return summary
