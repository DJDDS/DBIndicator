def test_unique_helper_gate():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
