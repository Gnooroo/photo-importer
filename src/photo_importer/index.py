"""A lightweight local index of already-imported files, keyed by filename+size,
so re-running an import against the same source doesn't need to rehash or
re-walk the whole destination tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

INDEX_FILENAME = ".photo_importer_index.json"


class ImportIndex:
    def __init__(self, local_root: str):
        self.path = Path(local_root) / INDEX_FILENAME
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            with open(self.path) as f:
                self._entries = json.load(f)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._entries, f, indent=2, sort_keys=True)

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
