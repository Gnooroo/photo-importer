from photo_importer.index import ImportIndex


def test_record_and_contains(tmp_path):
    index = ImportIndex(str(tmp_path))
    assert not index.contains("photo.jpg", 100)

    index.record("photo.jpg", 100, str(tmp_path / "2024/03/15/photo.jpg"), "2024-03-15T00:00:00")

    assert index.contains("photo.jpg", 100)
    assert not index.contains("photo.jpg", 200)  # different size == different file


def test_persists_across_instances(tmp_path):
    index = ImportIndex(str(tmp_path))
    index.record("clip.mp4", 5000, str(tmp_path / "2024/01/01/clip.mp4"), "2024-01-01T00:00:00")
    index.save()

    reloaded = ImportIndex(str(tmp_path))
    assert reloaded.contains("clip.mp4", 5000)
