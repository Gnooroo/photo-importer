import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from photo_importer import metadata


def _fake_run(cmd, capture_output, text, check):
    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    paths = cmd[4:]
    entries = []
    for p in paths:
        if "with_exif" in p:
            entries.append({"SourceFile": p, "DateTimeOriginal": "2024:03:15 10:20:30"})
        elif "create_date_only" in p:
            entries.append({"SourceFile": p, "CreateDate": "2023:01:02 03:04:05"})
        else:
            entries.append({"SourceFile": p})
    result = Result()
    result.stdout = json.dumps(entries)
    return result


def test_uses_exif_date_when_available(tmp_path):
    f = tmp_path / "with_exif.jpg"
    f.write_bytes(b"data")

    with patch.object(metadata, "exiftool_available", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run):
        dates = metadata.get_capture_dates([f])

    assert dates[f] == datetime(2024, 3, 15, 10, 20, 30)


def test_falls_back_to_create_date(tmp_path):
    f = tmp_path / "create_date_only.mp4"
    f.write_bytes(b"data")

    with patch.object(metadata, "exiftool_available", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run):
        dates = metadata.get_capture_dates([f])

    assert dates[f] == datetime(2023, 1, 2, 3, 4, 5)


def test_falls_back_to_mtime_when_no_exif_tags(tmp_path):
    f = tmp_path / "no_tags.jpg"
    f.write_bytes(b"data")

    with patch.object(metadata, "exiftool_available", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run):
        dates = metadata.get_capture_dates([f])

    expected = datetime.fromtimestamp(f.stat().st_mtime)
    assert dates[f] == expected


def test_falls_back_to_mtime_when_exiftool_missing(tmp_path):
    f = tmp_path / "anything.jpg"
    f.write_bytes(b"data")

    with patch.object(metadata, "exiftool_available", return_value=False):
        dates = metadata.get_capture_dates([f])

    expected = datetime.fromtimestamp(f.stat().st_mtime)
    assert dates[f] == expected
