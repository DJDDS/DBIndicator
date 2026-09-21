import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from app import trial25_stage_d as sd


FORBIDDEN = ("pnl", "return", "mean", "median", "win_rate", "profit_factor", "t_stat")


def _state(n):
    events = {}
    for i in range(n):
        eid = f"event-{i:03d}"
        events[eid] = {
            "event_id": eid,
            "symbol": f"S{i%12:02d}",
            "status": "COMPLETED_RAW",
            "lot_size": 50,
            "exit_captured_at": f"2026-10-{10 + i//8:02d}T09:{30 + i%8:02d}:00",
        }
    return {"events": events}


def _raw(path, n):
    lines = []
    for i in range(n):
        eid = f"event-{i:03d}"
        entry_atm = 20.0 + (i % 5)
        exit_atm = 15.0 + ((i * 3) % 9)
        entry = {
            "atm_call": {"best_bid": entry_atm, "best_ask": entry_atm + .2},
            "atm_put": {"best_bid": entry_atm - 1, "best_ask": entry_atm - .8},
            "lower_put": {"best_bid": 1.0, "best_ask": 1.1},
            "upper_call": {"best_bid": 1.0, "best_ask": 1.1},
        }
        exitq = {
            "atm_call": {"best_bid": exit_atm - .2, "best_ask": exit_atm},
            "atm_put": {"best_bid": exit_atm - 1.2, "best_ask": exit_atm - 1},
            "lower_put": {"best_bid": .5, "best_ask": .6},
            "upper_call": {"best_bid": .5, "best_ask": .6},
        }
        lines.append(json.dumps({"event_id": eid, "capture": "ENTRY", "quotes": entry}))
        lines.append(json.dumps({"event_id": eid, "capture": "EXIT", "quotes": exitq}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_summary_before_40_contains_no_efficacy_fields():
    out = sd.safe_stage_d_summary(_state(39))
    flat = json.dumps(out).lower()
    for key in FORBIDDEN:
        assert key not in flat
    assert out["completed"] == 39
    assert out["target"] == 40
    assert out["status"] == "STAGE_D_COLLECTING"


def test_39_completed_events_cannot_create_calibration(tmp_path):
    raw = tmp_path/"raw.jsonl"; _raw(raw, 39)
    out = sd.maybe_freeze_calibration(
        _state(39), raw, tmp_path/"cal.json", tmp_path/"cal.sha256",
        now=dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc),
    )
    assert out["status"] == "WAITING_FOR_40"
    assert not (tmp_path/"cal.json").exists()


def test_42_completed_events_freeze_exact_deterministic_first_40(tmp_path):
    raw = tmp_path/"raw.jsonl"; _raw(raw, 42)
    state = _state(42)
    out = sd.maybe_freeze_calibration(
        state, raw, tmp_path/"cal.json", tmp_path/"cal.sha256",
        now=dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
    )
    assert out["status"] == "FROZEN_STAGE_D"
    assert len(out["event_ids"]) == 40
    expected = sorted(
        state["events"].values(),
        key=lambda e: (e["exit_captured_at"], e["event_id"]),
    )[:40]
    assert out["event_ids"] == [e["event_id"] for e in expected]
    assert out["sigma_d"] > 0
    assert out["stage_c_required_n"] >= 40
    encoded = json.dumps(out).lower()
    assert "\"returns\"" not in encoded
    assert "\"mean\"" not in encoded
    assert "\"event_pnl\"" not in encoded


def test_existing_calibration_is_verified_not_rewritten(tmp_path):
    raw = tmp_path/"raw.jsonl"; _raw(raw, 45)
    cal = tmp_path/"cal.json"; manifest = tmp_path/"cal.sha256"
    first = sd.maybe_freeze_calibration(_state(40), raw, cal, manifest)
    before = cal.read_bytes()
    second = sd.maybe_freeze_calibration(_state(45), raw, cal, manifest)
    assert second["status"] == "EXISTING_VALID_FREEZE"
    assert cal.read_bytes() == before
    assert second["event_ids"] == first["event_ids"]


def test_tampered_calibration_is_rejected(tmp_path):
    raw = tmp_path/"raw.jsonl"; _raw(raw, 40)
    cal = tmp_path/"cal.json"; manifest = tmp_path/"cal.sha256"
    sd.maybe_freeze_calibration(_state(40), raw, cal, manifest)
    cal.write_bytes(cal.read_bytes() + b"\n")
    with pytest.raises(sd.StageDIntegrityError):
        sd.maybe_freeze_calibration(_state(41), raw, cal, manifest)


def test_stage_c_sample_size_has_hard_minimum_40():
    assert sd.stage_c_required_n(1.0) == 40
    assert sd.stage_c_required_n(100.0) > 40
