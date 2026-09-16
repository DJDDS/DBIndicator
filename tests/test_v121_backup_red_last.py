def test_race_safe_compression_contract():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", "") == "unique-per-attempt"
