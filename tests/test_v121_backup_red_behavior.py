import datetime as dt


def test_compression_avoids_known_colliding_temp_path(tmp_path, monkeypatch):
    from app import v121_backup
    root = tmp_path / "index_vol"; root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("x\n", encoding="utf-8")
    forbidden = str(root / "2026-09-16_micro.jsonl.gz.tmp")
    real = v121_backup.gzip.open
    def guarded(path, mode, *args, **kwargs):
        if str(path) == forbidden:
            raise FileExistsError("known shared temp collision")
        return real(path, mode, *args, **kwargs)
    monkeypatch.setattr(v121_backup.gzip, "open", guarded)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))
