def test_red_absolute_helper_terminal():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
