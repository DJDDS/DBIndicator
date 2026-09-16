def test_v121_backup_has_unique_gzip_staging_contract():
    from app import v121_backup
    assert callable(v121_backup._unique_archive_temp)
