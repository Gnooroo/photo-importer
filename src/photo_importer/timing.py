"""Wall-clock timing for logs, so operations (import, sync, metadata reads)
can be measured after the fact rather than just watched live.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime

from .output import report


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_time_str() -> str:
    return datetime.now().strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@contextmanager
def timed(label: str):
    """Prints "{label} started at ..." on entry and "{label} ended at ...
    (took ...)" on exit -- always, including when the block raises, so a
    failed operation's duration is still visible. Routes through the active
    output region if one is set on the calling thread (see output.py),
    otherwise the plain shared stream.
    """
    start = time.monotonic()
    report(f"{label} started at {now_str()}")
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        report(f"{label} ended at {now_str()} (took {format_duration(elapsed)})")
