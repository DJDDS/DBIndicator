import datetime as dt
import json
from pathlib import Path

from app import trial25_shadow as sh


NOW = dt.datetime(2026, 10, 8, 15, 10, 4)


def event_fixture():
    return {
        "symbol": "ABC",
        "meeting_date": "2026-10-09",
        "entry_date": "2026-10-08",
        "exit_date": "2026-10-12",
        "status": "ENTRY_DUE",
    }


def structure_fixture(atm=100):
    def leg(role, typ, strike, token):
        return {
            "role": role, "type": typ, "strike": float(strike),
            "expiry": "2026-10-27", "lot_size": 50,
            "tradingsymbol": f"ABC-{strike}-{typ}", "instrument_token": token,
        }
    return {
        "status": "OK", "expiry": "2026-10-27", "atm_strike": float(atm), "lot_size": 50,
        "contract_identities": {
            "atm_call": leg("atm_call", "CE", atm, 1),
            "atm_put": leg("atm_put", "PE", atm, 2),
            "lower_put": leg("lower_put", "PE", 80, 3),
            "upper_call": leg("upper_call", "CE", 120, 4),
        },
    }


def quote_fixture():
    return {
        "atm_call": {"tradingsymbol": "ABC-100-CE", "instrument_token": 1, "best_bid": 10.0, "best_ask": 10.2},
        "atm_put": {"tradingsymbol": "ABC-100-PE", "instrument_token": 2, "best_bid": 9.8, "best_ask": 10.0},
        "lower_put": {"tradingsymbol": "ABC-80-PE", "instrument_token": 3, "best_bid": 1.0, "best_ask": 1.1},
        "upper_call": {"tradingsymbol": "ABC-120-CE", "instrument_token": 4, "best_bid": 0.9, "best_ask": 1.0},
    }


def _paths(tmp_path):
    return (tmp_path/"state.json", tmp_path/"ledger.jsonl", tmp_path/"raw.jsonl")


def test_event_id_is_deterministic_and_does_not_depend_on_expiry():
    a = sh.event_id("ABC", "2026-10-09", "2026-10-08")
    b = sh.event_id("ABC", "2026-10-09", "2026-10-08")
    assert a == b
    assert len(a) == 24


def test_entry_capture_freezes_exact_four_contracts_and_is_idempotent(tmp_path):
    state_file, ledger, raw = _paths(tmp_path)
    first = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=structure_fixture(),
        quotes=quote_fixture(), captured_at=NOW,
    )
    changed = structure_fixture(atm=105)
    second = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=changed,
        quotes=quote_fixture(), captured_at=NOW + dt.timedelta(seconds=1),
    )
    assert first["event_id"] == second["event_id"]
    assert second["status"] == "ENTRY_CAPTURED"
    assert second["contracts"] == first["contracts"]
    state = sh.load_state(state_file)
    assert state["entry_capture_count"] == 1
    assert len(raw.read_text(encoding="utf-8").splitlines()) == 1


def test_exit_requires_same_contract_ids(tmp_path):
    state_file, ledger, raw = _paths(tmp_path)
    entry = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=structure_fixture(),
        quotes=quote_fixture(), captured_at=NOW,
    )
    bad = quote_fixture()
    bad["upper_call"] = dict(bad["upper_call"])
    bad["upper_call"]["instrument_token"] = 999
    out = sh.record_exit(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event_id=entry["event_id"], quotes=bad,
        captured_at=dt.datetime(2026, 10, 12, 9, 30, 4),
    )
    assert out["status"] == "UNAVAILABLE_CONTRACT_CHANGED"


def test_completed_raw_event_is_immutable_and_restart_safe(tmp_path):
    state_file, ledger, raw = _paths(tmp_path)
    entry = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=structure_fixture(),
        quotes=quote_fixture(), captured_at=NOW,
    )
    completed = sh.record_exit(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event_id=entry["event_id"], quotes=quote_fixture(),
        captured_at=dt.datetime(2026, 10, 12, 9, 30, 4),
    )
    assert completed["status"] == "COMPLETED_RAW"
    before_state = state_file.read_bytes()
    before_raw = raw.read_bytes()
    again = sh.record_exit(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event_id=entry["event_id"], quotes=quote_fixture(),
        captured_at=dt.datetime(2026, 10, 12, 9, 30, 5),
    )
    assert again["status"] == "COMPLETED_RAW"
    assert state_file.read_bytes() == before_state
    assert raw.read_bytes() == before_raw
    restored = sh.load_state(state_file)
    assert restored["events"][entry["event_id"]]["status"] == "COMPLETED_RAW"
    assert restored["exit_capture_count"] == 1


