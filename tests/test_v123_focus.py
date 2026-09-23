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



def test_invalidated_live_setup_is_recorded_once_not_repeated_each_callback():
    t0 = dt.datetime(2026, 9, 23, 11, 32, 0)
    observer = {
        "events": [_observer_event("ABB", direction="Bullish", family="PRESSURE_SHIFT", price=7125.0)],
        "leaders": [], "laggards": [],
    }
    # Mirrors the production glitch: live price is already below the bullish
    # invalidation level, while the same tactical snapshot remains published.
    tactical = {
        "candidates": [{
            "symbol": "ABB", "direction": "Bullish", "state": "READY",
            "live_price": 7125.0, "trigger": 7130.0, "invalidation": 7126.0,
            "option_route": {"tradeable": False, "reason": "friction consumes 66.1% of expected premium move"},
        }]
    }

    state = None
    for i in range(12):
        state = v123_focus.update_focus(
            state, observer, {"rows": []}, tactical, [_scan("ABB", 7125.0)],
            now=t0 + dt.timedelta(seconds=i * 5),
        )

    assert "ABB" not in state["focus"]
    assert "ABB" in state["continuation_watch"]
    assert state["continuation_watch"]["ABB"]["lifecycle"] == "CONTINUATION_WATCH"
    assert not [x for x in state["recent"] if x.get("symbol") == "ABB"]


def test_recent_cleanup_collapses_duplicate_cards_for_same_symbol_direction():
    now = dt.datetime(2026, 9, 23, 11, 40)
    rows = [
        {
            "symbol": "ABB", "direction": "Bullish", "lifecycle": "INVALIDATED",
            "completed_at": "2026-09-23T11:32:01", "trigger": 7130.0, "invalidation": 7126.0,
        },
        {
            "symbol": "ABB", "direction": "Bullish", "lifecycle": "INVALIDATED",
            "completed_at": "2026-09-23T11:32:05", "trigger": 7130.0, "invalidation": 7126.0,
        },
    ]
    cleaned = v123_focus._recent_cleanup(rows, now)
    assert len(cleaned) == 1
    assert cleaned[0]["completed_at"] == "2026-09-23T11:32:05"



def test_profitable_fast_exit_becomes_proven_mover_not_invalidated():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event("JINDALSTEL")], "leaders": [], "laggards": []}
    trade = {"candidates": [{
        "symbol": "JINDALSTEL", "direction": "Bullish", "state": "TRADEABLE",
        "live_price": 1150.0, "trigger": 1148.0, "invalidation": 1140.0,
        "entry_episode_no": 1, "entry_episode_open": True,
        "locked_option_contract": "JINDALSTEL1160CE",
        "locked_option_strike": 1160.0, "locked_option_delta": 0.54,
        "option_route": {"tradeable": True, "contract": {"symbol": "JINDALSTEL1160CE"}},
    }]}
    state = v123_focus.update_focus(None, observer, {"rows": []}, trade, [_scan("JINDALSTEL", 1150.0)], now=t0)
    assert state["focus"]["JINDALSTEL"]["lifecycle"] == "ACTIVE"

    exited = {"candidates": [{
        "symbol": "JINDALSTEL", "direction": "Bullish", "state": "EXIT",
        "reason": "3m profit-protection structure lost",
        "live_price": 1160.0, "trigger": 1148.0, "invalidation": 1140.0,
        "entry_episode_no": 1, "entry_episode_open": False,
        "entry_episode_result": "PROVEN_MOVE",
        "locked_option_contract": "JINDALSTEL1160CE",
        "locked_option_strike": 1160.0, "locked_option_delta": 0.58,
        "option_route": {"tradeable": True, "contract": {"symbol": "JINDALSTEL1160CE"}},
    }]}
    state = v123_focus.update_focus(
        state, observer, {"rows": []}, exited, [_scan("JINDALSTEL", 1160.0)],
        now=t0 + dt.timedelta(minutes=6)
    )
    assert "JINDALSTEL" not in state["focus"]
    row = state["continuation_watch"]["JINDALSTEL"]
    assert row["lifecycle"] == "CONTINUATION_WATCH"
    assert row["locked_option_contract"] == "JINDALSTEL1160CE"
    assert row["entry_episode_result"] == "PROVEN_MOVE"


