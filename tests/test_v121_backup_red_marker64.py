def test_red_absolute_safety_terminal():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False)
