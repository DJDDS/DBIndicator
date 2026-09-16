def test_race_fix_gate_complete():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False)
