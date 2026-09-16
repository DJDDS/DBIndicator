def test_unique_archive_temp_api_is_available():
    from app import v121_backup
    assert callable(getattr(v121_backup, "_unique_archive_temp", None))
