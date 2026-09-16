def test_unique_archive_staging_helper_exists():
    from app import v121_backup
    assert hasattr(v121_backup, "_unique_archive_temp")
