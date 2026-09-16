import inspect


def test_fixed_shared_temp_implementation_is_gone():
    from app import v121_backup
    assert "tmp=Path(str(dst)+'.tmp')" not in inspect.getsource(v121_backup.compress_completed_day)