def test_public_summary_contains_counts_not_raw_quotes(tmp_path):
    state_file, ledger, raw = _paths(tmp_path)
    entry = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=structure_fixture(),
        quotes=quote_fixture(), captured_at=NOW,
    )
    out = sh.public_summary(sh.load_state(state_file))
    assert out["entry_captured"] == 1
    assert out["completed"] == 0
    assert "quotes" not in json.dumps(out).lower()


def test_public_summary_reports_stale_audit_without_efficacy(tmp_path):
    state_file, ledger, raw = _paths(tmp_path)
    quotes = quote_fixture()
    for role, snap in quotes.items():
        snap["two_sided"] = True
        snap["last_trade_stale_600s"] = role in ("atm_call", "lower_put")
    entry = sh.record_entry(
        state_file=state_file, ledger_file=ledger, raw_quote_file=raw,
        event=event_fixture(), structure=structure_fixture(),
        quotes=quotes, captured_at=NOW,
    )
    out = sh.public_summary(sh.load_state(state_file))
    assert entry["entry_stale_audit"]["live_book_old_trade_legs"] == 2
    assert out["stale_audit"]["live_book_old_trade_legs"] == 2
    encoded = json.dumps(out).lower()
    for forbidden in ("pnl", "profit_factor", "win_rate", "t_stat", "mean_return"):
        assert forbidden not in encoded


def test_calendar_removal_before_entry_kills_discovered_event(tmp_path):
    state = sh.empty_state()
    ledger = tmp_path / "ledger.jsonl"
    active = {
        "events": {
            "ABC": {
                "symbol": "ABC",
                "meeting_date": "2026-10-09",
                "state": "ACTIVE",
                "first_seen_at": "2026-10-01T09:20:00+05:30",
                "last_changed_at": "2026-10-01T09:20:00+05:30",
                "source_fingerprint": "active",
            }
        }
    }
    sh.discover_events(
        state, active, {"ABC"},
        now=dt.datetime(2026, 10, 7, 12, 0),
        ledger_file=ledger,
    )
    event = next(iter(state["events"].values()))
    assert event["status"] == "DISCOVERED"

    removed = {
        "events": {
            "ABC": {
                "symbol": "ABC",
                "meeting_date": "2026-10-09",
                "state": "REMOVED",
                "first_seen_at": "2026-10-01T09:20:00+05:30",
                "last_changed_at": "2026-10-08T10:00:00+05:30",
                "source_fingerprint": "removed",
            }
        }
    }
    sh.discover_events(
        state, removed, {"ABC"},
        now=dt.datetime(2026, 10, 8, 10, 1),
        ledger_file=ledger,
    )
    event = next(iter(state["events"].values()))
    assert event["status"] == "UNAVAILABLE_REMOVED_BEFORE_ENTRY"


def test_public_summary_exposes_safe_event_queue_without_contracts_or_efficacy():
    state = {
        "events": {
            "evt-1": {
                "event_id": "evt-1",
                "symbol": "ABC",
                "meeting_date": "2026-10-09",
                "entry_date": "2026-10-08",
                "exit_date": "2026-10-12",
                "status": "DISCOVERED",
                "contracts": {"secret": "must-not-leak"},
                "spot_at_entry": 100.0,
            }
        }
    }
    out = sh.public_summary(state)
    assert out["event_queue"] == [{
        "event_id": "evt-1",
        "symbol": "ABC",
        "meeting_date": "2026-10-09",
        "entry_date": "2026-10-08",
        "exit_date": "2026-10-12",
        "status": "DISCOVERED",
    }]
    encoded = json.dumps(out["event_queue"]).lower()
    assert "contracts" not in encoded
    assert "spot_at_entry" not in encoded
    assert "pnl" not in encoded
    assert "return" not in encoded
