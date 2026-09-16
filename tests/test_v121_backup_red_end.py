def test_race_safe_helper_exists_before_green():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
