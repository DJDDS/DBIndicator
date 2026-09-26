import datetime as dt

from app import v123_swing


def row(symbol, *, close=101.0, prev=100.0, atr=2.0, htf="Bullish",
        hi20=100.8, lo20=90.0, sector_lead=0.5, oi="Long Buildup",
        rvol=1.2, oi60=0.4):
    return {
        "symbol": symbol,
        "close": close,
        "prev_close": prev,
        "atr": atr,
        "prior_high_20d": hi20,
        "prior_low_20d": lo20,
        "htf_direction": htf,
        "stock_sector_lead_pct": sector_lead,
        "oi_structure": oi,
        "tod_rvol": rvol,
        "oi_chg_60m_pct": oi60,
        "price_chg_today_pct": (close / prev - 1) * 100,
        "sector": "METAL",
        "avwap": 100.2,
    }


def test_1d_focus_only_refreshes_at_scheduled_windows():
    before = dt.datetime(2026, 9, 23, 9, 40)
    state = v123_swing.update(None, [row("A")], now=before)
    assert state["selected"] == {}

    morning = dt.datetime(2026, 9, 23, 9, 46)
    state = v123_swing.update(state, [row("A")], now=morning)
    assert "A" in state["selected"]
    assert state["last_refresh_slot"] == "MORNING"

    # A stronger-looking new name between checkpoints must not churn membership.
    state = v123_swing.update(state, [row("A"), row("B", close=103.0)], now=dt.datetime(2026, 9, 23, 10, 30))
    assert "A" in state["selected"]
    assert "B" not in state["selected"]


def test_1d_midday_fills_empty_slots_without_kicking_existing():
    t0 = dt.datetime(2026, 9, 23, 9, 46)
    state = v123_swing.update(None, [row("A")], now=t0)
    state = v123_swing.update(state, [row("A"), row("B")], now=dt.datetime(2026, 9, 23, 12, 31))
    assert {"A", "B"} <= set(state["selected"])
    assert state["last_refresh_slot"] == "MIDDAY"


def test_1d_final_phase_freezes_membership_into_close():
    state = v123_swing.update(None, [row("A"), row("B")], now=dt.datetime(2026, 9, 23, 14, 31))
    selected = set(state["selected"])
    assert state["phase"] == "FINAL / FROZEN INTO CLOSE"

    state = v123_swing.update(state, [row("A"), row("B"), row("C")], now=dt.datetime(2026, 9, 23, 15, 0))
    assert set(state["selected"]) == selected


def test_1d_marks_extended_move_do_not_chase():
    # Extension is now measured from the actionable trigger, not prev close.
    cand = v123_swing._candidate(row("A", close=106.5, prev=100.0, atr=2.0, hi20=103.5))
    assert cand["runway"] == "EXTENDED"
    assert "DO NOT CHASE" in cand["action"]


def test_1d_thesis_removal_requires_invalidation_not_ranking_change():
    state = v123_swing.update(None, [row("A")], now=dt.datetime(2026, 9, 23, 9, 46))
    invalid = row("A", close=99.0, prev=100.0, atr=2.0, hi20=100.8)
    state = v123_swing.update(state, [invalid], now=dt.datetime(2026, 9, 23, 10, 15))
    assert "A" not in state["selected"]



def test_swing_move_consumed_is_measured_from_trigger_not_previous_close():
    cand = v123_swing._candidate(
        row("A", close=101.0, prev=100.0, atr=2.0, hi20=100.8)
    )
    assert cand["trigger"] == 100.8
    assert cand["move_consumed_atr"] == 0.1


def test_swing_invalidation_always_stays_on_adverse_side_with_half_atr_minimum():
    bull = v123_swing._candidate(
        row("BULL", close=101.0, prev=100.0, atr=2.0, hi20=100.8)
    )
    assert bull["invalidation"] <= bull["trigger"] - 1.0

    bear_row = row(
        "BEAR", close=99.0, prev=100.0, atr=2.0, htf="Bearish",
        hi20=110.0, lo20=99.2, sector_lead=-0.5, oi="Short Buildup",
    )
    bear_row["avwap"] = 99.8
    bear = v123_swing._candidate(bear_row)
    assert bear["invalidation"] >= bear["trigger"] + 1.0
