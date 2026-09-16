import pathlib


def test_unique_archive_temp_returns_distinct_sibling_paths(tmp_path):
    from app import v121_backup
    dst = tmp_path / "day_micro.jsonl.gz"
    a = pathlib.Path(v121_backup._unique_archive_temp(dst))
    b = pathlib.Path(v121_backup._unique_archive_temp(dst))
    try:
        assert a != b
        assert a.parent == dst.parent == b.parent
        assert a.name.endswith(".tmp") and b.name.endswith(".tmp")
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)
