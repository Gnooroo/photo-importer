"""A lightweight local index of already-imported files, keyed by filename+size,
so re-running an import against the same source doesn't need to rehash or
re-walk the whole destination tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import app_state_dir

INDEX_FILENAME = "import_index.json"


class ImportIndex:
    """Stored under app_state_dir() (next to config.yaml), not inside
    local_root -- the library is meant to hold only real archive content,
    since (unlike before) it now gets rsynced to the NAS as-is including
    hidden files (see nas_sync._is_sync_excluded). One shared file can hold
    the index for multiple libraries, keyed by each local_root's resolved
    absolute path; saving re-reads and merges rather than overwriting so
    concurrent libraries don't clobber each other's index.
    """

    def __init__(self, local_root: str):
        self.path = app_state_dir() / INDEX_FILENAME
        self._root_key = str(Path(local_root).expanduser().resolve())
        self._entries: dict[str, dict] = self._load_all().get(self._root_key, {})

    def _load_all(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_all()
        data[self._root_key] = self._entries
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    @staticmethod
    def _key(filename: str, size: int) -> str:
        return f"{filename}:{size}"

    def contains(self, filename: str, size: int) -> bool:
        return self._key(filename, size) in self._entries

    def record(self, filename: str, size: int, dest_path: str, capture_date: str) -> None:
        self._entries[self._key(filename, size)] = {
            "dest_path": dest_path,
            "capture_date": capture_date,
        }
