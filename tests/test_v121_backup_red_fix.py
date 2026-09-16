def test_v121_compressor_uses_unique_attempt_temp_files():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_TEMP_STRATEGY", None) == "unique-per-attempt"
