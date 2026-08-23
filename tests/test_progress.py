from unittest.mock import patch

from photo_importer.progress import Progress


def test_tty_mode_overwrites_in_place():
    progress = Progress(total=10, is_tty=True)
    printed = []

    with patch("builtins.print", side_effect=lambda *a, **k: printed.append((a, k))):
        progress.update("step 1", 1)
        progress.update("step 2", 2)
        progress.done()

    assert printed[0] == (("\rstep 1",), {"end": "", "flush": True})
    assert printed[1] == (("\rstep 2",), {"end": "", "flush": True})
    assert printed[2] == ((), {})  # done() prints a bare newline on a tty


def test_non_tty_mode_prints_real_lines_throttled():
    progress = Progress(total=100, is_tty=False, throttle_pct=10)
    printed = []

    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
        for i in range(1, 101):
            progress.update(f"step {i}", i)
        progress.done()

    # ~10 lines (one per 10% bucket), not 100 -- and each is a real newline-terminated print
    assert 8 <= len(printed) <= 12
    assert printed[-1] == "step 100"


def test_non_tty_mode_always_reports_final_update():
    progress = Progress(total=7, is_tty=False, throttle_pct=50)
    printed = []

    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
        for i in range(1, 8):
            progress.update(f"step {i}", i)

    assert printed[-1] == "step 7"


def test_non_tty_done_does_not_print_extra_newline():
    progress = Progress(total=10, is_tty=False)
    with patch("builtins.print") as mock_print:
        progress.done()
    mock_print.assert_not_called()


def test_zero_total_never_prints():
    progress = Progress(total=0, is_tty=True)
    with patch("builtins.print") as mock_print:
        progress.update("anything", 0)
    mock_print.assert_not_called()
