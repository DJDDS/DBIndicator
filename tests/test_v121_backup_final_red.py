def test_v121_compression_race_regression_gate():
    from app import v121_backup
    assert hasattr(v121_backup, "_unique_archive_temp"), "race-safe unique archive staging is not implemented yet"
