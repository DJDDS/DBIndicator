import datetime as dt

from app.v12_option_recorder import (
    due_snapshot_slot,
    normalize_contract_snapshot,
    quote_in_batches,
    rank_deep_symbols,
    select_broad_atm_contracts,
    select_deep_contracts,
)


def _contract(symbol, expiry, strike, typ):
    return {
        "tradingsymbol": f"{symbol}-{expiry:%Y%m%d}-{strike:g}-{typ}",
        "instrument_token": hash((symbol, expiry, strike, typ)) & 0xFFFF,
        "instrument_type": typ,
        "strike": float(strike),
        "expiry": expiry,
        "lot_size": 100,
    }


def _contracts(symbol="AAA"):
    today = dt.date(2026, 9, 5)
    expiries = [dt.date(2026, 9, 29), dt.date(2026, 10, 27), dt.date(2026, 11, 24)]
    rows = []
    for expiry in expiries:
        for strike in (80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200):
            rows.extend([_contract(symbol, expiry, strike, "CE"), _contract(symbol, expiry, strike, "PE")])
    # expired junk must never be selected
    rows.append(_contract(symbol, today - dt.timedelta(days=1), 100, "CE"))
    return rows


def test_due_snapshot_slot_uses_fixed_clock_grace_and_never_backfills():
    empty = {"captured_slots": {}}
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 9, 30), empty) == "OPEN_STABLE"
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 9, 37), empty) == "OPEN_STABLE"
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 9, 38), empty) is None
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 13, 3), empty) == "MIDDAY"
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 15, 12), empty) == "PRE_CAS"
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 15, 37), empty) == "POST_CAS"
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 15, 45), empty) is None


def test_due_snapshot_slot_deduplicates_already_captured_slot():
    state = {"captured_slots": {"2026-09-07": ["MIDDAY"]}}
    assert due_snapshot_slot(dt.datetime(2026, 9, 7, 13, 2), state) is None


def test_broad_selector_keeps_near_next_and_atm_ce_pe_only():
    rows = select_broad_atm_contracts({"AAA": _contracts()}, {"AAA": 104.0}, today=dt.date(2026, 9, 5))
    assert len(rows) == 4
    assert {r["expiry"] for r in rows} == {dt.date(2026, 9, 29), dt.date(2026, 10, 27)}
    assert {r["strike"] for r in rows} == {100.0}
    assert {r["instrument_type"] for r in rows} == {"CE", "PE"}


def test_deep_selector_keeps_atm_plus_minus_six_strike_steps_for_two_expiries():
    rows = select_deep_contracts({"AAA": _contracts()}, {"AAA": 140.0}, ["AAA"], today=dt.date(2026, 9, 5), wings=6)
    # 13 strikes x CE/PE x 2 expiries
    assert len(rows) == 52
    assert len({r["tradingsymbol"] for r in rows}) == 52
    assert {r["expiry"] for r in rows} == {dt.date(2026, 9, 29), dt.date(2026, 10, 27)}


def test_deep_symbol_ranking_prioritizes_earnings_then_observed_liquidity():
    broad = {
        "AAA": {"liquidity_score": 90},
        "BBB": {"liquidity_score": 95},
        "CCC": {"liquidity_score": 70},
    }
    assert rank_deep_symbols(broad, {"CCC"}, limit=2) == ["CCC", "BBB"]


class FakeKite:
    def __init__(self, fail_batch=None):
        self.calls = []
        self.fail_batch = fail_batch

    def quote(self, keys):
        self.calls.append(list(keys))
        if self.fail_batch is not None and len(self.calls) == self.fail_batch:
            raise RuntimeError("batch failure")
        return {k: {"last_price": 1.0} for k in keys}


def test_quote_batching_never_exceeds_400_and_partial_failure_is_audited():
    kite = FakeKite(fail_batch=2)
    quotes, errors = quote_in_batches(kite, [f"NFO:X{i}" for i in range(901)], batch_size=400, sleep_fn=lambda _seconds: None)
    assert [len(c) for c in kite.calls] == [400, 400, 101]
    assert len(quotes) == 501
    assert len(errors) == 1
    assert errors[0]["batch_index"] == 2
    assert errors[0]["size"] == 400


def _quote(two_sided=True):
    buy = [{"price": 9.8, "quantity": 100, "orders": 2}, {"price": 9.7, "quantity": 80, "orders": 1}]
    sell = [{"price": 10.2, "quantity": 120, "orders": 3}, {"price": 10.3, "quantity": 90, "orders": 2}] if two_sided else []
    return {
        "last_price": 10.0,
        "volume": 1200,
        "oi": 5000,
        "last_trade_time": dt.datetime(2026, 9, 7, 13, 0),
        "depth": {"buy": buy, "sell": sell},
    }


def test_normalized_snapshot_preserves_depth_and_computes_bid_mid_ask_iv():
    contract = _contract("AAA", dt.date(2026, 9, 29), 100, "CE")
    snap = normalize_contract_snapshot(contract, _quote(), 100.0, dt.datetime(2026, 9, 7, 13, 1), "MIDDAY")
    assert snap["two_sided"] is True
    assert snap["best_bid"] == 9.8
    assert snap["best_ask"] == 10.2
    assert snap["mid"] == 10.0
    assert snap["spread_rupees"] == 0.4
    assert snap["spread_pct"] == 4.0
    assert len(snap["depth"]["buy"]) == 2
    assert snap["bid_iv_pct"] is not None
    assert snap["mid_iv_pct"] is not None
    assert snap["ask_iv_pct"] is not None
    assert snap["delta"] is not None
    assert snap["stale"] is False


def test_one_sided_quote_is_retained_but_not_claimed_executable():
    contract = _contract("AAA", dt.date(2026, 9, 29), 100, "CE")
    snap = normalize_contract_snapshot(contract, _quote(two_sided=False), 100.0, dt.datetime(2026, 9, 7, 13, 1), "MIDDAY")
    assert snap["two_sided"] is False
    assert snap["best_bid"] == 9.8
    assert snap["best_ask"] is None
    assert snap["mid"] is None
    assert snap["spread_pct"] is None


def test_quote_batching_paces_rest_calls_at_one_per_second():
    kite = FakeKite()
    sleeps = []
    quote_in_batches(kite, [f"NFO:P{i}" for i in range(801)], batch_size=400, sleep_fn=sleeps.append)
    assert [len(c) for c in kite.calls] == [400, 400, 1]
    assert len(sleeps) == 2
    assert all(seconds >= 1.0 for seconds in sleeps)
