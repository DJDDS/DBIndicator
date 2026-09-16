def test_race_safe_marker():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False)
