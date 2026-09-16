import datetime as dt


def test_v121_compression_never_uses_production_collision_temp_name(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")
    collision = root / "2026-09-16_micro.jsonl.gz.tmp"

    original = v121_backup.gzip.open

    def reject_collision(filename, mode, *args, **kwargs):
        if str(filename) == str(collision):
            raise RuntimeError("shared gzip temp path collision")
        return original(filename, mode, *args, **kwargs)

    monkeypatch.setattr(v121_backup.gzip, "open", reject_collision)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))

    assert (root / "2026-09-16_micro.jsonl.gz").exists()
