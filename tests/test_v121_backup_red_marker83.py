def test_red_last_helper_marker():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
