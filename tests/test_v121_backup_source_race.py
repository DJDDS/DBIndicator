import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_compression_tolerates_peer_removing_source(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")

    barrier = threading.Barrier(2)
    original_exists = v121_backup.Path.exists

    def synchronized_exists(self):
        result = original_exists(self)
        if self == source and result:
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(v121_backup.Path, "exists", synchronized_exists)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(v121_backup.compress_completed_day, root, dt.date(2026, 9, 16)) for _ in range(2)]
        errors = []
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)

    assert errors == []
    assert (root / "2026-09-16_micro.jsonl.gz").exists()
