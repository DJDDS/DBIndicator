def test_unique_staging_gate():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", "") == "unique-per-attempt"
