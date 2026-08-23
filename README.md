# photo-importer

A small CLI for importing photos/videos off an SD card (or any source folder)
into a local library organized by capture date (`YYYY/MM/DD`), skipping files
that have already been imported, plus a separate command to `rsync` the local
library to a NAS SMB share.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires [exiftool](https://exiftool.org/) for accurate capture-date detection
(`brew install exiftool`); without it, file modification time is used instead.

Copy the config template and fill in your paths:

```
cp config.example.yaml config.yaml
```

## Usage

Import from an auto-detected mounted volume (e.g. an inserted SD card):

```
photo-importer import
```

Import from an explicit path, or preview without copying:

```
photo-importer import --source /Volumes/SDCARD
photo-importer import --dry-run
```

Sync the local library to your NAS (the SMB share must already be mounted --
via Finder's "Connect to Server", or `mount_smbfs`):

```
photo-importer sync
```

Both commands read `config.yaml` from the current directory (or
`~/.config/photo-importer/config.yaml`); pass `--config path/to/file.yaml` to
use a different one. `--source` and `--local-root` on `import`, and
`--local-root` on `sync`, override the config file.

## How dedup works

Each import records `filename:size -> destination path` in
`<local_root>/.photo_importer_index.json`. Re-running against the same or an
overlapping source skips anything already in the index, so you can import
incrementally from a card without worrying about double-copying.

## Local library vs. NAS: NAS is the archive

`sync` is a one-way, additive push
(`rsync -a --ignore-existing --inplace --info=progress2`, no `--delete`): it
never removes or overwrites anything on the NAS, it only copies files that
aren't there yet. This is intentional -- the NAS is meant to hold everything
ever imported, while the local library is disposable and can be pruned to
save space once its contents are confirmed synced. A file already present on
the NAS at the same relative path is treated as a duplicate and skipped,
regardless of whether the local copy is still around.

If sync feels slow, it's almost always the network, not rsync: a Wi-Fi link
to the NAS is the usual bottleneck for large photo/video libraries, and a
wired Ethernet connection (or an NAS-side rsync/SSH service instead of an SMB
mount, if available) will generally be much faster.
