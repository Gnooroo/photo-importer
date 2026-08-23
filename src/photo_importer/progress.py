"""Live progress reporting that degrades gracefully for non-interactive
consumers.

A real terminal interprets "\r" as "return to the start of the line," which
is what makes a single line update in place. Anything that isn't a real
terminal -- a pipe, a log file, a GUI/front end reading the subprocess's
stdout -- just sees "\r" as an ordinary character with no special meaning.
Since progress updates never emit a real newline until the very end, such a
consumer has nothing to show until the whole run finishes, at which point
everything printed arrives at once and looks like it jumped straight to
100% (this is the bug: it isn't that nothing happened, it's that nothing
was *visible* until the end). Progress prints real, newline-terminated
lines for that case instead, throttled so a large batch doesn't spam
hundreds of lines.
"""

from __future__ import annotations

import sys

from .output import get_region, print_line
from .timing import now_time_str


class Progress:
    def __init__(self, total: int, throttle_pct: int = 5, is_tty: bool | None = None):
        self.total = total
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self.throttle_pct = max(1, throttle_pct)
        self._last_bucket = -1

    def update(self, message: str, done: int) -> None:
        if self.total <= 0:
            return
        message = f"[{now_time_str()}] {message}"
        region = get_region()

        if self.is_tty:
            if region is not None:
                region.update(message)
            else:
                print_line(f"\r{message}", end="")
            return

        pct = done * 100 // self.total
        bucket = pct // self.throttle_pct
        if bucket != self._last_bucket or done == self.total:
            if region is not None:
                region.update(message)
            else:
                print_line(message)
            self._last_bucket = bucket

    def done(self) -> None:
        if get_region() is not None:
            return  # region stays open -- its owner closes it, not each Progress instance
        if self.is_tty:
            print_line()
