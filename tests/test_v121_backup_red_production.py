import inspect


def test_production_shared_tmp_signature_removed():
    from app import v121_backup
    text = inspect.getsource(v121_backup.compress_completed_day)
    assert "str(dst)+'.tmp'" not in text
