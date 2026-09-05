import datetime as dt
import json

from app.v12_feasibility import summarize_feasibility
from app.v12_option_recorder import record_snapshot


def _contract(symbol, expiry, strike, typ):
    return {
        "tradingsymbol": f"{symbol}{expiry:%m%d}{int(strike)}{typ}",
        "instrument_token": abs(hash((symbol, expiry, strike, typ))) % 100000,
        "instrument_type": typ,
        "strike": float(strike),
        "expiry": expiry,
        "lot_size": 100,
    }


def _map(symbols=("AAA", "BBB")):
    out = {}
    for symbol in symbols:
        rows = []
        for expiry in (dt.date(2026, 9, 29), dt.date(2026, 10, 27)):
            for strike in (80, 90, 100, 110, 120):
                rows += [_contract(symbol, expiry, strike, "CE"), _contract(symbol, expiry, strike, "PE")]
        out[symbol] = rows
    return out


def _q(last=10.0):
    return {
        "last_price": last,
        "volume": 1000,
        "oi": 5000,
        "last_trade_time": dt.datetime(2026, 9, 7, 13, 0),
        "depth": {
            "buy": [{"price": 9.9, "quantity": 100, "orders": 2}],
            "sell": [{"price": 10.1, "quantity": 120, "orders": 3}],
        },
    }


class FakeKite:
    def __init__(self, fail_call=None):
        self.calls = 0
        self.fail_call = fail_call
    def quote(self, keys):
        self.calls += 1
        if self.fail_call == self.calls:
            raise RuntimeError("quote batch failed")
        return {key: _q() for key in keys}


