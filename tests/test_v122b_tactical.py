import datetime as dt

import pytest

from app import v122b_tactical as t


def _bar(ts, o, h, l, c, v=1000):
    return {"ts": ts.isoformat(timespec="seconds"), "open": o, "high": h, "low": l, "close": c, "volume": v, "complete": True}


def test_pool_is_bounded_two_sided_and_drops_extended():
    radar = {
        "bullish": [
            {"symbol": "B1", "phase": "PRE-IGNITION", "action_stage": "ARMED", "pressure": 80, "runway": 1},
            {"symbol": "B2", "phase": "EXTENDED", "pressure": 99, "runway": .3},
            {"symbol": "B3", "phase": "PRE-IGNITION", "pressure": 70, "runway": 1},
            {"symbol": "B4", "phase": "IGNITION", "pressure": 90, "runway": .9},
            {"symbol": "B5", "phase": "QUIET", "pressure": 60, "runway": 1},
        ],
        "bearish": [
            {"symbol": f"S{i}", "phase": "PRE-IGNITION", "pressure": 80-i, "runway": 1}
            for i in range(1, 7)
        ],
    }
    results = [{"symbol": x, "close": 100, "atr": 2} for x in ["B1","B2","B3","B4","B5","S1","S2","S3","S4","S5","S6"]]
    pool = t.select_tactical_pool(radar, results)
    assert len(pool) <= 8
    assert "B2" not in {x["symbol"] for x in pool}
    assert sum(x["direction"] == "Bullish" for x in pool) <= 4
    assert sum(x["direction"] == "Bearish" for x in pool) <= 4


def test_three_minute_builder_completes_exact_bucket():
    b = t.ThreeMinuteBarBuilder()
    base = dt.datetime(2026, 9, 22, 10, 0, 1)
    assert b.update(100, 1000, base) is None
    assert b.update(101, 1010, base + dt.timedelta(seconds=50)) is None
    completed = b.update(102, 1020, base + dt.timedelta(minutes=3))
    assert completed["open"] == 100
    assert completed["high"] == 101
    assert completed["close"] == 101
    assert completed["volume"] == 10


def test_weighted_depth_and_microprice_are_directional():
    tick = {"depth": {
        "buy": [
            {"price": 99.9, "quantity": 400},
            {"price": 99.8, "quantity": 300},
            {"price": 99.7, "quantity": 200},
        ],
        "sell": [
            {"price": 100.1, "quantity": 100},
            {"price": 100.2, "quantity": 80},
            {"price": 100.3, "quantity": 60},
        ],
    }}
    m = t.weighted_depth_metrics(tick)
    assert m["l1_imbalance"] > 0
    assert m["l5_imbalance"] > 0
    assert m["microprice_bias_bps"] > 0


def test_basis_fails_closed_when_timestamps_are_not_synchronised():
    cash = {"last_price": 100, "_received_at": "2026-09-22T10:00:00"}
    fut = {"last_price": 101, "_received_at": "2026-09-22T10:00:05"}
    assert t.synchronized_basis(cash, fut)["valid"] is False
    fut["_received_at"] = "2026-09-22T10:00:01"
    out = t.synchronized_basis(cash, fut)
    assert out["valid"] is True
    assert out["basis_pct"] == pytest.approx(1.0)


def test_micro_breakout_requires_structure_not_score():
    now = dt.datetime(2026, 9, 22, 10, 6)
    bars = [
        _bar(now-dt.timedelta(minutes=6), 99.2, 100.0, 99.0, 99.8),
        _bar(now-dt.timedelta(minutes=3), 99.7, 100.1, 99.4, 100.0),
    ]
    current = _bar(now, 100.0, 100.4, 99.9, 100.25)
    candidate = {"symbol": "ABC", "direction": "Bullish", "atr": 2.0, "score": 1}
    setup = t.detect_structural_setup(bars, current, candidate, now=now)
    assert setup["setup"] == "MICRO_BREAKOUT"
    assert setup["triggered"] is True
    assert setup["trigger"] == pytest.approx(100.1)
    assert setup["invalidation"] == pytest.approx(99.0)


