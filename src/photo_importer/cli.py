"""photo-importer CLI: import photos/videos from a source into a date-organized
local library, and sync that library to a NAS over SMB.

Running `photo-importer` with no subcommand is the default "one-shot" flow:
import while overlapping a background NAS sync (see one_shot.py). `import`
and `sync` remain available as standalone, NAS-untouched / explicit-only
subcommands.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import migrate, nas_sync, one_shot
from .config import ConfigError, apply_cli_overrides, load_config, require_local_root
from .importer import run_import
from .source import SourceError, resolve_source
from .timing import timed


def _add_import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", help='Source path, or "auto" to detect a mounted volume')
    parser.add_argument("--local-root", help="Local destination root for the sorted library")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without copying")


def _add_workers_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workers", type=int,
        help="Concurrent rsync workers for NAS sync (default from config, or 2)",
    )


def _add_migrate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", help="Source folder to consolidate from")
    parser.add_argument(
        "--dest",
        help="Archive destination root (default: NAS mount + remote subpath from config). "
        "Overriding this skips the NAS-mount check, so migrate can run standalone against any two folders.",
    )
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")


def _add_backup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        action="append",
        help="Active backup endpoint to sync from, e.g. a phone backup app's upload folder "
        "-- repeatable for multiple sources (default: backup.source_paths from config)",
    )
    parser.add_argument(
        "--dest",
        help="Archive destination root (default: NAS mount + remote subpath from config). "
        "Overriding this skips the NAS-mount check.",
    )
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied without copying")


def _build_parser() -> argparse.ArgumentParser:
    import_args = argparse.ArgumentParser(add_help=False)
    _add_import_args(import_args)

    migrate_args = argparse.ArgumentParser(add_help=False)
    _add_migrate_args(migrate_args)

    backup_args = argparse.ArgumentParser(add_help=False)
    _add_backup_args(backup_args)

    parser = argparse.ArgumentParser(prog="photo-importer", parents=[import_args])
    _add_workers_arg(parser)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "import", parents=[import_args], help="Import new photos/videos from a source (local only, no NAS sync)"
    )

    sync_parser = subparsers.add_parser("sync", help="rsync the local library to the NAS")
    sync_parser.add_argument("--config", help="Path to config.yaml")
    sync_parser.add_argument("--local-root", help="Local library root to sync from")
    _add_workers_arg(sync_parser)

    migrate_parser = subparsers.add_parser(
        "migrate", help="Consolidate an existing catalog into the archive (copy/purge/move steps)"
    )
    migrate_subparsers = migrate_parser.add_subparsers(dest="migrate_command", required=True)
    migrate_subparsers.add_parser(
        "copy", parents=[migrate_args],
        help="Copy files into the archive (safe, additive, never touches the source)",
    )
    migrate_subparsers.add_parser(
        "purge", parents=[migrate_args],
        help="Delete source files already confirmed present in the archive (no copying)",
    )
    migrate_subparsers.add_parser(
        "move", parents=[migrate_args],
        help=(
            "Rename files straight into the archive instead of copy+purge (faster, since "
            "source and archive are normally the same filesystem) -- only for folders you "
            "know are NOT actively receiving new uploads, same as purge"
        ),
    )

    subparsers.add_parser(
        "backup-sync", parents=[backup_args],
        help=(
            "Copy new photos/videos from an active backup endpoint (e.g. a phone backup "
            "app's upload folder) into the NAS archive -- cached, copy-only, additive, "
            "since files can never be moved out of a folder something else is actively "
            "writing into"
        ),
    )

    parser.set_defaults(command="one-shot")
    return parser


def _cmd_import(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    source_dir = resolve_source(config.source, config.nas_mount_point)

    summary = run_import(source_dir, local_root, config.extension_set, dry_run=args.dry_run)

    verb = "Would be new" if args.dry_run else "New"
    print(f"Scanned: {summary.scanned}")
    print(f"{verb}: {summary.imported}")
    print(f"Already imported (skipped): {summary.skipped_duplicate}")
    return 0


def _ensure_nas_mounted(config) -> None:
    """Best-effort auto-mount (same `open <nas.smb_url>` trick one-shot mode
    already uses, see nas_sync.ensure_mounted) before requiring the NAS mount
    point to actually be reachable -- so `sync`, `migrate`, and `backup-sync`
    don't force mounting the share by hand first either.
    """
    if config.nas_mount_point:
        nas_sync.ensure_mounted(config.nas_mount_point, config.nas_smb_url)
    nas_sync.require_mounted(config.nas_mount_point)


def _cmd_sync(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    _ensure_nas_mounted(config)
    workers = args.workers or config.nas_sync_workers

    ok = nas_sync.sync_with_heartbeat(
        local_root, config.nas_mount_point, config.nas_remote_subpath, label="Sync", workers=workers
    )

    synced, total = nas_sync.count_synced(
        local_root, config.nas_mount_point, config.nas_remote_subpath, use_cache=True
    )
    print(f"Synced to NAS: {synced}/{total} files")
    if not ok:
        print("Sync failed.", file=sys.stderr)
        return 1
    print("Sync complete.")
    return 0


def _cmd_one_shot(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    source_dir = resolve_source(config.source, config.nas_mount_point)
    workers = args.workers or config.nas_sync_workers

    label = "One-shot (dry-run)" if args.dry_run else "One-shot (total)"
    with timed(label):
        result = one_shot.run_one_shot(
            source_dir,
            local_root,
            config.extension_set,
            config.nas_mount_point,
            config.nas_remote_subpath,
            config.nas_smb_url,
            dry_run=args.dry_run,
            sync_workers=workers,
        )

    verb = "Would be new" if args.dry_run else "New"
    print(f"Scanned: {result.summary.scanned}")
    print(f"{verb}: {result.summary.imported}")
    print(f"Already imported (skipped): {result.summary.skipped_duplicate}")
    if not args.dry_run:
        if result.nas_available:
            print(f"NAS sync: {'ok' if result.sync_ok else 'failed'}")
            synced, total = nas_sync.count_synced(
                local_root, config.nas_mount_point, config.nas_remote_subpath, use_cache=True
            )
            print(f"Synced to NAS: {synced}/{total} files")
        else:
            print("NAS sync: skipped (NAS not available)")
    return 0


def _resolve_migrate_args(args) -> tuple:
    config = load_config(args.config)
    source_dir = args.source or config.migrate_source_path
    if not source_dir:
        raise ConfigError(
            "Migrate source is not set. Pass --source or set migrate.source_path in your config.yaml."
        )
    if args.dest:
        dest_root = os.path.expanduser(args.dest)
    else:
        _ensure_nas_mounted(config)
        dest_root = (
            os.path.join(config.nas_mount_point, config.nas_remote_subpath)
            if config.nas_remote_subpath
            else config.nas_mount_point
        )
    return source_dir, dest_root, config.extension_set


def _resolve_backup_args(args) -> tuple:
    config = load_config(args.config)
    source_dirs = args.source or config.backup_source_paths
    if not source_dirs:
        raise ConfigError(
            "Backup sync source is not set. Pass --source (repeatable) or set "
            "backup.source_paths in your config.yaml."
        )
    if args.dest:
        dest_root = os.path.expanduser(args.dest)
    else:
        _ensure_nas_mounted(config)
        dest_root = (
            os.path.join(config.nas_mount_point, config.nas_remote_subpath)
            if config.nas_remote_subpath
            else config.nas_mount_point
        )
    return source_dirs, dest_root, config.extension_set


def _print_skipped_breakdown(skipped_by_extension: dict) -> None:
    if not skipped_by_extension:
        return
    total = sum(skipped_by_extension.values())
    print(f"Skipped (extension not configured for import): {total}")
    for ext, count in sorted(skipped_by_extension.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {ext}: {count}")


def _cmd_migrate_copy(args) -> int:
    source_dir, dest_root, extension_set = _resolve_migrate_args(args)
    summary = migrate.run_copy(source_dir, dest_root, extension_set, dry_run=args.dry_run)

    verb = "Would copy" if args.dry_run else "Copied"
    print(f"Found in source: {summary.scanned_total}")
    print(f"{verb}: {summary.copied}")
    print(f"Already in archive: {summary.already_present}")
    if summary.failed:
        print(f"Failed: {summary.failed}")
        for path, reason in summary.failed_files:
            print(f"  {path}: {reason}")
    _print_skipped_breakdown(summary.skipped_by_extension)
    return 0


def _cmd_migrate_purge(args) -> int:
    source_dir, dest_root, extension_set = _resolve_migrate_args(args)
    summary = migrate.run_purge(source_dir, dest_root, extension_set, dry_run=args.dry_run)

    verb = "Would purge" if args.dry_run else "Purged"
    print(f"Found in source: {summary.scanned_total}")
    print(f"{verb}: {summary.purged}")
    print(f"Not yet archived (left alone): {summary.not_yet_archived}")
    _print_skipped_breakdown(summary.skipped_by_extension)
    return 0


def _cmd_migrate_move(args) -> int:
    source_dir, dest_root, extension_set = _resolve_migrate_args(args)
    summary = migrate.run_move(source_dir, dest_root, extension_set, dry_run=args.dry_run)

    verb = "Would move" if args.dry_run else "Moved"
    print(f"Found in source: {summary.scanned_total}")
    print(f"{verb}: {summary.moved}")
    print(f"Already in archive (deleted from source): {summary.already_present}")
    if summary.failed:
        print(f"Failed: {summary.failed}")
        for path, reason in summary.failed_files:
            print(f"  {path}: {reason}")
    _print_skipped_breakdown(summary.skipped_by_extension)
    return 0


def _cmd_backup_sync(args) -> int:
    source_dirs, dest_root, extension_set = _resolve_backup_args(args)
    verb = "Would copy" if args.dry_run else "Copied"
    multi = len(source_dirs) > 1
    totals = migrate.CopySummary()

    for source_dir in source_dirs:
        if multi:
            print(f"== {source_dir} ==")
        label = f"Backup sync ({source_dir})" if multi else "Backup sync"
        summary = migrate.run_copy(
            source_dir, dest_root, extension_set, dry_run=args.dry_run, cache=True, label=label
        )

        print(f"Found in source: {summary.scanned_total}")
        print(f"{verb}: {summary.copied}")
        print(f"Already in archive: {summary.already_present}")
        if summary.failed:
            print(f"Failed: {summary.failed}")
            for path, reason in summary.failed_files:
                print(f"  {path}: {reason}")
        _print_skipped_breakdown(summary.skipped_by_extension)

        totals.scanned_total += summary.scanned_total
        totals.copied += summary.copied
        totals.already_present += summary.already_present
        totals.failed += summary.failed
        if multi:
            print()

    if multi:
        print(f"Total across {len(source_dirs)} sources -- found: {totals.scanned_total}, "
              f"{verb.lower()}: {totals.copied}, already in archive: {totals.already_present}, "
              f"failed: {totals.failed}")

    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import":
            return _cmd_import(args)
        elif args.command == "sync":
            return _cmd_sync(args)
        elif args.command == "migrate":
            if args.migrate_command == "copy":
                return _cmd_migrate_copy(args)
            elif args.migrate_command == "move":
                return _cmd_migrate_move(args)
            else:
                return _cmd_migrate_purge(args)
        elif args.command == "backup-sync":
            return _cmd_backup_sync(args)
        else:
            return _cmd_one_shot(args)
    except (ConfigError, SourceError, nas_sync.NasSyncError, migrate.MigrateError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
