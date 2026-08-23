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

    paths = cmd[5:]
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


def test_iter_capture_date_batches_yields_multiple_ordered_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "BATCH_SIZE", 2)
    files = []
    for i in range(5):
        f = tmp_path / f"with_exif_{i}.jpg"
        f.write_bytes(b"data")
        files.append(f)

    with patch.object(metadata, "exiftool_available", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run):
        batches = list(metadata.iter_capture_date_batches(files))

    assert [len(b) for b in batches] == [2, 2, 1]
    # batches drain in the same order paths were given, and each batch's
    # keys preserve that same relative order too.
    assert [p for batch in batches for p in batch] == files
    for batch in batches:
        for path, date in batch.items():
            assert date == datetime(2024, 3, 15, 10, 20, 30)


def test_iter_capture_date_batches_falls_back_to_mtime_without_exiftool(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "BATCH_SIZE", 2)
    files = []
    for i in range(3):
        f = tmp_path / f"anything_{i}.jpg"
        f.write_bytes(b"data")
        files.append(f)

    with patch.object(metadata, "exiftool_available", return_value=False):
        batches = list(metadata.iter_capture_date_batches(files))

    assert [len(b) for b in batches] == [2, 1]
    for f in files:
        expected = datetime.fromtimestamp(f.stat().st_mtime)
        matching = [batch[f] for batch in batches if f in batch]
        assert matching == [expected]


def test_get_capture_dates_matches_union_of_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "BATCH_SIZE", 2)
    files = []
    for i in range(5):
        f = tmp_path / f"with_exif_{i}.jpg"
        f.write_bytes(b"data")
        files.append(f)

    with patch.object(metadata, "exiftool_available", return_value=True), \
         patch("subprocess.run", side_effect=_fake_run):
        dates = metadata.get_capture_dates(files)
        batches = list(metadata.iter_capture_date_batches(files))

    expected = {}
    for batch in batches:
        expected.update(batch)
    assert dates == expected
