"""Generic (size, mtime) verification cache shared by nas_sync.py (local
library -> NAS mirror) and migrate.py (an arbitrary source folder -> the
date-organized archive, e.g. backup-sync) -- both need the same trick to keep
a repeat "what's still pending" pass fast: once a file's (size, mtime) has
been recorded as confirmed present at some destination, an unchanged file on
a later pass is trusted without re-checking the destination side at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import app_state_dir


class PathStateCache:
    """Persists, per relative path, the (size, mtime) last confirmed present
    at some destination -- keyed by the resolved absolute path of `root`
    (the source tree being walked) so one shared file can hold state for
    multiple roots without them clobbering each other (saving re-reads and
    merges rather than overwriting). Scoped to `dest` too: if the
    destination changes, previously-recorded verifications don't mean
    anything against the new one, so a mismatched entry is discarded rather
    than trusted.

    Lives under app_state_dir(), not inside `root` itself -- callers walk
    `root` verbatim (rsync mirror, or a raw backup-folder scan), so a
    dotfile living in there would otherwise need excluding from every pass.

    A file's mtime changing is exactly the signal that it needs
    re-verification -- both nas_sync's local library and migrate's
    typical sources (camera/phone backups) are effectively write-once, so
    in steady state mtimes are stable and this cache stays valid
    indefinitely; touching or re-writing a file naturally invalidates just
    that one entry.
    """

    def __init__(self, filename: str, root: str, dest: str):
        self.path = app_state_dir() / filename
        self._root_key = str(Path(root).expanduser().resolve())
        self._dest = dest
        self._entries: dict[str, list] = {}
        self._dirty = False
        root_data = self._load_all().get(self._root_key, {})
        if root_data.get("dest") == dest:
            self._entries = root_data.get("entries", {})

    def _load_all(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def is_verified(self, rel_path: str, size: int, mtime: float) -> bool:
        entry = self._entries.get(rel_path)
        return entry is not None and entry[0] == size and entry[1] == mtime

    def mark_verified(self, rel_path: str, size: int, mtime: float) -> None:
        entry = [size, mtime]
        if self._entries.get(rel_path) != entry:
            self._entries[rel_path] = entry
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_all()
        data[self._root_key] = {"dest": self._dest, "entries": self._entries}
        with open(self.path, "w") as f:
            json.dump(data, f)
        self._dirty = False
