import datetime as dt
from pathlib import Path


def test_railway_volume_becomes_default_v12_storage_root():
    from app.v12_storage import resolve_v12_storage
    out = resolve_v12_storage({"RAILWAY_VOLUME_MOUNT_PATH": "/data"})
    assert out["persistent"] is True
    assert out["mode"] == "PERSISTENT_VOLUME"
    assert out["root"] == "/data/v12"
    assert out["option_snapshots"] == "/data/v12/v12_option_snapshots.jsonl"
    assert out["option_state"] == "/data/v12/v12_option_state.json"
    assert out["earnings_ledger"] == "/data/v12/v12_earnings_ledger.jsonl"
    assert out["earnings_state"] == "/data/v12/v12_earnings_state.json"


def test_explicit_v12_file_override_wins_over_volume_default():
    from app.v12_storage import resolve_v12_storage
    out = resolve_v12_storage({
        "RAILWAY_VOLUME_MOUNT_PATH": "/data",
        "V12_OPTION_SNAPSHOT_FILE": "/custom/options.jsonl",
    })
    assert out["option_snapshots"] == "/custom/options.jsonl"
    assert out["option_state"] == "/data/v12/v12_option_state.json"


def test_no_volume_is_explicitly_ephemeral():
    from app.v12_storage import resolve_v12_storage
    out = resolve_v12_storage({})
    assert out["persistent"] is False
    assert out["mode"] == "EPHEMERAL"
    assert out["root"] == "."
    assert out["option_snapshots"] == "v12_option_snapshots.jsonl"


def test_recorder_health_proves_file_and_slot_writes(tmp_path):
    from app.v12_option_recorder import recorder_health, _save_v12_state
    snapshot = tmp_path / "v12_option_snapshots.jsonl"
    state_file = tmp_path / "v12_option_state.json"
    snapshot.write_text('{"slot":"OPEN_STABLE"}\n', encoding="utf-8")
    _save_v12_state(state_file, {
        "captured_slots": {"2026-09-07": ["OPEN_STABLE", "MIDDAY"]},
        "slot_summaries": [
            {"date":"2026-09-07","slot":"OPEN_STABLE","status":"CAPTURED","two_sided_symbols":22,"quote_errors":[]},
            {"date":"2026-09-07","slot":"MIDDAY","status":"CAPTURED_PARTIAL","two_sided_symbols":19,"quote_errors":[{"error":"one quote failed"}]},
        ],
        "quote_contracts": 880,
        "total_slot_records": 2,
        "quote_error_count": 1,
        "last_successful_write_at": "2026-09-07T13:01:00+05:30",
        "last_capture_at": "2026-09-07T13:01:00+05:30",
        "last_capture_status": "CAPTURED_PARTIAL",
        "last_error": "one quote failed",
        "last_write_error": None,
    })
    now = dt.datetime(2026, 9, 7, 13, 2, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    health = recorder_health(snapshot, state_file, now=now, storage_mode="PERSISTENT_VOLUME", storage_root="/data/v12")
    assert health["recorder_status"] == "RECORDING"
    assert health["storage_persistent"] is True
    assert health["snapshot_file_exists"] is True
    assert health["snapshot_file_bytes"] > 0
    assert health["total_slot_records"] == 2
    assert health["option_contracts_recorded"] == 880
    assert health["two_sided_atm_straddles_recorded"] == 41
    assert health["quote_error_count"] == 1
    assert health["slots"]["OPEN_STABLE"] == "CAPTURED"
    assert health["slots"]["MIDDAY"] == "CAPTURED_PARTIAL"
    assert health["slots"]["PRE_CAS"] == "WAITING"
    assert health["slots"]["POST_CAS"] == "WAITING"
    assert health["last_successful_write_at"] == "2026-09-07T13:01:00+05:30"


def test_recorder_health_flags_ephemeral_storage_and_write_error(tmp_path):
    from app.v12_option_recorder import recorder_health, _save_v12_state
    state_file = tmp_path / "v12_option_state.json"
    _save_v12_state(state_file, {"last_write_error": "disk full"})
    health = recorder_health(tmp_path / "missing.jsonl", state_file, now=dt.datetime(2026,9,7,9,0), storage_mode="EPHEMERAL", storage_root=".")
    assert health["recorder_status"] == "ERROR"
    assert health["storage_persistent"] is False
    assert "EPHEMERAL" in health["storage_status"]
    assert health["last_write_error"] == "disk full"


def test_v1201_dashboard_has_recorder_health_fields_and_health_api():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    web = Path("app/web.py").read_text(encoding="utf-8")
    for token in (
        'id="v12-storage-status"', 'id="v12-last-write"', 'id="v12-snapshot-size"',
        'id="v12-slot-open"', 'id="v12-slot-midday"', 'id="v12-slot-pre-cas"', 'id="v12-slot-post-cas"',
        'id="v12-contract-count"', 'id="v12-two-sided-count"', 'id="v12-quote-errors"', 'id="v12-write-error"',
    ):
        assert token in html
    assert '/api/v12-recorder-health' in web


def test_recorder_health_marks_weekend_slots_market_closed(tmp_path):
    from app.v12_option_recorder import recorder_health
    health = recorder_health(
        tmp_path / 'snap.jsonl', tmp_path / 'state.json',
        now=dt.datetime(2026, 9, 5, 11, 30), storage_mode='EPHEMERAL', storage_root='.'
    )
    assert set(health['slots'].values()) == {'MARKET_CLOSED'}
    assert health['recorder_status'] == 'WAITING'
