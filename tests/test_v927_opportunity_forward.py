import datetime as dt

from app import opportunity_forward


def _radar(score=80.0):
    return {
        "market_bias": "Bearish",
        "market_bias_strength_pct": 62,
        "bullish": [],
        "bearish": [{
            "symbol": "ABC", "direction": "Bearish", "score": score,
            "status": "HIGH ATTENTION", "oi_structure": "Short Buildup",
            "chase_guard": "OK",
        }],
    }


def test_forward_state_records_once_per_symbol_direction_trading_day():
    state = opportunity_forward.empty_state()
    now = dt.datetime(2026, 8, 31, 10, 0, 0)
    rows = [{"symbol": "ABC", "close": 100.0}]

    state = opportunity_forward.process_scan(state, _radar(), rows, now=now)
    state = opportunity_forward.process_scan(state, _radar(score=90), rows, now=now + dt.timedelta(minutes=3))

    assert len(state["events"]) == 1
    event = state["events"][0]
    assert event["entry_price"] == 100.0
    assert event["score"] == 80.0
    assert event["direction"] == "Bearish"


def test_forward_state_resolves_due_intraday_horizon_with_directional_return():
    state = opportunity_forward.empty_state()
    start = dt.datetime(2026, 8, 31, 10, 0, 0)
    state = opportunity_forward.process_scan(state, _radar(), [{"symbol": "ABC", "close": 100.0}], now=start)

    state = opportunity_forward.process_scan(
        state, _radar(), [{"symbol": "ABC", "close": 98.0}], now=start + dt.timedelta(minutes=31)
    )

    result = state["events"][0]["outcomes"]["30m"]
    assert result["exit_price"] == 98.0
    assert result["directional_return_pct"] == 2.0
    assert result["win"] is True


def test_forward_state_resolves_1d_on_next_session_at_or_after_entry_time():
    state = opportunity_forward.empty_state()
    start = dt.datetime(2026, 8, 31, 11, 0, 0)
    state = opportunity_forward.process_scan(state, _radar(), [{"symbol": "ABC", "close": 100.0}], now=start)

    # Earlier next day is not yet the same-time 1D horizon.
    state = opportunity_forward.process_scan(
        state, {"bullish": [], "bearish": []}, [{"symbol": "ABC", "close": 97.0}],
        now=dt.datetime(2026, 9, 1, 10, 30, 0),
    )
    assert "1D" not in state["events"][0]["outcomes"]

    state = opportunity_forward.process_scan(
        state, {"bullish": [], "bearish": []}, [{"symbol": "ABC", "close": 96.0}],
        now=dt.datetime(2026, 9, 1, 11, 2, 0),
    )
    assert state["events"][0]["outcomes"]["1D"]["directional_return_pct"] == 4.0


def test_forward_summary_reports_horizon_win_rate_average_and_score_bands():
    state = opportunity_forward.empty_state()
    start = dt.datetime(2026, 8, 31, 10, 0, 0)
    state = opportunity_forward.process_scan(state, _radar(85), [{"symbol": "ABC", "close": 100.0}], now=start)
    state = opportunity_forward.process_scan(state, _radar(85), [{"symbol": "ABC", "close": 99.0}], now=start + dt.timedelta(minutes=31))

    summary = opportunity_forward.summarize(state)
    assert summary["events"] == 1
    assert summary["horizons"]["30m"]["n"] == 1
    assert summary["horizons"]["30m"]["win_rate_pct"] == 100.0
    assert summary["horizons"]["30m"]["avg_directional_return_pct"] == 1.0
    assert summary["score_bands"]["70+"]["30m"]["n"] == 1
