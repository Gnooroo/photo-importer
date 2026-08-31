# photo-importer

A small CLI for importing photos/videos off an SD card (or any source folder)
into a local library organized by capture date (`YYYY/MM/DD`), skipping files
that have already been imported, and syncing that library to a NAS SMB share
via `rsync`.

## Install

macOS / Linux:

```
./install.sh
```

Windows (PowerShell):

```
.\install.ps1
```

Installs `photo-importer` as a global command via [pipx](https://pipx.pypa.io/)
(installed first if you don't have it -- via Homebrew on macOS, `pip install
--user pipx` on Linux/Windows) -- runnable from any directory afterward, no
venv activation needed. It's an *editable* install pointing back at this
checkout, so pulling/editing code here takes effect immediately; re-run the
install script only if `pyproject.toml`'s dependencies change. It also sets up
a user config file (`~/.config/photo-importer/config.yaml` on macOS/Linux,
`%APPDATA%\photo-importer\config.yaml` on Windows -- copying your existing
repo-root `config.yaml` there if you have one, otherwise from the example
template) -- see `config.example.yaml` for what to fill in.

Requires [exiftool](https://exiftool.org/) for accurate capture-date detection
(`brew install exiftool` / your Linux package manager / the Windows installer
on exiftool.org); without it, file modification time is used instead. NAS
sync requires `rsync` on PATH -- present by default on macOS and virtually
all Linux distros, but not on Windows (install it via WSL, or a native port
like [cwrsync](https://itefix.net/cwrsync)).

**Working on the code without a global install** -- use a local venv instead:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
```

## Usage

**Default (one-shot) mode** -- run with no subcommand. Imports from the
configured/auto-detected source while overlapping a background sync of
whatever's already in the local library, then does one more sync pass at the
end to push what this run just imported. See "One-shot mode" below.

```
photo-importer
photo-importer --dry-run     # preview only, never touches the NAS
```

**`import`** -- local-only, never touches the NAS:

```
photo-importer import
photo-importer import --source /Volumes/SDCARD
photo-importer import --dry-run
```

**`sync`** -- push the local library to the NAS on its own (tries to
auto-mount the SMB share first, same as one-shot mode -- see below):

```
photo-importer sync
```

**`backup-sync`** -- copy new photos/videos from an active backup endpoint
(e.g. a phone backup app's upload folder) straight into the NAS archive (see
"`backup-sync`: syncing an active backup endpoint" below):

```
photo-importer backup-sync
```

All four read `config.yaml` from the current directory (or
`~/.config/photo-importer/config.yaml`); pass `--config path/to/file.yaml` to
use a different one. `--source` and `--local-root` (one-shot and `import`),
`--local-root` (`sync`), and `--source` (`backup-sync`, repeatable, from
`backup.source_paths`) override the config file.

Summary output uses `New` / `Already imported (skipped)` rather than
"Imported" for the already-there count -- on a re-run where everything is
already present, this reads as "0 new, 983 already imported" instead of the
easily-misread "Imported: 0" (which looks like nothing worked, when really
nothing *needed* to). `sync` and one-shot (when the NAS is available) also
report `Synced to NAS: X/Y files` -- a live comparison of what's actually on
the NAS right now against the local library, independent of whether the sync
that just ran succeeded, so it's always a trustworthy answer to "how caught
up is the archive," not just "did the last command exit 0."

## How dedup works

Each import records `filename:size -> destination path` in
`<local_root>/.photo_importer_index.json`. Re-running against the same or an
overlapping source skips anything already in the index, so you can import
incrementally from a card without worrying about double-copying.

## Recovering from an interrupted import

New files are copied to a hidden temp name (`.filename.ext.tmp`) and only
atomically renamed to their real name once the copy finishes -- if the tool
crashes or is killed mid-copy, the next `import` run automatically finds and
removes any such leftover temp files before doing anything else. They're
always **deleted, not resumed/promoted**: an orphaned temp file's content
can't be verified as correct (a crash could have truncated it, and even a
right-sized one isn't proof against a bad byte during the copy), so the
source is trusted over the leftover, and the same re-run naturally re-copies
that file cleanly. This is only actually lossy if the original source (e.g.
the SD card) is no longer available by the time you re-run -- the tool prints
a clear message when this happens so you can check.

## Local library vs. NAS: NAS is the archive

Sync is a one-way, additive push (`rsync -a --ignore-existing --inplace`, no
`--delete`): it never removes or overwrites anything on the NAS, it only
copies files that aren't there yet. This is intentional -- the NAS is meant
to hold everything ever imported, while the local library is disposable and
can be pruned to save space once its contents are confirmed synced. A file
already present on the NAS at the same relative path is treated as a
duplicate and skipped, regardless of whether the local copy is still around.

If sync feels slow, it's almost always the network, not rsync: a Wi-Fi link
to the NAS is the usual bottleneck for large photo/video libraries, and a
wired Ethernet connection (or an NAS-side rsync/SSH service instead of an SMB
mount, if available) will generally be much faster.

**Parallel sync workers**: since rsync doesn't parallelize file transfers
within one process, `sync` (standalone and one-shot's background loop) runs
`nas.sync_workers` (default 2) concurrent rsync invocations instead of one,
each covering a disjoint slice of the files not yet on the NAS -- worthwhile
since the real bottleneck is usually network bandwidth, not CPU, and a
single stream often doesn't saturate it. The file list is computed once
ourselves, then split by a byte-balanced (not just file-count-balanced)
greedy assignment -- important because camera libraries mix small JPGs with
huge video/RAW files, so a naive round-robin split could load one worker
with all the big files while others sit idle. `--workers N` overrides the
config value per invocation. Start low; going much higher rarely helps once
you're bandwidth-bound and can even hurt via contention.

## Progress reporting

Metadata reading and the copy step print live progress (e.g.
`Importing: 342/1528 (22%) imported=340 skipped=2`) so a large card doesn't
sit silent for minutes. On a real terminal this is a single line that
overwrites itself in place; when stdout isn't an interactive terminal (piped,
redirected, or read by another front end/GUI wrapping the command) it instead
prints real, newline-terminated lines throttled to roughly every 5% of
progress -- `\r`-based overwriting only means anything to a real terminal, so
anything else would otherwise see nothing until the very end, when it'd all
arrive at once and look like the run jumped straight to 100% having done
nothing (`src/photo_importer/progress.py`). Every progress line is also
prefixed with a `[HH:MM:SS]` timestamp.

`sync` never uses rsync's own `-v`/`--progress` output -- on a large, mostly-
already-synced library those print a line for *every* file rsync considers,
including ones skipped because they're already there ("Skip existing
'<path>'"), which is enormous, useless noise. Progress instead comes from
`nas_sync.count_synced()`, an independent check of real on-disk state (how
many local files actually exist on the NAS right now, by path + size) --
`photo-importer sync` reports this periodically while a long sync is running
(`sync_with_heartbeat`) and always in its final summary line.

Each major operation also prints its own `started at` / `ended at (took ...)`
line (`src/photo_importer/timing.py`) -- metadata reading, import as a whole,
each NAS sync pass, and, in one-shot mode, the whole run (`One-shot (total)`,
a genuinely different number from import-time + sync-time since they
overlap -- see below). This is meant to make it easy to go back through a
log afterward and see exactly how long each part of a run took, not just
watch it live.

**One-shot mode specifically** runs import and the background NAS sync on
separate threads at the same time, so their progress lines could otherwise
land on top of each other -- literally concatenate onto the same line with no
separator if one thread's message arrives mid-write of the other's unfinished
`\r` line. Import and sync each get their own persistent status line instead
(`src/photo_importer/output.py`): two live-updating regions on a real
terminal (via ANSI cursor positioning), or two clearly `[import]`/`[sync]`
-prefixed streams of lines otherwise -- so it's always visually obvious both
are genuinely running concurrently, not just occasionally interleaved.

## One-shot mode

Running `photo-importer` with no subcommand starts a background NAS sync
immediately, running continuously and concurrently with import for as long
as import is still going -- not just once up front and once at the end.
Every ~15s it does another sync pass and reports how many local files are
confirmed on the NAS so far; once import finishes it does one final pass to
catch anything from the last window, so files land on the NAS progressively
as import produces them rather than all piling up into a single pass only
after import has completely finished (which would give little to no overlap
benefit on a run that's mostly new imports rather than backlog). This is
safe because sync is additive/idempotent (above) and because new files are
written to a hidden temp name and atomically renamed into place, and
`sync`'s rsync command excludes dotfiles (`--exclude=.*`) -- a concurrent
sync pass can never observe (and thus permanently skip, via
`--ignore-existing`) a partially-written file, no matter how often it runs
mid-import.

If `nas.mount_point` isn't already mounted, one-shot mode (and `sync`,
`migrate`, and `backup-sync` when running against the default NAS
destination, i.e. without `--dest`) tries to mount it automatically via
`open <nas.smb_url>` (uses a Keychain-saved login if you have one, or pops
Finder's own login dialog -- this tool never handles credentials itself),
waiting up to 10s. Set `nas.smb_url` in `config.yaml` to enable the
auto-mount attempt; leave it unset to skip straight to whatever happens when
still unmounted. What happens next differs by command: one-shot mode shows a
warning and a prompt to press Enter before continuing as an import-only pass
(NAS sync skipped for that run); `sync`, `migrate`, and `backup-sync` instead
fail with an error telling you to mount the share yourself.

Because one-shot mode acts automatically (including pushing to the NAS), it
also checks that the source actually looks like a camera card -- i.e. it has
a top-level `DCIM` folder, per the standard virtually every digital camera
and phone uses -- before doing anything else, even during `--dry-run`. If it
doesn't, you'll see a warning and a prompt to press Enter before continuing,
so a random USB drive doesn't accidentally get scanned/imported/synced into
the photo archive.

## `backup-sync`: syncing an active backup endpoint

If some other tool actively writes into a folder you don't control -- the
classic case is your NAS vendor's phone backup app dropping your phone's
photos/videos there, typically on the NAS itself, not sorted by capture date
-- `backup-sync` folds new files from there into the same `YYYY/MM/DD`
archive everything else lands in:

```
photo-importer backup-sync
photo-importer backup-sync --source /Volumes/NAS_SHARE/MobileBackup/YourPhone
photo-importer backup-sync --source /Volumes/NAS_SHARE/MobileBackup/Phone1 --source /Volumes/NAS_SHARE/MobileBackup/Phone2
photo-importer backup-sync --dry-run
```

Multiple backup endpoints are supported (`--source` is repeatable, or list
several under `backup.source_paths` in `config.yaml`) -- e.g. more than one
family member's phone backing up into its own folder. Each source is synced
into the same archive as an independent pass with its own cache entry, and
progress/summary output is broken out per source, with a combined total at
the end.

It's really `migrate copy` (see below) under a name that matches how you'll
actually use it: routinely re-run against a folder that only grows. That
folder is an active backup endpoint -- someone else's upload destination, not
yours to manage -- so `backup-sync` is copy-only and additive like `copy` --
it never deletes or moves anything out of it (no `purge`/`move` equivalent),
which also sidesteps having to know whether the backup app would re-upload a
file it found missing.

Unlike a plain `migrate copy`, `backup-sync` keeps a small persistent cache
(next to `config.yaml`, keyed by the source+destination pair) of files
already confirmed copied, so a routine re-run only has to resolve capture
dates and check archive presence for files that are actually new -- a
"nothing new since last time" pass costs almost nothing, even against a
large and growing backup folder, instead of re-scanning and re-resolving
metadata for the whole thing every time.

Set `backup.source_paths` in `config.yaml` to avoid passing `--source` every
time; by default the destination is the same NAS archive `sync` and
`migrate` use (`nas.mount_point` + `nas.remote_subpath`), auto-mounted the
same way (see above) if not already reachable -- pass `--dest` to target a
different folder instead (skips the NAS-mount check, same as `migrate
--dest`).

## `migrate`: consolidating an existing messy catalog

If you've got old photos/videos scattered across various folders/structures
(e.g. already sitting on the NAS from before this tool existed) that you want
folded into the same `YYYY/MM/DD` archive as everything else, `migrate` does
that -- as explicit steps you run whenever you choose to, never automatically:

```
photo-importer migrate copy  --source /Volumes/NAS_SHARE/OldPhotos
photo-importer migrate purge --source /Volumes/NAS_SHARE/OldPhotos
# or, for a folder you know is no longer receiving new uploads:
photo-importer migrate move  --source /Volumes/NAS_SHARE/OldPhotos
```

- **`copy`** is always safe and purely additive: it copies files into the
  archive if they're not already there (same temp-name-then-atomic-rename
  safety as `import`), and never touches the source. Safe to run against
  *any* folder -- including one still actively receiving new files (e.g.
  from a phone backup app) -- since nothing is ever deleted.
- **`purge`** is pure cleanup and never copies anything: it deletes a source
  file *only if* a matching copy (same resolved archive path, same size) is
  already confirmed present in the archive. Run this only against folders
  you know are no longer actively receiving new files -- the tool doesn't
  try to detect that for you, it's your call which folders are safe. (If a
  folder is still an active destination for some other backup/sync tool,
  deleting from it could cause that tool to treat the file as missing and
  re-send it -- `copy` alone already gets it into the archive without that
  risk; only run `purge` once you're sure a folder is done receiving new
  uploads.)
- **`move`** is a faster alternative to running `copy` then `purge` back to
  back, for a folder you've already confirmed is inactive -- same judgement
  call as `purge`. Source and the archive are normally the same filesystem
  (the archive is always a subpath of the NAS mount), so files are renamed
  straight into place instead of copied and separately deleted -- no bytes
  need to cross SMB at all for a rename, just a directory-entry update.
  Falls back to copy+delete only if source and archive ever turn out to be
  on different filesystems. Unlike `copy`, `move` deletes from source
  immediately for every file it touches (including ones it just moved in),
  so it carries the same active-upload risk as `purge` -- don't point it at
  a folder still receiving new uploads.

All three scan recursively (any nested folder structure), and process the
entire backlog found by that scan in one invocation -- metadata is read once
per run, not once per file, so there's no benefit to splitting a large
catalog across multiple calls. They're naturally resumable if interrupted --
just re-run the same command to continue; each run figures out what's still
pending fresh rather than tracking a separate cursor/checkpoint file. All
three refuse to run (before touching anything) if `--source`
overlaps with the archive destination itself, and support `--dry-run`.

By default the destination is the NAS mount + `nas.remote_subpath` from
config, auto-mounted (see the one-shot section above) if not already
reachable. Pass `--dest` to consolidate into any other folder instead --
this skips the NAS-mount check entirely, so
`migrate` can run standalone (no `config.yaml`, no NAS) against any two
folders on disk:

```
photo-importer migrate copy --source ~/OldPhotos --dest ~/Archive
```

Note `move`'s "no bytes cross SMB" shortcut only holds when `--dest` stays
on the same filesystem as `--source` (true by default, since the archive is
a subpath of the NAS mount); pointed elsewhere, it transparently falls back
to copy+delete per file.

## Platform support

Built and tested primarily on macOS -- that's the only platform actually
exercised end to end. Linux and Windows are supported to the extent described
below, on a best-effort basis:

- **Source auto-detection** (`source: auto`): macOS checks `/Volumes`; Linux
  checks `/run/media/$USER`, `/media/$USER`, and `/media`; Windows enumerates
  drive letters and picks ones the OS reports as removable media. On any
  platform, `--source /explicit/path` (or a drive letter/UNC path on Windows)
  always works regardless of auto-detection.
- **NAS auto-mount** (the `open smb://...` step used by one-shot mode,
  `sync`, `migrate`, and `backup-sync`) is macOS-only -- Linux and Windows
  have no equivalent single-command auto-mount, so on those platforms every
  command just checks whether `nas.mount_point` is already reachable and
  warns/prompts (one-shot mode) or errors (`sync`/`migrate`/`backup-sync`) if
  not. Mount the share yourself first (a cifs/GVFS mount on Linux, a mapped
  drive letter or UNC path on Windows).
- **rsync progress flags** are chosen per-OS (`--progress` for macOS's bundled
  openrsync, `--info=progress2` for the modern GNU rsync Linux typically
  ships -- assumed for Windows too, since whatever rsync port you install
  there is usually GNU-compatible).
- Core logic (scanning, EXIF/mtime dates, dedup index, atomic temp-then-rename
  copies) uses only cross-platform stdlib path handling and isn't
  OS-specific.

If something's broken on Linux or Windows, it's very possibly this lack of
live testing rather than a fundamental design issue -- file paths/behavior
observed on that platform are the most useful thing to report.
