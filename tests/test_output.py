from unittest.mock import patch

from photo_importer import output
from photo_importer.output import Regions, get_region, print_line


def test_print_line_inserts_newline_before_foreign_message_after_unfinished_line():
    printed = []
    output._mid_line = False
    try:
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append((a, k))):
            print_line("Importing: 1/10", end="")  # simulates an unfinished \r progress line
            print_line("Background sync: pass 1 complete")  # a foreign, newline-terminated message

        # the foreign message must not land directly after the unfinished line:
        # a bare print() (finishing the previous line) must appear in between
        assert printed[0] == (("Importing: 1/10",), {"end": "", "flush": True})
        assert printed[1] == ((), {})  # the inserted "finish the line" newline
        assert printed[2] == (("Background sync: pass 1 complete",), {"end": "\n", "flush": True})
    finally:
        output._mid_line = False


def test_print_line_no_extra_newline_when_previous_line_was_already_finished():
    printed = []
    output._mid_line = False
    try:
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append((a, k))):
            print_line("first message")
            print_line("second message")

        assert len(printed) == 2
    finally:
        output._mid_line = False


def test_non_tty_regions_prefix_each_message_with_its_name():
    regions = Regions(["import", "sync"], is_tty=False)
    printed = []

    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
        regions.region("import").update("Importing: 1/10")
        regions.region("sync").update("Background sync: pass 1")

    assert printed == ["[import] Importing: 1/10", "[sync] Background sync: pass 1"]


def test_tty_regions_use_ansi_cursor_positioning_and_reserve_lines_once():
    regions = Regions(["import", "sync"], is_tty=True)
    written = []

    with patch("sys.stdout.write", side_effect=lambda s: written.append(s)), \
         patch("sys.stdout.flush"), \
         patch("builtins.print") as mock_print:
        regions.region("import").update("Importing: 1/10")
        regions.region("sync").update("Background sync: pass 1")
        regions.region("import").update("Importing: 2/10")

    # reserved exactly once (2 blank lines for 2 regions), not once per update
    assert mock_print.call_count == 2
    # import is region index 0 of 2 -> 2 lines up; sync is index 1 -> 1 line up
    assert written[0] == "\x1b[2F\x1b[2KImporting: 1/10\x1b[2E"
    assert written[1] == "\x1b[1F\x1b[2KBackground sync: pass 1\x1b[1E"
    assert written[2] == "\x1b[2F\x1b[2KImporting: 2/10\x1b[2E"


def test_region_context_manager_sets_and_clears_current_region():
    regions = Regions(["a"], is_tty=False)
    assert get_region() is None
    with regions.region("a") as r:
        assert get_region() is r
    assert get_region() is None


def test_report_uses_active_region_when_set():
    regions = Regions(["only"], is_tty=False)
    printed = []

    with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0])):
        with regions.region("only"):
            output.report("hello")

    assert printed == ["[only] hello"]


def test_report_falls_back_to_print_line_when_no_region_active():
    output._mid_line = False
    printed = []
    try:
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")):
            output.report("hello")

        assert printed == ["hello"]
    finally:
        output._mid_line = False
