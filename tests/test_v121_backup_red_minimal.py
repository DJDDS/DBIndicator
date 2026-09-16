def test_race_safe_staging_api_exists():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", None) == "unique-per-attempt"
