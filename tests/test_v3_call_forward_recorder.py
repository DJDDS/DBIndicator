import datetime as dt
import json

from app.v3_call_forward_recorder import CallV1ForwardRecorder


def _candidate():
    return {
        "state": "PASS",
        "pass": True,
        "probability": 0.71,
        "threshold": 0.5929846109883474,
        "selected_contract": "TEST26OCT100CE",
        "selected_offset": 0,
    }


def _snap(bid=9.8, ask=10.2, mid=10.0):
    return {
        "symbol": "TEST26OCT100CE",
        "type": "CE",
        "strike": 100.0,
        "expiry": "2026-10-27",
        "dte": 27,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_pct": (ask - bid) / mid * 100.0,
        "iv_pct": 25.0,
        "delta": 0.52,
        "gamma": 0.01,
        "theta_per_day": -0.2,
        "vega": 0.08,
        "oi": 10000,
        "volume": 500,
        "quote_age_s": 1.0,
    }


def test_forward_recorder_records_entry_once_and_executable_outcomes(tmp_path):
    ledger = tmp_path / "forward.jsonl"
    state = tmp_path / "forward_state.json"
    rec = CallV1ForwardRecorder(ledger, state)
    t0 = dt.datetime(2026, 10, 1, 10, 0, 0)

    event_id = rec.observe_signal(
        now=t0,
        symbol="TEST",
        spot=100.0,
        candidate=_candidate(),
        contract_snapshot=_snap(),
    )
    assert event_id
    assert rec.status()["entries"] == 1
    assert rec.active_subscriptions() == [{"symbol": "TEST", "contract": "TEST26OCT100CE"}]

    # Repeated PASS evaluations do not create duplicate entries.
    rec.observe_signal(
        now=t0 + dt.timedelta(seconds=2),
        symbol="TEST",
        spot=100.1,
        candidate=_candidate(),
        contract_snapshot=_snap(),
    )
    assert rec.status()["entries"] == 1

    rec.observe_market(
        now=t0 + dt.timedelta(seconds=61),
        symbol="TEST",
        contract="TEST26OCT100CE",
        spot=100.5,
        snapshot=_snap(bid=10.8, ask=11.2, mid=11.0),
    )
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    one = next(x for x in lines if x.get("record_type") == "OUTCOME" and x.get("horizon") == "1m")
    # Entry is executable at ask 10.2; exit is executable at bid 10.8.
    assert round(one["executable_return_pct"], 6) == round((10.8 / 10.2 - 1.0) * 100.0, 6)
    assert round(one["underlying_return_bps"], 6) == 50.0

    for seconds in (181, 301, 601, 721, 901):
        rec.observe_market(
            now=t0 + dt.timedelta(seconds=seconds),
            symbol="TEST",
            contract="TEST26OCT100CE",
            spot=101.0,
            snapshot=_snap(bid=11.0, ask=11.4, mid=11.2),
        )
    status = rec.status()
    assert status["outcomes"] == 6
    assert status["completed"] == 1
    assert status["open_events"] == 0


def test_forward_recorder_state_survives_reload_and_rearm_is_delayed(tmp_path):
    ledger = tmp_path / "forward.jsonl"
    state = tmp_path / "forward_state.json"
    t0 = dt.datetime(2026, 10, 1, 10, 0, 0)
    rec = CallV1ForwardRecorder(ledger, state)
    first = rec.observe_signal(
        now=t0,
        symbol="TEST",
        spot=100.0,
        candidate=_candidate(),
        contract_snapshot=_snap(),
    )
    for seconds in (61, 181, 301, 601, 721, 901):
        rec.observe_market(
            now=t0 + dt.timedelta(seconds=seconds),
            symbol="TEST",
            contract="TEST26OCT100CE",
            spot=101.0,
            snapshot=_snap(),
        )
    assert rec.status()["completed"] == 1

    rec2 = CallV1ForwardRecorder(ledger, state)
    assert rec2.status()["entries"] == 1
    assert rec2.status()["completed"] == 1

    # A still-high PASS cannot immediately create a correlated second event.
    blocked = rec2.observe_signal(
        now=t0 + dt.timedelta(minutes=11),
        symbol="TEST",
        spot=101.0,
        candidate=_candidate(),
        contract_snapshot=_snap(),
    )
    assert blocked == first
    assert rec2.status()["entries"] == 1

    second = rec2.observe_signal(
        now=t0 + dt.timedelta(minutes=16),
        symbol="TEST",
        spot=102.0,
        candidate=_candidate(),
        contract_snapshot=_snap(),
    )
    assert second and second != first
    assert rec2.status()["entries"] == 2


def test_nonpass_releases_latch_without_creating_entry(tmp_path):
    rec = CallV1ForwardRecorder(tmp_path / "f.jsonl", tmp_path / "s.json")
    t0 = dt.datetime(2026, 10, 1, 10, 0)
    no = dict(_candidate(), state="BELOW_GATE", **{"pass": False})
    assert rec.observe_signal(
        now=t0, symbol="TEST", spot=100.0, candidate=no, contract_snapshot=None
    ) is None
    assert rec.status()["entries"] == 0


def test_stale_exit_quote_cannot_be_recorded_as_executable(tmp_path):
    rec = CallV1ForwardRecorder(tmp_path / "f.jsonl", tmp_path / "s.json")
    t0 = dt.datetime(2026, 10, 9, 10, 0)
    rec.observe_signal(now=t0, symbol="TEST", spot=100, candidate=_candidate(), contract_snapshot=_snap())
    rec.observe_market(now=t0 + dt.timedelta(seconds=61), symbol="TEST", contract="TEST26OCT100CE",
                       spot=100, snapshot=dict(_snap(), quote_age_s=60))
    assert rec.status()["outcomes"] == 0
    rec.observe_market(now=t0 + dt.timedelta(seconds=106), symbol="TEST", contract="TEST26OCT100CE",
                       spot=100, snapshot=dict(_snap(), quote_age_s=60))
    lines = [json.loads(x) for x in (tmp_path / "f.jsonl").read_text().splitlines()]
    assert next(x for x in lines if x.get("horizon") == "1m")["status"] == "MISSING_QUOTE"


def test_forward_entry_rejects_stale_unknown_age_or_wrong_contract(tmp_path):
    rec = CallV1ForwardRecorder(tmp_path / "f.jsonl", tmp_path / "s.json")
    t0 = dt.datetime(2026, 10, 9, 10, 0)
    for snap in [dict(_snap(), quote_age_s=60), dict(_snap(), quote_age_s=None),
                 dict(_snap(), symbol="OTHERCE")]:
        assert rec.observe_signal(now=t0, symbol="TEST", spot=100, candidate=_candidate(),
                                  contract_snapshot=snap) is None
    assert rec.status()["entries"] == 0


def test_recorder_without_a_ledger_does_not_claim_it_is_recording():
    assert CallV1ForwardRecorder(None, None).status()["status"] == "DISABLED"
