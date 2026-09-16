import datetime as dt


def test_production_traceback_shared_tmp_path_is_not_used(tmp_path, monkeypatch):
    from app import v121_backup
    root = tmp_path / "index_vol"
    root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("x\n", encoding="utf-8")
    fixed = str(root / "2026-09-16_micro.jsonl.gz.tmp")
    original = v121_backup.gzip.open
    def guarded(path, mode, *args, **kwargs):
        assert str(path) != fixed, "production race: shared .gz.tmp path reused"
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(v121_backup.gzip, "open", guarded)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))
