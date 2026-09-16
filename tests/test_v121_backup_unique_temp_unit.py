import datetime as dt


def test_compression_allocates_unique_temp_file(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    (root / "2026-09-16_micro.jsonl").write_text("{\"tick\":1}\n", encoding="utf-8")

    calls = []
    original = getattr(v121_backup, "tempfile", None)
    assert original is not None, "compression must use tempfile for collision-free staging"

    real_mkstemp = original.mkstemp
    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(original, "mkstemp", capture)
    v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))
    assert calls
