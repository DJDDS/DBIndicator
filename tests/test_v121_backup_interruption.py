import datetime as dt

import pytest


def test_compression_failure_preserves_source_and_cleans_temp(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")

    def fail_copy(*args, **kwargs):
        raise OSError("simulated compression failure")

    monkeypatch.setattr(v121_backup.shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="simulated compression failure"):
        v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))

    assert source.exists()
    assert not (root / "2026-09-16_micro.jsonl.gz").exists()
    assert list(root.glob("*.tmp")) == []