def test_pre_result_lte_8_dte_defaults_to_next_month():
    now = dt.datetime(2026, 9, 22, 10, 0)
    near = {
        "symbol":"ABC29SEP100CE","type":"CE","strike":100,"expiry":"2026-09-29","dte":7,
        "mid":10.0,"spread_pct":0.5,"delta":0.55,"lot_size":500,
    }
    nxt = {
        "symbol":"ABC27OCT100CE","type":"CE","strike":100,"expiry":"2026-10-27","dte":35,
        "mid":12.0,"spread_pct":0.5,"delta":0.55,"lot_size":500,
    }
    route = t.route_option(
        [near,nxt], direction="Bullish", spot=100, now=now, speed_class="IMPULSE",
        expected_underlying_move_abs=5.0,
        earnings={"pre_result": True},
    )
    assert route["tradeable"] is True
    assert route["preferred_expiry"] == "2026-10-27"
    assert route["pre_result_next_month"] is True
    assert route["contract"]["symbol"] == "ABC27OCT100CE"


def test_friction_gate_can_reject_thin_option_even_with_good_stock_setup():
    now = dt.datetime(2026, 9, 22, 10, 0)
    thin = {
        "symbol":"THIN29SEP100CE","type":"CE","strike":100,"expiry":"2026-09-29","dte":7,
        "mid":5.0,"spread_pct":18.0,"delta":0.5,"lot_size":500,
    }
    route = t.route_option(
        [thin], direction="Bullish", spot=100, now=now, speed_class="IMPULSE",
        expected_underlying_move_abs=1.0, earnings={"pre_result": False},
    )
    assert route["tradeable"] is False
    assert "friction" in route["reason"]


def test_confirmed_earnings_degrades_when_calendar_is_stale():
    now = dt.datetime(2026, 9, 22, 10, 0)
    state = {
        "status":"OK",
        "last_refresh_at":"2026-09-20T08:00:00",
        "events":{"ABC":{"meeting_date":"2026-09-25","state":"ACTIVE","purpose":"Financial Results"}},
    }
    e = t.earnings_context(state,"ABC",now)
    assert e["confidence"] == "STALE"
    assert e["pre_result"] is False


def test_state_machine_never_calls_failed_break_tradeable_in_v1():
    state = t.classify_state(
        {"setup":"FAILED_BREAK_REVERSAL","research_only":True,"triggered":True},
        fast_veto={"veto":False}, stale=False, depth_persist={},
        option_route={"tradeable":True}, active_same_direction=0,
    )
    assert state["state"] == "RESEARCH_ONLY"
    assert state["tradeable"] is False


def test_tactical_pool_is_not_starved_by_empty_legacy_radar():
    rows = [
        {
            "symbol": "DIRECTBULL", "close": 100.0, "atr": 2.0,
            "oi_structure": "Long Buildup", "oi_chg_15m_pct": 0.45,
            "oi_chg_30m_pct": 0.7, "tod_rvol": 1.15,
            "price_chg_60m_pct": 0.2, "compression_score": 70.0,
        },
        {
            "symbol": "DIRECTBEAR", "close": 200.0, "atr": 4.0,
            "oi_structure": "Short Buildup", "oi_chg_15m_pct": 0.35,
            "oi_chg_30m_pct": 0.6, "tod_rvol": 1.1,
            "price_chg_60m_pct": -0.2, "compression_score": 68.0,
        },
    ]
    pool = t.select_tactical_pool({"bullish": [], "bearish": []}, rows)
    by_symbol = {x["symbol"]: x for x in pool}
    assert {"DIRECTBULL", "DIRECTBEAR"} <= set(by_symbol)
    assert by_symbol["DIRECTBULL"]["direction"] == "Bullish"
    assert by_symbol["DIRECTBEAR"]["direction"] == "Bearish"
    assert by_symbol["DIRECTBULL"]["tactical_source"] == "DIRECT_15M"



def test_option_contract_lock_keeps_same_ready_contract_when_still_executable():
    now = dt.datetime(2026, 9, 23, 10, 0)
    locked = {
        "symbol":"ABC29SEP100CE","type":"CE","strike":100,"expiry":"2026-09-29","dte":6,
        "mid":10.0,"spread_pct":0.5,"delta":0.58,"lot_size":500,
    }
    newer = {
        "symbol":"ABC29SEP105CE","type":"CE","strike":105,"expiry":"2026-09-29","dte":6,
        "mid":7.0,"spread_pct":0.4,"delta":0.50,"lot_size":500,
    }
    route = t.route_option(
        [locked, newer], direction="Bullish", spot=104.0, now=now,
        speed_class="IMPULSE", expected_underlying_move_abs=5.0,
        earnings={"pre_result": False},
        locked_contract_symbol="ABC29SEP100CE",
    )
    assert route["tradeable"] is True
    assert route["locked"] is True
    assert route["contract"]["symbol"] == "ABC29SEP100CE"
    assert "ENTRY CONTRACT LOCK" in route["selection_reason"]


