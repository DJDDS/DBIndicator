import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from app import v12_feasibility, v12_feasibility_freeze as freeze


def _state(pass_count=22):
    captured = {
        day: list(freeze.REQUIRED_FINAL_SLOTS)
        for day in freeze.EXPECTED_TRADING_DAYS
    }
    captured["2026-09-11"] = ["MIDDAY", "PRE_CAS", "POST_CAS"]
    stats = {}
    for i in range(pass_count):
        stats[f"PASS{i:03d}"] = {
            "broad_snapshots": 39,
            "two_sided_snapshots": 39,
            "spread_values": [1.0, 1.5, 2.0],
            "term_structure_snapshots": 39,
            "earnings_quote_snapshots": 0,
        }
    for i in range(3):
        stats[f"FAIL{i:03d}"] = {
            "broad_snapshots": 39,
            "two_sided_snapshots": 39,
            "spread_values": [5.0, 6.0, 7.0],
            "term_structure_snapshots": 39,
            "earnings_quote_snapshots": 0,
        }
    return {
        "captured_slots": captured,
        "last_capture_at": "2026-09-21T15:37:17",
        "last_capture_status": "CAPTURED",
        "last_error": None,
        "last_successful_write_at": "2026-09-21T15:37:17",
        "last_write_error": None,
        "quote_contracts": 1000,
        "quote_error_count": 0,
        "stale_contracts": 100,
        "symbol_stats": stats,
        "final_week_samples": 0,
    }


def _write_state(path, state):
    path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def test_freeze_is_exact_reproducible_and_does_not_mutate_live_state(tmp_path):
    state_file = tmp_path / "v12_option_state.json"
    state = _state()
    _write_state(state_file, state)
    before = state_file.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    result = freeze.create_10d_feasibility_freeze(
        state_file,
        tmp_path,
        feasibility_source_file=Path(v12_feasibility.__file__),
        deployed_commit="abc123",
        generated_at=dt.datetime(2026, 9, 21, 16, 0, tzinfo=dt.timezone.utc),
    )

    assert result["status"] == "CREATED_AND_VERIFIED"
    assert state_file.read_bytes() == before
    assert hashlib.sha256(state_file.read_bytes()).hexdigest() == before_hash

    report = json.loads(Path(result["paths"]["report"]).read_text(encoding="utf-8"))
    expected = v12_feasibility.summarize_feasibility(state)
    assert report["feasibility"] == expected
    assert report["metadata"]["source_state_sha256"] == before_hash
    assert report["metadata"]["deployed_commit"] == "abc123"
    assert report["feasibility"]["trading_days_recorded"] == 10
    assert report["feasibility"]["slots_captured"] == 39
    assert report["feasibility"]["trial25_locked"] is True
    assert report["feasibility"]["tradeable_symbols"] == 22
    assert report["feasibility"]["status"] == "STOCK OPTIONS PRACTICALLY TESTABLE"


def test_freeze_refuses_wrong_first_ten_day_contract(tmp_path):
    state_file = tmp_path / "v12_option_state.json"
    state = _state()
    state["captured_slots"]["2026-09-22"] = list(freeze.REQUIRED_FINAL_SLOTS)
    _write_state(state_file, state)
    with pytest.raises(freeze.FreezePreconditionError):
        freeze.create_10d_feasibility_freeze(state_file, tmp_path)


def test_freeze_refuses_incomplete_final_day(tmp_path):
    state_file = tmp_path / "v12_option_state.json"
    state = _state()
    state["captured_slots"]["2026-09-21"] = ["OPEN_STABLE", "MIDDAY", "PRE_CAS"]
    _write_state(state_file, state)
    with pytest.raises(freeze.FreezePreconditionError):
        freeze.create_10d_feasibility_freeze(state_file, tmp_path)


def test_existing_freeze_is_idempotent_even_after_live_state_moves_on(tmp_path):
    state_file = tmp_path / "v12_option_state.json"
    _write_state(state_file, _state())
    first = freeze.create_10d_feasibility_freeze(state_file, tmp_path)

    later = _state()
    later["captured_slots"]["2026-09-22"] = list(freeze.REQUIRED_FINAL_SLOTS)
    _write_state(state_file, later)
    second = freeze.create_10d_feasibility_freeze(state_file, tmp_path)

    assert second["status"] == "EXISTING_VALID_FREEZE"
    assert second["hashes"] == first["hashes"]


def test_existing_freeze_detects_tampering(tmp_path):
    state_file = tmp_path / "v12_option_state.json"
    _write_state(state_file, _state())
    result = freeze.create_10d_feasibility_freeze(state_file, tmp_path)
    frozen_state = Path(result["paths"]["state"])
    frozen_state.write_bytes(frozen_state.read_bytes() + b"\n")

    with pytest.raises(freeze.FreezeIntegrityError):
        freeze.create_10d_feasibility_freeze(state_file, tmp_path)
