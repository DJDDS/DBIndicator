def test_helper_regression():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
