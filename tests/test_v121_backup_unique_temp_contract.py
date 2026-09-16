def test_unique_temp_contract_is_declared():
    from app import v121_backup
    assert v121_backup.COMPRESSION_TEMP_STRATEGY == "unique-per-attempt"
