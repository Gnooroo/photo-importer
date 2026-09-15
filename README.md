# photo-importer

CLI that imports photos/videos from an SD card (or any folder) into a local
library organized by capture date (`YYYY/MM/DD`), skips files already
imported, and syncs the library to a NAS over SMB via `rsync`.

## Install

macOS / Linux: `./install.sh`
Windows (PowerShell): `.\install.ps1`

Installs `photo-importer` globally via [pipx](https://pipx.pypa.io/) as an
editable install, so code changes here take effect immediately (re-run only
if `pyproject.toml` dependencies change). Also creates a user config at
`~/.config/photo-importer/config.yaml` (macOS/Linux) or
`%APPDATA%\photo-importer\config.yaml` (Windows) — see `config.example.yaml`
for what to fill in.

Requires:
- [exiftool](https://exiftool.org/) for accurate capture dates (falls back to
  file mtime if missing)
- `rsync` on PATH for NAS sync (bundled on macOS/Linux; on Windows use WSL or
  [cwrsync](https://itefix.net/cwrsync))

**Working on the code without a global install:**
```
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
```

## Usage

| Command | What it does |
|---|---|
| `photo-importer` | One-shot: import + background NAS sync overlapped (see below) |
| `photo-importer import` | Import only, never touches the NAS |
| `photo-importer sync` | Push local library to the NAS |
| `photo-importer backup-sync` | Copy new files from a phone-backup-style folder into the archive |
| `photo-importer migrate copy\|purge\|move` | One-time consolidation of an existing messy catalog |

Add `--dry-run` to preview without writing anything. All commands read
`config.yaml` (cwd, then `~/.config/photo-importer/`), overridable with
`--config`, `--source`, `--local-root`, `--dest` as applicable per command.

Summary output distinguishes `New` from `Already imported (skipped)`, and
`sync`/one-shot report `Synced to NAS: X/Y files` — a live check of what's
actually on the NAS, not just whether the last sync exited 0.

## How dedup works

Each import records `filename:size -> destination path` in
`<local_root>/.photo_importer_index.json`. Re-running against an overlapping
source skips anything already indexed.

## Interrupted imports

Files copy to a hidden temp name and are atomically renamed once complete. A
crash mid-copy leaves an orphaned temp file; the next run always **deletes**
it (never resumes it, since partial content can't be trusted) and re-copies
from source. This only loses data if the original source is gone by the next
run — the tool warns when that happens.

## NAS sync model

One-way, additive push (`rsync -a --ignore-existing --inplace`, no
`--delete`): never removes or overwrites anything on the NAS. The NAS is the
permanent archive; the local library is disposable once confirmed synced.

- **Parallel workers**: `nas.sync_workers` (default 2) concurrent rsync
  processes, splitting files by a byte-balanced assignment so one worker
  doesn't get stuck with all the large video files. Override with
  `--workers N`. Network bandwidth is usually the bottleneck, not worker
  count — going higher rarely helps.
- **Auto-mount**: if `nas.mount_point` isn't mounted, one-shot/`sync`/
  `migrate`/`backup-sync` try `open <nas.smb_url>` (macOS only) and wait up to
  10s. One-shot then prompts to continue import-only; the other commands
  error out and ask you to mount manually. Skipped entirely by `--dest`.
- If sync feels slow, it's almost always the network — wired Ethernet or a
  NAS-side rsync/SSH service beats an SMB mount over Wi-Fi.

## One-shot mode

`photo-importer` with no subcommand runs NAS sync continuously in the
background (every ~15s) while import runs, plus a final pass after import
finishes — so files land on the NAS progressively instead of all at once at
the end. Safe because sync is additive/idempotent and in-progress files are
invisible to it (temp name + atomic rename, dotfiles excluded from rsync).

Also checks the source looks like a camera card (top-level `DCIM` folder)
before doing anything, since this mode acts automatically — prompts to
continue if not, so a random USB drive doesn't get imported by mistake.

## `backup-sync`

For a folder some other tool actively writes into that you don't control —
typically a NAS vendor's phone-backup app dropping files unsorted — folds new
files into the same `YYYY/MM/DD` archive:

```
photo-importer backup-sync
photo-importer backup-sync --source /Volumes/NAS/MobileBackup/Phone1 --source /Volumes/NAS/MobileBackup/Phone2
```

`--source` is repeatable (or set `backup.source_paths` in config) for
multiple endpoints, each synced independently with its own progress/summary.

It's `migrate copy` under a routine-use name: copy-only and additive, never
deletes from the source, since that folder belongs to another tool. Keeps a
small cache of already-copied files so repeat runs only process what's new.

## `migrate`

One-time (or occasional, manual) consolidation of an existing catalog into
the `YYYY/MM/DD` archive:

```
photo-importer migrate copy  --source /Volumes/NAS/OldPhotos
photo-importer migrate purge --source /Volumes/NAS/OldPhotos   # after copy, deletes matched sources
photo-importer migrate move  --source /Volumes/NAS/OldPhotos   # copy+purge in one pass
```

- **`copy`** — additive only, never touches the source. Safe to run against
  any folder, including one still receiving new files.
- **`purge`** — deletes a source file only if a matching copy already exists
  in the archive. Only run against folders that are no longer actively
  receiving new files (your call — not auto-detected).
- **`move`** — like copy+purge back to back, but renames instead of
  copy-then-delete when source and archive share a filesystem (the normal
  case), so no bytes cross the network. Same "folder must be inactive"
  caveat as `purge`.

All three scan recursively, process the whole backlog in one run, are safe to
re-run if interrupted, and refuse to run if `--source` overlaps the
destination. `--dest` overrides the NAS archive default and works standalone
(no config/NAS needed):

```
photo-importer migrate copy --source ~/OldPhotos --dest ~/Archive
```

## Progress output

Live progress line (e.g. `Importing: 342/1528 (22%) imported=340 skipped=2`),
prefixed with `[HH:MM:SS]`. Overwrites in place on a real terminal;
newline-terminated and throttled to ~5% steps when piped/redirected. Each
major step also logs a `started at` / `ended at (took ...)` line.

One-shot mode runs import and sync on separate threads, each with its own
status region (`[import]`/`[sync]` prefixes, or two live-updating ANSI
regions on a real terminal) so their output never interleaves illegibly.

`sync` never uses rsync's own `-v`/`--progress` (too noisy on a large,
mostly-synced library) — progress comes from an independent count of what's
actually confirmed on the NAS.

## Platform support

Built and tested primarily on **macOS**. Linux and Windows should work but
aren't regularly exercised:

- **Source auto-detect**: macOS `/Volumes`; Linux `/run/media/$USER`,
  `/media/$USER`, `/media`; Windows removable drive letters. `--source` with
  an explicit path always works regardless.
- **NAS auto-mount** is macOS-only. On Linux/Windows, mount the share
  yourself first (cifs/GVFS, or a mapped drive/UNC path).
- **rsync progress flags** are chosen per-OS automatically.

Report issues with the actual file paths/behavior you saw — most Linux/
Windows problems are untested edge cases, not design flaws.
