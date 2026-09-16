import datetime as dt


def test_compress_completed_day_does_not_open_fixed_gz_tmp(tmp_path, monkeypatch):
    from app import v121_backup
    root = tmp_path / "index_vol"; root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("x\n", encoding="utf-8")
    fixed = root / "2026-09-16_micro.jsonl.gz.tmp"
    opened = []
    real = v121_backup.gzip.open
    def capture(path, mode, *args, **kwargs):
        opened.append(str(path))
        return real(path, mode, *args, **kwargs)
    monkeypatch.setattr(v121_backup.gzip, "open", capture)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))
    assert str(fixed) not in opened
