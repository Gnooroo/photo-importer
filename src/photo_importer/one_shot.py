"""Default one-shot flow: import from source while a background NAS sync
runs continuously alongside it, so newly-imported files get pushed to the
NAS as the import progresses instead of only being caught by a single sync
pass after everything else is already done.

Safe to overlap with an in-progress import because:
- nas_sync.sync() is additive/idempotent (--ignore-existing, no --delete), so
  running it concurrently with new files landing in local_root never risks
  clobbering or losing anything.
- importer.run_import() writes new files via a temp-name-then-rename
  (".{name}.tmp"), and nas_sync.sync() always excludes that pattern (see
  nas_sync._is_sync_excluded), so a concurrent sync pass can never observe
  or transfer a partially-written file. Genuinely hidden photos/videos are
  not excluded and sync like any other file.

Import (main thread) and the background sync (a separate thread) each get
their own output region (see output.py) so their progress lines stay
visually distinct instead of competing for the same line/stream.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import nas_sync, source
from .importer import ImportSummary, run_import
from .output import Regions, report

BACKGROUND_SYNC_INTERVAL_SECONDS = 15


@dataclass
class OneShotResult:
    summary: ImportSummary
    nas_available: bool
    sync_ok: bool


def _background_sync_loop(
    local_root: str,
    mount_point: str,
    remote_subpath: str,
    import_done: threading.Event,
    result: dict,
    workers: int = 1,
    interval: int = BACKGROUND_SYNC_INTERVAL_SECONDS,
) -> None:
    """Runs sync passes back-to-back (paced by `interval`) for as long as
    import is still running, then one more pass once it's done -- so files
    get pushed to the NAS progressively as import produces them, rather than
    everything piling up for a single pass at the very end.

    Each pass goes through sync_with_heartbeat() (not sync() directly) so a
    single pass -- now potentially several concurrent rsync workers moving
    real data for minutes -- still reports count_synced()-based progress
    *during* the pass, not just at pass boundaries. Without this, a slow
    parallel pass would sit silent long enough to look hung, the same bug
    already fixed once this session for the non-parallel case.
    """
    pass_num = 0
    ok = True
    while not import_done.is_set():
        pass_num += 1
        pass_ok = nas_sync.sync_with_heartbeat(
            local_root, mount_point, remote_subpath, label=f"Background NAS sync (pass {pass_num})", workers=workers
        )
        ok = ok and pass_ok
        synced, total = nas_sync.count_synced(local_root, mount_point, remote_subpath, use_cache=True)
        report(f"Background NAS sync: pass {pass_num} complete ({synced}/{total} files on NAS so far)")
        import_done.wait(timeout=interval)

    pass_num += 1
    pass_ok = nas_sync.sync_with_heartbeat(
        local_root, mount_point, remote_subpath, label="Background NAS sync (final pass)", workers=workers
    )
    ok = ok and pass_ok
    synced, total = nas_sync.count_synced(local_root, mount_point, remote_subpath, use_cache=True)
    report(f"Background NAS sync: final pass complete ({synced}/{total} files on NAS)")
    result["ok"] = ok


def run_one_shot(
    source_dir: str,
    local_root: str,
    extension_set: set[str],
    nas_mount_point: str | None,
    nas_remote_subpath: str,
    nas_smb_url: str | None,
    dry_run: bool = False,
    sync_workers: int = 1,
) -> OneShotResult:
    if not source.looks_like_camera_card(source_dir):
        report(
            f"Warning: {source_dir} doesn't look like a camera card (no DCIM folder "
            "found). This tool is meant for camera SD/microSD cards, not general USB "
            "storage."
        )
        input("Press Enter to continue anyway...")

    if dry_run:
        summary = run_import(source_dir, local_root, extension_set, dry_run=True)
        return OneShotResult(summary, nas_available=False, sync_ok=False)

    nas_available = bool(nas_mount_point) and nas_sync.ensure_mounted(nas_mount_point, nas_smb_url)
    if nas_mount_point and not nas_available:
        report(
            f"Warning: {nas_mount_point} could not be mounted automatically. "
            "NAS sync will be skipped for this run."
        )
        input("Press Enter to continue with import only...")

    thread = None
    import_done = threading.Event()
    result: dict = {}
    regions = Regions(["import", "sync"]) if nas_available else None

    if nas_available:
        report("Starting background NAS sync (runs continuously alongside import)...")

        def _background():
            with regions.region("sync"):
                _background_sync_loop(
                    local_root, nas_mount_point, nas_remote_subpath, import_done, result, workers=sync_workers
                )

        thread = threading.Thread(target=_background, daemon=True)
        thread.start()

    if regions is not None:
        with regions.region("import"):
            summary = run_import(source_dir, local_root, extension_set, dry_run=False)
    else:
        summary = run_import(source_dir, local_root, extension_set, dry_run=False)

    import_done.set()

    sync_ok = False
    if thread is not None:
        thread.join()
        sync_ok = result.get("ok", False)
        regions.close()

    return OneShotResult(summary, nas_available, sync_ok)
