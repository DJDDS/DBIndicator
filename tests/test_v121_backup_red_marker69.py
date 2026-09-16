def test_red_ultimate_terminal():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False) is True
