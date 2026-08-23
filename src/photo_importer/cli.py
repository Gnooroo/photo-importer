"""photo-importer CLI: import photos/videos from a source into a date-organized
local library, and sync that library to a NAS over SMB.

Running `photo-importer` with no subcommand is the default "one-shot" flow:
import while overlapping a background NAS sync (see one_shot.py). `import`
and `sync` remain available as standalone, NAS-untouched / explicit-only
subcommands.
"""

from __future__ import annotations

import argparse
import sys

from . import nas_sync, one_shot
from .config import ConfigError, apply_cli_overrides, load_config, require_local_root
from .importer import run_import
from .source import SourceError, resolve_source


def _add_import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", help='Source path, or "auto" to detect a mounted volume')
    parser.add_argument("--local-root", help="Local destination root for the sorted library")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without copying")


def _build_parser() -> argparse.ArgumentParser:
    import_args = argparse.ArgumentParser(add_help=False)
    _add_import_args(import_args)

    parser = argparse.ArgumentParser(prog="photo-importer", parents=[import_args])
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "import", parents=[import_args], help="Import new photos/videos from a source (local only, no NAS sync)"
    )

    sync_parser = subparsers.add_parser("sync", help="rsync the local library to the NAS")
    sync_parser.add_argument("--config", help="Path to config.yaml")
    sync_parser.add_argument("--local-root", help="Local library root to sync from")

    parser.set_defaults(command="one-shot")
    return parser


def _cmd_import(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    source_dir = resolve_source(config.source, config.nas_mount_point)

    summary = run_import(source_dir, local_root, config.extension_set, dry_run=args.dry_run)

    verb = "Would import" if args.dry_run else "Imported"
    print(f"Scanned: {summary.scanned}")
    print(f"{verb}: {summary.imported}")
    print(f"Skipped (already imported): {summary.skipped_duplicate}")
    return 0


def _cmd_sync(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    nas_sync.sync(local_root, config.nas_mount_point, config.nas_remote_subpath)
    print("Sync complete.")
    return 0


def _cmd_one_shot(args) -> int:
    config = apply_cli_overrides(load_config(args.config), args)
    local_root = require_local_root(config)
    source_dir = resolve_source(config.source, config.nas_mount_point)

    result = one_shot.run_one_shot(
        source_dir,
        local_root,
        config.extension_set,
        config.nas_mount_point,
        config.nas_remote_subpath,
        config.nas_smb_url,
        dry_run=args.dry_run,
    )

    verb = "Would import" if args.dry_run else "Imported"
    print(f"Scanned: {result.summary.scanned}")
    print(f"{verb}: {result.summary.imported}")
    print(f"Skipped (already imported): {result.summary.skipped_duplicate}")
    if not args.dry_run:
        if result.nas_available:
            print(f"NAS sync: {'ok' if result.catchup_sync_ok else 'failed'}")
        else:
            print("NAS sync: skipped (NAS not available)")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import":
            return _cmd_import(args)
        elif args.command == "sync":
            return _cmd_sync(args)
        else:
            return _cmd_one_shot(args)
    except (ConfigError, SourceError, nas_sync.NasSyncError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
