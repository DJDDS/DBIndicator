def test_compression_race_fix_required():
    from app import v121_backup
    assert getattr(v121_backup, "COMPRESSION_RACE_SAFE", False), "RED: V12.1 shared .gz.tmp race remains"
