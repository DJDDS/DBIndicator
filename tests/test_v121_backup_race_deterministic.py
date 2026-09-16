import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_compressors_do_not_share_temporary_archive(tmp_path, monkeypatch):
    from app import v121_backup

    root = tmp_path / "index_vol"
    root.mkdir()
    source = root / "2026-09-16_micro.jsonl"
    source.write_text("{\"tick\":1}\n", encoding="utf-8")

    original_gzip_open = v121_backup.gzip.open
    barrier = threading.Barrier(2)
    temp_names = []
    lock = threading.Lock()

    def synchronized_gzip_open(filename, mode, *args, **kwargs):
        with lock:
            temp_names.append(str(filename))
        handle = original_gzip_open(filename, mode, *args, **kwargs)
        barrier.wait(timeout=5)
        return handle

    monkeypatch.setattr(v121_backup.gzip, "open", synchronized_gzip_open)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(v121_backup.compress_completed_day, root, dt.date(2026, 9, 16)) for _ in range(2)]
        errors = []
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)

    assert errors == []
    assert len(temp_names) == 2
    assert len(set(temp_names)) == 2
