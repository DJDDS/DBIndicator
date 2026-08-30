import datetime as dt

from app import v9_playbooks as v9


def base(direction="Bullish"):
    return {
        "symbol": "TEST",
        "breakout_direction": direction,
        "v8_direction": direction,
        "fresh_breakout": True,
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.4,
        "v8_participation": 82.0,
        "v8_relative": 78.0,
        "v8_derivatives": 74.0,
        "v8_structure": 80.0,
        "v8_oi_state": "Long Buildup" if direction == "Bullish" else "Fresh Short Buildup",
        "close_position_pct": 88.0 if direction == "Bullish" else 12.0,
        "basis_acceleration": 0.0,
        "vwap_side_agrees": True,
        "vwap_distance_atr": 0.25,
        "tod_rvol": 2.2,
        "opening_rvol": 2.0,
    }


def test_bull_opening_drive_is_early_session_playbook():
    row = base("Bullish")
    row["breakout_source"] = "Opening Range"
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 10, 0))
    play = next(p for p in plays if p["playbook"] == v9.BULL_OPENING_DRIVE)
    assert play["side"] == "Bullish"
    assert play["state"] == "TRADE CANDIDATE"
    assert "intraday" in play["modes"]


def test_opening_drive_does_not_fire_after_morning_window():
    row = base("Bullish")
    row["breakout_source"] = "Opening Range"
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 12, 0))
    assert all(p["playbook"] != v9.BULL_OPENING_DRIVE for p in plays)


def test_bull_pullback_reclaim_requires_confirmed_retest():
    row = base("Bullish")
    row.update({
        "fresh_breakout": False,
        "breakout_direction": None,
        "retained_breakout_direction": "Bullish",
        "retained_breakout_source": "Recent Range",
        "breakout_retest_confirmed": True,
        "retest_confirmed": True,
    })
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 11, 0))
    play = next(p for p in plays if p["playbook"] == v9.BULL_PULLBACK_RECLAIM)
    assert play["state"] == "TRADE CANDIDATE"
    assert set(play["modes"]) == {"intraday", "swing"}


def test_bear_fresh_short_buildup_requires_fresh_short_oi_state():
    row = base("Bearish")
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 11, 0))
    assert any(p["playbook"] == v9.BEAR_FRESH_SHORT_BUILDUP and p["state"] == "TRADE CANDIDATE" for p in plays)
    row["v8_oi_state"] = "Long Unwinding"
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 11, 0))
    assert all(p["playbook"] != v9.BEAR_FRESH_SHORT_BUILDUP for p in plays)


def test_bear_failed_breakout_is_not_mirrored_bear_breakdown():
    row = base("Bullish")
    row.update({
        "fresh_breakout": False,
        "breakout_direction": None,
        "v8_direction": None,
        "failed_breakout_direction": "Bearish",
        "failed_breakout_source": "Recent Range",
        "failed_breakout_level": 100.0,
        "failed_breakout_vwap_reject": True,
        "close_position_pct": 18.0,
    })
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 11, 30))
    play = next(p for p in plays if p["playbook"] == v9.BEAR_FAILED_BREAKOUT)
    assert play["side"] == "Bearish"
    assert play["state"] == "TRADE CANDIDATE"


def test_bear_vwap_retest_failure_requires_bearish_retest_confirmation():
    row = base("Bearish")
    row.update({
        "fresh_breakout": False,
        "breakout_direction": None,
        "retained_breakout_direction": "Bearish",
        "retained_breakout_source": "Recent Range",
        "breakout_retest_confirmed": True,
        "retest_confirmed": True,
    })
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 12, 0))
    play = next(p for p in plays if p["playbook"] == v9.BEAR_VWAP_RETEST_FAILURE)
    assert play["state"] == "TRADE CANDIDATE"


def test_real_catalyst_is_live_only_and_uses_headline_event_not_price_proxy():
    articles = [{
        "title": "TEST wins major order worth Rs 2,000 crore",
        "published_at": "2026-08-30T09:20:00+05:30",
        "sentiment_score": 0.7,
    }]
    catalyst = v9.score_real_catalyst(articles, now=dt.datetime.fromisoformat("2026-08-30T10:00:00+05:30"))
    assert catalyst["score"] >= 80
    row = base("Bullish")
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 10, 0), news_articles=articles)
    play = next(p for p in plays if p["playbook"] == v9.BULL_CATALYST_CONTINUATION)
    assert play["historical_status"] == "LIVE_SHADOW"
    assert play["state"] == "TRADE CANDIDATE"


def test_anti_chase_blocks_trade_candidate_across_playbooks():
    row = base("Bearish")
    row["breakout_extension_atr"] = 1.6
    plays = v9.evaluate_row(row, now=dt.datetime(2026, 8, 30, 11, 0))
    assert not any(p["state"] == "TRADE CANDIDATE" for p in plays)
