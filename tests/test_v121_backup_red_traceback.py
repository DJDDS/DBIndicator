import inspect


def test_compressor_source_no_longer_uses_shared_gzip_tmp_literal():
    from app import v121_backup
    source = inspect.getsource(v121_backup.compress_completed_day)
    assert "Path(str(dst)+'.tmp')" not in source
