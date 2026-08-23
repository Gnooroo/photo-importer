from photo_importer.scanner import scan


def test_scan_filters_by_extension(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(b"data")
    (tmp_path / "clip.mp4").write_bytes(b"data")
    (tmp_path / "notes.txt").write_bytes(b"data")
    sub = tmp_path / "DCIM"
    sub.mkdir()
    (sub / "nested.png").write_bytes(b"data")

    results = scan(str(tmp_path), {".jpg", ".mp4", ".png"})
    names = sorted(p.name for p in results)

    assert names == sorted(["nested.png", "photo.jpg", "clip.mp4"])


def test_scan_ignores_hidden_files(tmp_path):
    (tmp_path / ".hidden.jpg").write_bytes(b"data")
    (tmp_path / "visible.jpg").write_bytes(b"data")

    results = scan(str(tmp_path), {".jpg"})

    assert [p.name for p in results] == ["visible.jpg"]


def test_scan_returns_empty_for_no_matches(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"data")

    results = scan(str(tmp_path), {".jpg"})

    assert results == []
