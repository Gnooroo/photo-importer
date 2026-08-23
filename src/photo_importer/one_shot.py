"""Default one-shot flow: import from source while overlapping a background
NAS sync of whatever's already in the local library, then a final catch-up
sync for what this run just imported.

Safe to overlap with an in-progress import because:
- nas_sync.sync() is additive/idempotent (--ignore-existing, no --delete), so
  running it concurrently with new files landing in local_root never risks
  clobbering or losing anything.
- importer.run_import() writes new files via a temp-name-then-rename, so a
  concurrent sync can never observe a partially-written file.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import nas_sync
from .importer import ImportSummary, run_import


@dataclass
class OneShotResult:
    summary: ImportSummary
    nas_available: bool
    background_sync_ok: bool
    catchup_sync_ok: bool


def _try_sync(local_root: str, mount_point: str, remote_subpath: str) -> bool:
    try:
        nas_sync.sync(local_root, mount_point, remote_subpath)
        return True
    except nas_sync.NasSyncError as e:
        print(f"Warning: NAS sync failed: {e}")
        return False


def run_one_shot(
    source_dir: str,
    local_root: str,
    extension_set: set[str],
    nas_mount_point: str | None,
    nas_remote_subpath: str,
    nas_smb_url: str | None,
    dry_run: bool = False,
) -> OneShotResult:
    if dry_run:
        summary = run_import(source_dir, local_root, extension_set, dry_run=True)
        return OneShotResult(summary, nas_available=False, background_sync_ok=False, catchup_sync_ok=False)

    nas_available = bool(nas_mount_point) and nas_sync.ensure_mounted(nas_mount_point, nas_smb_url)
    if nas_mount_point and not nas_available:
        print(
            f"Warning: {nas_mount_point} could not be mounted automatically. "
            "NAS sync will be skipped for this run."
        )
        input("Press Enter to continue with import only...")

    background_sync_ok = False
    thread = None
    if nas_available:
        result = {}

        def _background():
            result["ok"] = _try_sync(local_root, nas_mount_point, nas_remote_subpath)

        thread = threading.Thread(target=_background, daemon=True)
        thread.start()

    summary = run_import(source_dir, local_root, extension_set, dry_run=False)

    catchup_sync_ok = False
    if thread is not None:
        thread.join()
        background_sync_ok = result.get("ok", False)
        catchup_sync_ok = _try_sync(local_root, nas_mount_point, nas_remote_subpath)

    return OneShotResult(summary, nas_available, background_sync_ok, catchup_sync_ok)
