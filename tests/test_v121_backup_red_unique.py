def test_unique_staging_helper_required():
    from app import v121_backup
    assert getattr(v121_backup, "_unique_archive_temp", None) is not None
