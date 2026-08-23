"""photo-importer CLI: import photos/videos from a source into a date-organized
local library, and sync that library to a NAS over SMB.
"""

from __future__ import annotations

import argparse
import sys

from . import nas_sync
from .config import ConfigError, apply_cli_overrides, load_config, require_local_root
from .importer import run_import
from .source import SourceError, resolve_source


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photo-importer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="Import new photos/videos from a source")
    import_parser.add_argument("--source", help='Source path, or "auto" to detect a mounted volume')
    import_parser.add_argument("--local-root", help="Local destination root for the sorted library")
    import_parser.add_argument("--config", help="Path to config.yaml")
    import_parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without copying")

    sync_parser = subparsers.add_parser("sync", help="rsync the local library to the NAS")
    sync_parser.add_argument("--config", help="Path to config.yaml")
    sync_parser.add_argument("--local-root", help="Local library root to sync from")

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


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import":
            return _cmd_import(args)
        elif args.command == "sync":
            return _cmd_sync(args)
    except (ConfigError, SourceError, nas_sync.NasSyncError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
