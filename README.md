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

**`sync`** -- push the local library to the NAS on its own (the SMB share
must already be mounted -- via Finder's "Connect to Server", or
`mount_smbfs`; unlike one-shot mode, this does not try to auto-mount):

```
photo-importer sync
```

All three read `config.yaml` from the current directory (or
`~/.config/photo-importer/config.yaml`); pass `--config path/to/file.yaml` to
use a different one. `--source` and `--local-root` (one-shot and `import`),
and `--local-root` (`sync`), override the config file.

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

Sync is a one-way, additive push (`rsync -av --ignore-existing --inplace`, no
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

## Progress reporting

Both metadata reading and the copy step print live progress (e.g.
`Importing: 342/1528 (22%) imported=340 skipped=2`) so a large card doesn't
sit silent for minutes. On a real terminal this is a single line that
overwrites itself in place; when stdout isn't an interactive terminal (piped,
redirected, or read by another front end/GUI wrapping the command) it instead
prints real, newline-terminated lines throttled to roughly every 5% of
progress -- `\r`-based overwriting only means anything to a real terminal, so
anything else would otherwise see nothing until the very end, when it'd all
arrive at once and look like the run jumped straight to 100% having done
nothing (`src/photo_importer/progress.py`). `sync` streams rsync's own
per-file `-v --progress` output (rsync makes its own tty-vs-not decisions).
In one-shot mode, the background sync (running concurrently with the import
loop) suppresses rsync's own per-file output -- so it doesn't fight with the
import progress line on the same terminal -- but still prints a "starting"
line, a "still running... (Ns elapsed)" heartbeat every 15s for as long as
it's active, and a clear complete/failed line when it finishes, so a large
backlog sync never looks like it's silently hung. The catch-up sync that
runs after import finishes (when nothing else is printing) is verbose as
usual.

Every progress line is also prefixed with a `[HH:MM:SS]` timestamp, and each
major operation prints its own `started at` / `ended at (took ...)` lines
(`src/photo_importer/timing.py`) -- metadata reading, import as a whole,
each NAS sync (labeled `Sync`, `Background NAS sync`, or `Catch-up NAS sync`
depending on which one it is), and, in one-shot mode, the whole run
(`One-shot (total)`, which is a genuinely different number from
import-time + sync-time since they overlap). This is meant to make it easy
to go back through a log afterward and see exactly how long each part of a
run took, not just watch it live.

## One-shot mode

Running `photo-importer` with no subcommand starts a background sync of the
local library's current contents *before* copying anything from the source,
so that backlog transfer overlaps with reading/copying off the card. Once
import finishes, a final synchronous sync pass catches whatever this run just
added. This is safe because sync is additive/idempotent (above) and because
new files are written to a hidden temp name and atomically renamed into place
-- a concurrent sync can never observe (and thus permanently skip, via
`--ignore-existing`) a partially-written file.

If `nas.mount_point` isn't already mounted, one-shot mode tries to mount it
automatically via `open <nas.smb_url>` (uses a Keychain-saved login if you
have one, or pops Finder's own login dialog -- this tool never handles
credentials itself), waiting up to 10s. If it still isn't mounted, you'll see
a warning and a prompt to press Enter before the run continues as an
import-only pass (NAS sync skipped for that run). Set `nas.smb_url` in
`config.yaml` to enable the auto-mount attempt; leave it unset to skip
straight to the warning/prompt when unmounted.

Because one-shot mode acts automatically (including pushing to the NAS), it
also checks that the source actually looks like a camera card -- i.e. it has
a top-level `DCIM` folder, per the standard virtually every digital camera
and phone uses -- before doing anything else, even during `--dry-run`. If it
doesn't, you'll see a warning and a prompt to press Enter before continuing,
so a random USB drive doesn't accidentally get scanned/imported/synced into
the photo archive.

## `migrate`: consolidating an existing messy catalog

If you've got old photos/videos scattered across various folders/structures
(e.g. already sitting on the NAS from before this tool existed) that you want
folded into the same `YYYY/MM/DD` archive as everything else, `migrate` does
that -- as two separate, explicit steps you run whenever you choose to,
never automatically:

```
photo-importer migrate copy  --source /Volumes/NAS_SHARE/OldPhotos
photo-importer migrate purge --source /Volumes/NAS_SHARE/OldPhotos
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

Both scan recursively (any nested folder structure), process in batches
(`--batch-size`, default 200 or `migrate.batch_size` in config) so a huge
catalog doesn't have to be done in one sitting, and are naturally resumable
-- just re-run the same command to continue; each run figures out what's
still pending fresh rather than tracking a separate cursor/checkpoint file.
Both refuse to run (before touching anything) if `--source` overlaps with
the archive destination itself, and support `--dry-run`.

## Platform support

Built and tested primarily on macOS -- that's the only platform actually
exercised end to end. Linux and Windows are supported to the extent described
below, on a best-effort basis:

- **Source auto-detection** (`source: auto`): macOS checks `/Volumes`; Linux
  checks `/run/media/$USER`, `/media/$USER`, and `/media`; Windows enumerates
  drive letters and picks ones the OS reports as removable media. On any
  platform, `--source /explicit/path` (or a drive letter/UNC path on Windows)
  always works regardless of auto-detection.
- **NAS auto-mount** (one-shot mode's `open smb://...` step) is macOS-only --
  Linux and Windows have no equivalent single-command auto-mount, so one-shot
  mode just checks whether `nas.mount_point` is already reachable and
  warns/prompts if not, same as everywhere else. Mount the share yourself
  first (a cifs/GVFS mount on Linux, a mapped drive letter or UNC path on
  Windows).
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
