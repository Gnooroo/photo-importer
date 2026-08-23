from unittest.mock import patch

import pytest

from photo_importer.timing import format_duration, timed


def test_format_duration_seconds_only():
    assert format_duration(45) == "45s"


def test_format_duration_minutes_and_seconds():
    assert format_duration(125) == "2m 5s"


def test_format_duration_hours_minutes_seconds():
    assert format_duration(3725) == "1h 2m 5s"


def test_format_duration_zero():
    assert format_duration(0) == "0s"


def test_timed_prints_start_and_end():
    printed = []
    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")), \
         patch("time.monotonic", side_effect=[100.0, 142.0]), \
         patch("photo_importer.timing.now_str", return_value="2026-08-23 12:00:00"):
        with timed("Import"):
            pass

    assert len(printed) == 2
    assert printed[0] == "Import started at 2026-08-23 12:00:00"
    assert printed[1] == "Import ended at 2026-08-23 12:00:00 (took 42s)"


def test_timed_prints_end_even_on_exception():
    printed = []
    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")):
        with pytest.raises(ValueError):
            with timed("Sync"):
                raise ValueError("boom")

    assert any("Sync ended at" in p for p in printed)
