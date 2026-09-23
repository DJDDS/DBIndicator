import datetime as dt

from app import v123_focus


def _observer_event(symbol="ABC", direction="Bullish", family="OPENING_DRIVE", price=100.0):
    return {
        "symbol": symbol,
        "direction": direction,
        "event_family": family,
        "live_price": price,
        "day_change_pct": 1.2 if direction == "Bullish" else -1.2,
        "ret_5m_pct": 0.4 if direction == "Bullish" else -0.4,
        "relative_5m_vs_nifty_pct": 0.3 if direction == "Bullish" else -0.3,
        "why": ["live underlying event"],
    }


def _scan(symbol="ABC", price=100.0):
    return {
        "symbol": symbol,
        "close": price,
        "atr": 2.0,
        "prev_close": 99.0,
        "sector": "BANK",
    }


def test_focus_persists_when_latest_snapshot_no_longer_contains_stock():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event()], "leaders": [], "laggards": []}
    state = v123_focus.update_focus(None, observer, {"rows": []}, {"candidates": []}, [_scan()], now=t0)
    assert "ABC" in state["focus"]

    # Three minutes later the discovery event is gone. The stock must remain
    # on the Focus Desk rather than disappearing like the old radar.
    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan()], now=t0 + dt.timedelta(minutes=3)
    )
    assert "ABC" in state["focus"]
    assert state["focus"]["ABC"]["lifecycle"] in ("DISCOVERED", "BUILDING")


def test_bad_option_does_not_invalidate_good_underlying():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event()], "leaders": [], "laggards": []}
    tactical = {
        "candidates": [{
            "symbol": "ABC", "direction": "Bullish", "state": "READY",
            "live_price": 100.2, "trigger": 100.5, "invalidation": 99.2,
            "future_tick_age_s": 1.0,
            "option_route": {"tradeable": False, "reason": "friction consumes 70% of expected premium move"},
        }]
    }
    state = v123_focus.update_focus(None, observer, {"rows": []}, tactical, [_scan()], now=t0)
    row = state["focus"]["ABC"]
    assert row["lifecycle"] == "READY"
    assert row["vehicles"]["option"] == "BLOCKED"
    assert row["vehicles"]["future"] == "ELIGIBLE"
    assert row["vehicles"]["cash"] == "ELIGIBLE"


def test_direction_does_not_flip_from_one_opposite_event():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    state = v123_focus.update_focus(
        None, {"events": [_observer_event(direction="Bullish")], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan()], now=t0
    )
    opposite = {"events": [_observer_event(direction="Bearish", price=99.8)], "leaders": [], "laggards": []}
    state = v123_focus.update_focus(
        state, opposite, {"rows": []}, {"candidates": []}, [_scan(price=99.8)],
        now=t0 + dt.timedelta(minutes=10)
    )
    assert state["focus"]["ABC"]["direction"] == "Bullish"
    assert state["focus"]["ABC"]["opposite_first_seen_at"] is not None


def test_active_stock_becomes_pullback_not_disappears_when_expansion_pauses():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event()], "leaders": [], "laggards": []}
    tactical = {
        "candidates": [{
            "symbol": "ABC", "direction": "Bullish", "state": "TRADEABLE",
            "live_price": 101.0, "trigger": 100.5, "invalidation": 99.5,
            "future_tick_age_s": 1.0, "option_route": {"tradeable": True, "contract": {"symbol": "ABCOPT"}},
        }]
    }
    state = v123_focus.update_focus(None, observer, {"rows": []}, tactical, [_scan()], now=t0)
    assert state["focus"]["ABC"]["lifecycle"] == "ACTIVE"

    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan(price=100.8)], now=t0 + dt.timedelta(minutes=6)
    )
    assert "ABC" in state["focus"]
    assert state["focus"]["ABC"]["lifecycle"] == "PULLBACK"


def test_reentry_ready_after_pullback_is_same_thesis():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event()], "leaders": [], "laggards": []}
    trade = {"candidates": [{
        "symbol": "ABC", "direction": "Bullish", "state": "TRADEABLE",
        "live_price": 101.0, "trigger": 100.5, "invalidation": 99.5,
    }]}
    state = v123_focus.update_focus(None, observer, {"rows": []}, trade, [_scan()], now=t0)
    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan()], now=t0 + dt.timedelta(minutes=5)
    )
    ready = {"candidates": [{
        "symbol": "ABC", "direction": "Bullish", "state": "READY",
        "live_price": 100.9, "trigger": 101.1, "invalidation": 100.2,
    }]}
    state = v123_focus.update_focus(
        state, {"events": [_observer_event(family="PULLBACK_RECLAIM", price=100.9)], "leaders": [], "laggards": []},
        {"rows": []}, ready, [_scan()], now=t0 + dt.timedelta(minutes=8)
    )
    assert state["focus"]["ABC"]["lifecycle"] == "REENTRY_READY"


def test_tactical_candidates_come_only_from_focus_desk():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    state = v123_focus.update_focus(
        None, {"events": [_observer_event("ABC")], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan("ABC"), _scan("OTHER")], now=t0
    )
    rows = v123_focus.tactical_candidates(state, [_scan("ABC"), _scan("OTHER")])
    assert [x["symbol"] for x in rows] == ["ABC"]
    assert rows[0]["direction"] == "Bullish"


def test_missed_mover_is_audited_even_when_not_selected():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {
        "events": [_observer_event("ABC")],
        "leaders": [{"symbol": "MISSED", "day_change_pct": 3.1, "ret_5m_pct": 0.8}],
        "laggards": [],
    }
    state = v123_focus.update_focus(
        None, observer, {"rows": []}, {"candidates": []}, [_scan("ABC"), _scan("MISSED")], now=t0
    )
    assert "MISSED" in state["missed"]
    assert state["missed"]["MISSED"]["reason"] == "MOVE_WITHOUT_QUALIFYING_LIVE_EVENT"


def test_new_trading_day_clears_yesterdays_focus():
    t0 = dt.datetime(2026, 9, 23, 15, 0)
    state = v123_focus.update_focus(
        None, {"events": [_observer_event("ABC")], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan("ABC")], now=t0
    )
    assert "ABC" in state["focus"]
    t1 = dt.datetime(2026, 9, 24, 9, 20)
    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []}, [_scan("ABC")], now=t1
    )
    assert state["focus"] == {}
    assert state["trade_date"] == "2026-09-24"
