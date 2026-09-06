from pathlib import Path

BUILD = "2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB"


def test_v121_build_marker_and_trial_lock():
    assert Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip() == BUILD
    text = Path("V12_CHANGELOG.md").read_text(encoding="utf-8")
    assert "V12.1" in text
    assert "Trial 25 LOCKED" in text
    assert "India VIX is an input" in text
    assert "stock-option recorder remains unchanged" in text


def test_v121_storage_paths_follow_railway_volume():
    from app.v12_storage import resolve_v12_storage
    out = resolve_v12_storage({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})
    assert out["index_vol_root"] == "/data/v12/index_vol"
    assert out["index_vol_state"] == "/data/v12/v121_index_vol_state.json"
    assert out["index_vol_backup_state"] == "/data/v12/v121_index_vol_backup_state.json"
    assert out["rv_lab_state"] == "/data/v12/v121_rv_lab_state.json"


def test_v121_config_defaults_are_fixed_and_backup_is_optional():
    from app import config
    assert config.V121_STRIKE_STEPS == 12
    assert config.V121_MICRO_SECONDS == 5
    assert config.V121_DEPTH_SECONDS == 60
    assert config.V121_BACKUP_S3_BUCKET == ""
    assert config.V121_BACKUP_S3_PREFIX == "dbindicator/v121"


def test_v121_requirements_and_env_document_stream_and_backup():
    req = Path("requirements.txt").read_text(encoding="utf-8")
    assert "boto3==1.43.18" in req
    env = Path(".env.example").read_text(encoding="utf-8")
    for key in (
        "V121_STRIKE_STEPS", "V121_MICRO_SECONDS", "V121_DEPTH_SECONDS",
        "V121_BACKUP_S3_BUCKET", "V121_BACKUP_S3_PREFIX", "V121_BACKUP_S3_ENDPOINT_URL",
    ):
        assert key in env
