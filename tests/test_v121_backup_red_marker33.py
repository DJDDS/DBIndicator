def test_red_strategy_ultimate():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", None) == "unique-per-attempt"
