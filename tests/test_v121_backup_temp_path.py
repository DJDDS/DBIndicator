import datetime as dt


def test_compression_uses_unique_temp_archive_name(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")

    seen = []
    original = v121_backup.gzip.open

    def capture(filename, mode, *args, **kwargs):
        seen.append(str(filename))
        return original(filename, mode, *args, **kwargs)

    monkeypatch.setattr(v121_backup.gzip, "open", capture)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))

    assert len(seen) == 1
    assert seen[0].endswith(".tmp")
    assert seen[0] != str(root / "2026-09-16_micro.jsonl.gz.tmp")
