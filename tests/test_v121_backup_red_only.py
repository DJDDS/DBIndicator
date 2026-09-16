def test_v121_unique_temp_fix_not_optional():
    from app import v121_backup
    assert hasattr(v121_backup, "_unique_archive_temp")