def test_option_contract_lock_reroutes_only_with_explicit_reason():
    now = dt.datetime(2026, 9, 23, 10, 0)
    stale_lock = {
        "symbol":"ABC29SEP90CE","type":"CE","strike":90,"expiry":"2026-09-29","dte":6,
        "mid":15.0,"spread_pct":0.5,"delta":0.90,"lot_size":500,
    }
    replacement = {
        "symbol":"ABC29SEP100CE","type":"CE","strike":100,"expiry":"2026-09-29","dte":6,
        "mid":10.0,"spread_pct":0.5,"delta":0.58,"lot_size":500,
    }
    route = t.route_option(
        [stale_lock, replacement], direction="Bullish", spot=101.0, now=now,
        speed_class="IMPULSE", expected_underlying_move_abs=5.0,
        earnings={"pre_result": False},
        locked_contract_symbol="ABC29SEP90CE",
    )
    assert route["tradeable"] is True
    assert route["locked"] is False
    assert route["contract"]["symbol"] == "ABC29SEP100CE"
    assert route["reroute_reason"]
    assert "RE-ROUTED" in route["selection_reason"]



def test_five_minute_witness_is_direction_aware_without_new_score():
    bull = t.five_minute_witness("Bullish", 0.31, 0.18)
    assert bull["state"] == "SUPPORTIVE"

    bear = t.five_minute_witness("Bearish", -0.27, -0.14)
    assert bear["state"] == "SUPPORTIVE"

    mixed = t.five_minute_witness("Bullish", 0.20, -0.03)
    assert mixed["state"] == "MIXED"

    opposing = t.five_minute_witness("Bullish", -0.18, -0.09)
    assert opposing["state"] == "OPPOSING"


def test_option_route_health_separates_transient_execution_noise_from_hard_block():
    healthy = t.option_route_health({"tradeable": True})
    assert healthy["state"] == "HEALTHY"

    degraded = t.option_route_health({
        "tradeable": False,
        "reason": "friction consumes 44.2% of expected premium move",
    })
    assert degraded["state"] == "DEGRADED"

    missing_quote = t.option_route_health({
        "tradeable": False,
        "reason": "missing bid/ask",
    })
    assert missing_quote["state"] == "DEGRADED"

    hard = t.option_route_health({
        "tradeable": False,
        "reason": "no valid option expiry",
    })
    assert hard["state"] == "BLOCKED"

    wait = t.option_route_health(None)
    assert wait["state"] == "WAIT"



def test_opening_range_uses_latest_trading_day_only():
    bars = [
        {"ts": "2026-09-23T09:15:00", "high": 1052.0, "low": 995.0},
        {"ts": "2026-09-23T09:18:00", "high": 1048.0, "low": 1000.0},
        {"ts": "2026-09-24T09:15:00", "high": 1002.0, "low": 997.0},
        {"ts": "2026-09-24T09:18:00", "high": 1004.0, "low": 995.0},
    ]
    high, low = t._opening_range(bars, trading_day=dt.date(2026, 9, 24))
    assert high == 1004.0
    assert low == 995.0


def test_three_minute_builder_drops_stale_partial_after_gap():
    b = t.ThreeMinuteBarBuilder()
    assert b.update(100.0, 1000, dt.datetime(2026, 9, 24, 10, 0, 5)) is None
    assert b.update(100.5, 1010, dt.datetime(2026, 9, 24, 10, 1, 5)) is None

    # A 30-minute gap must not emit the old 10:00 partial bar as a newly
    # completed bar at 10:30.
    completed = b.update(102.0, 1200, dt.datetime(2026, 9, 24, 10, 30, 1))
    assert completed is None
    assert b.current["bucket"] == dt.datetime(2026, 9, 24, 10, 30)


def test_three_minute_builder_still_completes_contiguous_bucket():
    b = t.ThreeMinuteBarBuilder()
    b.update(100.0, 1000, dt.datetime(2026, 9, 24, 10, 0, 5))
    b.update(100.5, 1010, dt.datetime(2026, 9, 24, 10, 2, 55))
    completed = b.update(101.0, 1020, dt.datetime(2026, 9, 24, 10, 3, 1))
    assert completed is not None
    assert completed["ts"].startswith("2026-09-24T10:00:00")
    assert completed["complete"] is True
