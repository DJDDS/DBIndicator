def test_v121_backup_declares_race_safe_compression():
    from app import v121_backup
    assert v121_backup.COMPRESSION_RACE_SAFE is True
