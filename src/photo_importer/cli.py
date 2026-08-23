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


def _build_parser() -> argparse.ArgumentParser:
    import_args = argparse.ArgumentParser(add_help=False)
    _add_import_args(import_args)

    migrate_args = argparse.ArgumentParser(add_help=False)
    _add_migrate_args(migrate_args)

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


def _cmd_sync(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    nas_sync.require_mounted(config.nas_mount_point)
    workers = args.workers or config.nas_sync_workers

    ok = nas_sync.sync_with_heartbeat(
        local_root, config.nas_mount_point, config.nas_remote_subpath, label="Sync", workers=workers
    )

    synced, total = nas_sync.count_synced(local_root, config.nas_mount_point, config.nas_remote_subpath)
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
            synced, total = nas_sync.count_synced(local_root, config.nas_mount_point, config.nas_remote_subpath)
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
        nas_sync.require_mounted(config.nas_mount_point)
        dest_root = (
            os.path.join(config.nas_mount_point, config.nas_remote_subpath)
            if config.nas_remote_subpath
            else config.nas_mount_point
        )
    return source_dir, dest_root, config.extension_set


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
        else:
            return _cmd_one_shot(args)
    except (ConfigError, SourceError, nas_sync.NasSyncError, migrate.MigrateError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
