def test_compressor_reports_race_safe_contract():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False) is True
