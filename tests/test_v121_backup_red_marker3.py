def test_unique_per_attempt_staging_marker():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", None) == "unique-per-attempt"
