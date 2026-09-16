def test_compressor_race_safety_marker():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False)
