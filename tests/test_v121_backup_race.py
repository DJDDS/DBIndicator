import datetime as dt


def test_compression_does_not_use_shared_fixed_gzip_temp(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("{\"tick\":1}\n", encoding="utf-8")
    forbidden = str(root / "2026-09-16_micro.jsonl.gz.tmp")
    real_open = v121_backup.gzip.open

    def guarded_open(path, mode, *args, **kwargs):
        if str(path) == forbidden:
            raise FileNotFoundError("simulated production shared-temp collision")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(v121_backup.gzip, "open", guarded_open)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))

    archive = root / "2026-09-16_micro.jsonl.gz"
    assert archive.exists()
    assert not (root / "2026-09-16_micro.jsonl").exists()
