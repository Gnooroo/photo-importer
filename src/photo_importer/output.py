"""Thread-safe stdout coordination.

Two concerns, both arising from one-shot mode running import (main thread)
and the background NAS sync (a separate thread) at the same time:

1. Without coordination, a plain print() from one thread can land in the
   middle of the other thread's unfinished \r-progress line, concatenating
   two unrelated messages onto one line with no separator.
2. Even once that's fixed, a single shared stream can't show "import is at
   40%" and "sync is at pass 3" at the same time -- one message always
   overwrites or follows the other, so it's unclear both are actually
   running concurrently.

`Regions` solves both: import and sync each get their own persistent status
line (real cursor-addressed lines on a terminal; clearly name-prefixed lines
as a non-tty fallback, matching Progress's own tty/non-tty split). A
contextvar (`set_region`/`get_region`) lets Progress/timed() -- which don't
otherwise know anything about one-shot's concurrency -- automatically route
into whichever region is active on the calling thread, with zero change to
their call sites. Standalone import/sync/migrate never call set_region(), so
get_region() returns None there and everything behaves exactly as before
(single shared stream via print_line).
"""

from __future__ import annotations

import contextvars
import sys
import threading

_current_region: contextvars.ContextVar = contextvars.ContextVar("current_region", default=None)

_lock = threading.Lock()
_mid_line = False


def print_line(message: str = "", end: str = "\n", flush: bool = True) -> None:
    """Default (no active region) output path: a single shared stream with
    the same mid-line protection as Regions, for callers outside one-shot's
    concurrent phase (standalone import/sync/migrate).
    """
    global _mid_line
    with _lock:
        if _mid_line and end != "":
            print()
        print(message, end=end, flush=flush)
        _mid_line = end == ""


class Region:
    def __init__(self, regions: "Regions", name: str):
        self._regions = regions
        self._name = name

    def update(self, message: str) -> None:
        self._regions._write(self._name, message)

    def __enter__(self) -> "Region":
        token = _current_region.set(self)
        self._token = token
        return self

    def __exit__(self, *exc_info) -> None:
        _current_region.reset(self._token)


class Regions:
    """Fixed set of named, independently-updating status lines."""

    def __init__(self, names: list[str], is_tty: bool | None = None):
        self.names = names
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self._lock = threading.Lock()
        self._reserved = False

    def region(self, name: str) -> Region:
        return Region(self, name)

    def _write(self, name: str, message: str) -> None:
        with self._lock:
            if not self.is_tty:
                print(f"[{name}] {message}", flush=True)
                return
            if not self._reserved:
                for _ in self.names:
                    print()
                self._reserved = True
            idx = self.names.index(name)
            up = len(self.names) - idx
            # CPL (cursor to start of line, N lines up) -> EL (erase line) ->
            # write -> CNL (cursor to start of line, N lines down), landing
            # back where we started so the next unrelated print (if any)
            # lands below the whole reserved block, not inside it.
            sys.stdout.write(f"\x1b[{up}F\x1b[2K{message}\x1b[{up}E")
            sys.stdout.flush()

    def close(self) -> None:
        with self._lock:
            if self.is_tty and self._reserved:
                print()  # move past the reserved block for whatever prints next
            self._reserved = False


def get_region() -> Region | None:
    return _current_region.get()


def set_region(region: Region | None):
    """Sets the active region for Progress/timed() on the calling thread
    only (contextvars are thread-local by default -- a background thread
    must call this itself, it won't inherit the main thread's region).
    Returns a token; prefer using the region as a context manager instead.
    """
    return _current_region.set(region)


def report(message: str) -> None:
    """Print a one-off status message, routed into the active region if
    one is set (main-thread warnings before regions start, or anything in
    a non-concurrent command, fall back to the plain shared stream).
    """
    region = get_region()
    if region is not None:
        region.update(message)
    else:
        print_line(message)
