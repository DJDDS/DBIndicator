import datetime as dt


def test_compression_temp_archive_is_not_shared_fixed_name(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("{\"tick\":1}\n", encoding="utf-8")

    opened = []
    original = v121_backup.gzip.open
    def capture(filename, mode, *args, **kwargs):
        opened.append(str(filename))
        return original(filename, mode, *args, **kwargs)
    monkeypatch.setattr(v121_backup.gzip, "open", capture)

    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))
    assert opened
    assert not opened[0].endswith(".jsonl.gz.tmp")
