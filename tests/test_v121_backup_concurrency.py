import datetime as dt
import gzip
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_compression_same_day_is_idempotent(tmp_path):
    from app.v121_backup import compress_completed_day

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n" * 20000, encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compress_completed_day, root, dt.date(2026, 9, 16)) for _ in range(2)]
        results = [future.result() for future in futures]

    archive = root / "2026-09-16_micro.jsonl.gz"
    assert archive.exists()
    assert not source.exists()
    assert all(archive in result for result in results)
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        assert handle.readline() == "{\"tick\":1}\n"
    assert list(root.glob("*.tmp")) == []
