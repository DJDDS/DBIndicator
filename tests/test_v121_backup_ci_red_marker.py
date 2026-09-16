def test_v121_compression_race_fix_marker():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False) is True
