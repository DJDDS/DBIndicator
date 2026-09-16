import datetime as dt
from pathlib import Path

import pytest


def test_compression_keeps_source_when_archive_finalization_fails(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")

    real_replace = Path.replace

    def fail_gzip_replace(self, target):
        if str(target).endswith(".jsonl.gz"):
            raise OSError("simulated finalize failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_gzip_replace)
    with pytest.raises(OSError, match="simulated finalize failure"):
        v121_backup.compress_completed_day(root, dt.date(2026, 9, 16))

    assert source.exists()
    assert not (root / "2026-09-16_micro.jsonl.gz").exists()
