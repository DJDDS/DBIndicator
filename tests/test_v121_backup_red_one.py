def test_compression_race_safety_flag_is_enabled():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False)