def test_micro_exit_moves_to_continuation_until_broader_thesis_breaks():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    state = v123_focus.update_focus(
        None, {"events": [_observer_event("ABC", price=101.0)], "leaders": [], "laggards": []},
        {"rows": []},
        {"candidates": [{"symbol":"ABC","direction":"Bullish","state":"TRADEABLE","live_price":101.0,"trigger":100.5,"invalidation":99.5}]},
        [_scan("ABC", 101.0)], now=t0
    )
    exited = {"candidates": [{
        "symbol":"ABC","direction":"Bullish","state":"EXIT",
        "reason":"underlying structural invalidation hit",
        "live_price":99.4,"trigger":100.5,"invalidation":99.5,
    }]}
    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, exited, [_scan("ABC",99.4)], now=t0+dt.timedelta(minutes=5)
    )
    assert "ABC" in state["continuation_watch"]
    thesis_level = state["continuation_watch"]["ABC"]["thesis_invalidation"]
    assert thesis_level < 99.5

    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows": []}, {"candidates": []},
        [_scan("ABC", thesis_level - 0.1)], now=t0+dt.timedelta(minutes=8)
    )
    assert "ABC" not in state["continuation_watch"]
    assert any(x.get("symbol") == "ABC" and x.get("lifecycle") == "INVALIDATED" for x in state["recent"])


def test_aubank_fresh_continuation_event_rearms_same_thesis():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    observer = {"events": [_observer_event("AUBANK", price=100.0)], "leaders": [], "laggards": []}
    trade = {"candidates": [{
        "symbol":"AUBANK","direction":"Bullish","state":"TRADEABLE",
        "live_price":100.0,"trigger":99.8,"invalidation":99.2,
    }]}
    state = v123_focus.update_focus(None, observer, {"rows":[]}, trade, [_scan("AUBANK",100.0)], now=t0)

    exited = {"candidates": [{
        "symbol":"AUBANK","direction":"Bullish","state":"EXIT",
        "reason":"3m entry structure lost","live_price":99.7,
        "trigger":99.8,"invalidation":99.2,
    }]}
    state = v123_focus.update_focus(
        state, {"events": [], "leaders": [], "laggards": []},
        {"rows":[]}, exited, [_scan("AUBANK",99.7)], now=t0+dt.timedelta(minutes=3)
    )
    assert "AUBANK" in state["continuation_watch"]

    rearm = {
        "events": [_observer_event("AUBANK", family="PULLBACK_RECLAIM", price=100.4)],
        "leaders": [], "laggards": [],
    }
    state = v123_focus.update_focus(
        state, rearm, {"rows":[]}, {"candidates":[]},
        [_scan("AUBANK",100.4)], now=t0+dt.timedelta(minutes=8)
    )
    assert "AUBANK" in state["focus"]
    assert "AUBANK" not in state["continuation_watch"]
    assert state["focus"]["AUBANK"]["rearmed_at"] is not None


def test_continuation_watch_stays_in_deep_tactical_pool():
    t0 = dt.datetime(2026, 9, 23, 10, 0)
    state = v123_focus.update_focus(
        None, {"events": [_observer_event("AUBANK")], "leaders": [], "laggards": []},
        {"rows":[]},
        {"candidates":[{"symbol":"AUBANK","direction":"Bullish","state":"READY","live_price":100.0,"trigger":100.2,"invalidation":100.1}]},
        [_scan("AUBANK",100.0)], now=t0
    )
    assert "AUBANK" in state["continuation_watch"]
    rows = v123_focus.tactical_candidates(state, [_scan("AUBANK",100.0)])
    assert any(x["symbol"] == "AUBANK" for x in rows)


def test_forensics_records_exact_discovery_gate_failure():
    t0 = dt.datetime(2026, 9, 23, 11, 0)
    observer = {
        "events": [],
        "leaders": [{
            "symbol":"MISS","day_change_pct":2.4,"ret_3m_pct":0.05,"ret_5m_pct":0.18,
            "ret_10m_pct":0.30,"relative_5m_vs_nifty_pct":0.02,
            "volume_rate_accel":0.9,"discovery_reason":"NO_EVENT_FAMILY_QUALIFIED",
            "discovery_failed_gates":["5m move < 0.20%","volume rate < 1.20x","relative 5m < 0.08%"],
        }],
        "laggards": [],
    }
    state = v123_focus.update_focus(
        None, observer, {"rows":[]}, {"candidates":[]}, [_scan("MISS",102.4)], now=t0
    )
    row = state["forensics"]["MISS"]
    assert row["stage"] == "DISCOVERY"
    assert "volume rate < 1.20x" in row["reason"]
    assert row["discovery_failed_gates"]