def test_record_snapshot_persists_slot_contracts_and_state(tmp_path):
    state_file = tmp_path / "state.json"
    snapshot_file = tmp_path / "snapshots.jsonl"
    out = record_snapshot(
        FakeKite(),
        [{"symbol": "AAA", "close": 100.0}, {"symbol": "BBB", "close": 100.0}],
        {"AAA"},
        now=dt.datetime(2026, 9, 7, 13, 1),
        snapshot_file=snapshot_file,
        state_file=state_file,
        contracts_map=_map(),
        deep_symbol_limit=1, sleep_fn=lambda _seconds: None,
    )
    assert out["status"] == "CAPTURED"
    assert out["slot"] == "MIDDAY"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["captured_slots"]["2026-09-07"] == ["MIDDAY"]
    assert state["slot_summaries"][-1]["broad_symbols"] == 2
    record = json.loads(snapshot_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["slot"] == "MIDDAY"
    assert record["broad_contracts"]
    assert record["deep_contracts"]
    assert record["trial25_locked"] is True


def test_record_snapshot_is_idempotent_inside_same_slot(tmp_path):
    state_file = tmp_path / "state.json"
    snapshot_file = tmp_path / "snapshots.jsonl"
    kwargs = dict(
        kite=FakeKite(), results=[{"symbol": "AAA", "close": 100.0}], earnings_symbols=set(),
        now=dt.datetime(2026, 9, 7, 13, 1), snapshot_file=snapshot_file, state_file=state_file,
        contracts_map=_map(("AAA",)), deep_symbol_limit=1, sleep_fn=lambda _seconds: None,
    )
    first = record_snapshot(**kwargs)
    second = record_snapshot(**kwargs)
    assert first["status"] == "CAPTURED"
    assert second["status"] == "NOT_DUE"
    assert len(snapshot_file.read_text(encoding="utf-8").splitlines()) == 1


def test_partial_quote_failure_is_recorded_not_raised(tmp_path):
    out = record_snapshot(
        FakeKite(fail_call=2),
        [{"symbol": "AAA", "close": 100.0}, {"symbol": "BBB", "close": 100.0}],
        set(), now=dt.datetime(2026, 9, 7, 13, 1),
        snapshot_file=tmp_path / "snapshots.jsonl", state_file=tmp_path / "state.json",
        contracts_map=_map(), deep_symbol_limit=2, sleep_fn=lambda _seconds: None,
    )
    assert out["status"] == "CAPTURED_PARTIAL"
    assert out["quote_errors"] >= 1


def _state(days=10, symbols=25, coverage=0.75, spread=2.0):
    captured = {}
    for i in range(days):
        day = (dt.date(2026, 8, 20) + dt.timedelta(days=i)).isoformat()
        captured[day] = ["OPEN_STABLE", "MIDDAY", "PRE_CAS", "POST_CAS"]
    total = days * 4
    two = int(total * coverage)
    stats = {
        f"S{i}": {
            "broad_snapshots": total,
            "two_sided_snapshots": two,
            "spread_values": [spread] * max(1, two),
            "term_structure_snapshots": two,
            "earnings_quote_snapshots": 0,
        }
        for i in range(symbols)
    }
    return {
        "captured_slots": captured,
        "symbol_stats": stats,
        "quote_contracts": total * symbols * 4,
        "stale_contracts": 10,
        "slot_summaries": [],
    }


def test_feasibility_has_no_verdict_before_ten_trading_days():
    out = summarize_feasibility(_state(days=9))
    assert out["status"] == "RECORDING — NO FEASIBILITY VERDICT"
    assert out["trading_days_recorded"] == 9


def test_feasibility_passes_with_twenty_tradeable_symbols_after_ten_days():
    out = summarize_feasibility(_state(days=10, symbols=25, coverage=0.75, spread=2.5))
    assert out["status"] == "STOCK OPTIONS PRACTICALLY TESTABLE"
    assert out["tradeable_symbols"] == 25
    assert out["median_straddle_spread_pct"] == 2.5


def test_feasibility_fails_when_fewer_than_twenty_symbols_meet_gate():
    out = summarize_feasibility(_state(days=10, symbols=19, coverage=0.90, spread=1.5))
    assert out["status"] == "STOCK OPTION LIQUIDITY GATE NOT MET"
    assert out["tradeable_symbols"] == 19


def test_feasibility_fails_when_coverage_or_spread_is_not_executable():
    low_cov = summarize_feasibility(_state(days=10, symbols=25, coverage=0.60, spread=1.0))
    wide = summarize_feasibility(_state(days=10, symbols=25, coverage=0.90, spread=5.0))
    assert low_cov["tradeable_symbols"] == 0
    assert wide["tradeable_symbols"] == 0


class SpotAwareKite(FakeKite):
    def quote(self, keys):
        self.calls += 1
        out = {}
        for key in keys:
            if key.startswith('NSE:'):
                out[key] = {'last_price': 109.0}
            else:
                out[key] = _q()
        return out


def test_snapshot_refreshes_spot_at_capture_time_before_selecting_atm(tmp_path):
    snapshot_file = tmp_path / 'snapshots.jsonl'
    out = record_snapshot(
        SpotAwareKite(),
        [{'symbol': 'AAA', 'close': 100.0}], set(),
        now=dt.datetime(2026, 9, 7, 13, 1),
        snapshot_file=snapshot_file, state_file=tmp_path/'state.json',
        contracts_map=_map(('AAA',)), deep_symbol_limit=1, sleep_fn=lambda _seconds: None,
    )
    assert out['status'] == 'CAPTURED'
    record = json.loads(snapshot_file.read_text().splitlines()[-1])
    assert {row['strike'] for row in record['broad_contracts']} == {110.0}
    assert {row['spot'] for row in record['broad_contracts']} == {109.0}


def test_snapshot_paces_between_spot_broad_and_deep_quote_phases(tmp_path):
    sleeps = []
    record_snapshot(
        FakeKite(), [{'symbol':'AAA','close':100.0}], set(),
        now=dt.datetime(2026,9,7,13,1), snapshot_file=tmp_path/'snap.jsonl', state_file=tmp_path/'state.json',
        contracts_map=_map(('AAA',)), deep_symbol_limit=1, sleep_fn=sleeps.append,
    )
    # One NSE spot request, one broad ATM request, one deep-ladder request.
    assert len(sleeps) >= 2
    assert all(x >= 1.0 for x in sleeps)
