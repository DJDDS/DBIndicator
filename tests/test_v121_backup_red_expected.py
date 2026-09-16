def test_v121_race_fix_is_present():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False), "expected RED before compression race fix"
