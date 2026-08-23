"""Default one-shot flow: import from source while a background NAS sync
runs continuously alongside it, so newly-imported files get pushed to the
NAS as the import progresses instead of only being caught by a single sync
pass after everything else is already done.

Safe to overlap with an in-progress import because:
- nas_sync.sync() is additive/idempotent (--ignore-existing, no --delete), so
  running it concurrently with new files landing in local_root never risks
  clobbering or losing anything.
- importer.run_import() writes new files via a temp-name-then-rename, and
  nas_sync.sync() excludes dotfiles (--exclude=.*), so a concurrent sync pass
  can never observe or transfer a partially-written file.

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
    interval: int = BACKGROUND_SYNC_INTERVAL_SECONDS,
) -> None:
    """Runs sync passes back-to-back (paced by `interval`) for as long as
    import is still running, then one more pass once it's done -- so files
    get pushed to the NAS progressively as import produces them, rather than
    everything piling up for a single pass at the very end.
    """
    pass_num = 0
    ok = True
    while not import_done.is_set():
        pass_num += 1
        try:
            nas_sync.sync(local_root, mount_point, remote_subpath, label=f"Background NAS sync (pass {pass_num})")
        except nas_sync.NasSyncError as e:
            ok = False
            report(f"Warning: background NAS sync pass {pass_num} failed: {e}")
        synced, total = nas_sync.count_synced(local_root, mount_point, remote_subpath)
        report(f"Background NAS sync: pass {pass_num} complete ({synced}/{total} files on NAS so far)")
        import_done.wait(timeout=interval)

    pass_num += 1
    try:
        nas_sync.sync(local_root, mount_point, remote_subpath, label="Background NAS sync (final pass)")
    except nas_sync.NasSyncError as e:
        ok = False
        report(f"Warning: background NAS sync final pass failed: {e}")
    synced, total = nas_sync.count_synced(local_root, mount_point, remote_subpath)
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
                _background_sync_loop(local_root, nas_mount_point, nas_remote_subpath, import_done, result)

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
