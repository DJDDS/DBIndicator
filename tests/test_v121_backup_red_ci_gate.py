def test_v121_race_safe_compression_enabled():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False) is True
