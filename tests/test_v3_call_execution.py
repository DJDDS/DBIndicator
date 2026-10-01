import datetime as dt
import math

from app import v3_call_execution


FIXTURE = {
    "prem_ret1m_pct": None,
    "prem_ret3m_pct": None,
    "prem_ret5m_pct": None,
    "prem_accel": None,
    "iv": 27.140818,
    "iv_chg1m": None,
    "iv_chg3m": None,
    "iv_chg5m": None,
    "oi_chg1m_pct": None,
    "oi_chg3m_pct": None,
    "oi_chg5m_pct": None,
    "log_oi": 8.160803920954665,
    "vol_ratio10": 0.0,
    "log_volume": 0.0,
    "spot_ret1m_bps": None,
    "spot_ret3m_bps": None,
    "spot_ret5m_bps": None,
    "spot_accel_bps": None,
    "moneyness_pct": -0.6470165348670063,
    "abs_delta": 0.5129219647059814,
    "dte_days": 34.0,
    "premium_pct_spot": 3.459741193386053,
    "elasticity": 14.82544317726795,
    "required_med_bps": 12.181086112616253,
    "minute_of_day": 19.0,
    "offset": 0.0,
    "code": 2.0,
}


def test_frozen_model_probability_is_exact():
    got = v3_call_execution.score_features(FIXTURE)
    assert math.isclose(got, 0.8007769137159749, rel_tol=0.0, abs_tol=1e-12)


def test_bearish_path_is_intentionally_disabled():
    out = v3_call_execution.evaluate_call_candidates(
        [], {}, [], now=dt.datetime(2026, 9, 30, 10, 0),
        direction="Bearish", spot=100.0,
    )
    assert out["state"] == "NOT_APPLICABLE"
    assert out["pass"] is False
    assert out["evaluation_horizon_seconds"] == 300


def test_near_expiry_prefers_next_month():
    now = dt.datetime(2026, 9, 28, 10, 0)
    snaps = [
        {"expiry": "2026-09-29"},
        {"expiry": "2026-10-27"},
    ]
    expiry, code = v3_call_execution._preferred_expiry(snaps, now)
    assert expiry == dt.date(2026, 10, 27)
    assert code == 2


def test_bullish_without_option_history_is_warming():
    now = dt.datetime(2026, 9, 30, 10, 0)
    snaps = [{
        "type": "CE", "symbol": "TEST26OCT100CE", "strike": 100.0,
        "expiry": "2026-10-27", "dte": 27, "mid": 5.0, "bid": 4.9,
        "ask": 5.1, "spread_pct": 4.0, "iv_pct": 25.0, "delta": 0.5,
        "oi": 1000, "volume": 100,
    }]
    out = v3_call_execution.evaluate_call_candidates(
        snaps, {}, [], now=now, direction="Bullish", spot=100.0,
    )
    assert out["state"] in {"WARMING", "BLOCKED"}
    assert out["pass"] is False
    assert out["controls_trading"] is False


def test_focus_dashboard_surfaces_call_v1_decision():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "CALL V1 PASS" in html
    assert "CALL V1 BELOW GATE" in html
    assert "CALL V1 WARMING" in html
    assert "PE execution model not validated" in html
    assert "v123-kpi-callrec" in html
    assert "call_v1_forward_recorder" in html


def test_call_v1_pass_age_resets_when_pass_breaks_or_contract_changes():
    from app.v122b_stream import _annotate_call_v1_pass_age

    store = {}
    t0 = dt.datetime(2026, 10, 1, 10, 0, 0)
    base = {
        "state": "PASS",
        "pass": True,
        "selected_contract": "TEST26OCT100CE",
    }

    first = _annotate_call_v1_pass_age(store, "TEST", base, t0)
    assert first["pass_age_seconds"] == 0.0
    assert first["pass_age_label"] == "FRESH"

    second = _annotate_call_v1_pass_age(
        store, "TEST", base, t0 + dt.timedelta(seconds=45)
    )
    assert second["pass_age_seconds"] == 45.0
    assert second["pass_age_label"] == "PERSISTING"

    third = _annotate_call_v1_pass_age(
        store, "TEST", base, t0 + dt.timedelta(seconds=181)
    )
    assert third["pass_age_label"] == "EXTENDED"

    broken = _annotate_call_v1_pass_age(
        store,
        "TEST",
        {"state": "BELOW_GATE", "pass": False, "selected_contract": "TEST26OCT100CE"},
        t0 + dt.timedelta(seconds=182),
    )
    assert broken["pass_age_seconds"] is None
    assert "TEST" not in store

    restarted = _annotate_call_v1_pass_age(
        store, "TEST", base, t0 + dt.timedelta(seconds=184)
    )
    assert restarted["pass_age_seconds"] == 0.0
    assert restarted["pass_age_label"] == "FRESH"

    changed = _annotate_call_v1_pass_age(
        store,
        "TEST",
        dict(base, selected_contract="TEST26OCT105CE"),
        t0 + dt.timedelta(seconds=190),
    )
    assert changed["pass_age_seconds"] == 0.0
    assert changed["pass_age_label"] == "FRESH"


def test_focus_dashboard_displays_call_v1_pass_age():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "pass_age_seconds" in html
    assert "pass_age_label" in html
    assert "PASS age is descriptive only, not extra confidence" in html


def test_blocked_reason_distinguishes_missing_ticks_from_illiquid_chain():
    now = dt.datetime(2026, 10, 1, 10, 0)
    base = {"type": "CE", "expiry": "2026-10-27", "dte": 26, "oi": 1000, "volume": 0}
    no_ticks = [
        dict(base, symbol=f"TEST26OCT{k}CE", strike=float(k), mid=None, bid=None, ask=None,
             spread_pct=None, iv_pct=None, delta=None)
        for k in (95, 100, 105)
    ]
    out = v3_call_execution.evaluate_call_candidates(
        no_ticks, {}, [], now=now, direction="Bullish", spot=100.0,
    )
    assert out["state"] == "BLOCKED"
    assert out["reason"] == "no live ATM±1 CALL ticks yet (subscribed, waiting for quotes)"
    assert out["diagnostics"]["no_live_quote"] == 3
    assert out["controls_trading"] is False


def test_live_snapshots_without_spot_field_are_scored():
    """Production snapshots have no 'spot' key; the evaluator must use the live
    underlying price instead of silently blocking every contract."""
    now = dt.datetime(2026, 10, 1, 10, 30)
    snaps, history = [], {}
    for k in (95.0, 100.0, 105.0):
        sym = f"TEST26OCT{int(k)}CE"
        snaps.append({"type": "CE", "symbol": sym, "strike": k, "expiry": "2026-10-27", "dte": 26,
                      "mid": 5.0, "bid": 4.95, "ask": 5.05, "spread_pct": 2.0,
                      "iv_pct": 25.0, "delta": 0.5, "oi": 1000, "volume": 500})
        history[sym] = [{"ts": now - dt.timedelta(minutes=m), "mid": 5.0 - m * 0.02, "iv_pct": 25.0,
                         "oi": 1000, "cum_volume": 500 - m * 10} for m in range(12, -1, -1)]
    cash = [{"ts": now - dt.timedelta(minutes=m), "price": 100.0 - m * 0.01} for m in range(12, -1, -1)]
    out = v3_call_execution.evaluate_call_candidates(
        snaps, history, cash, now=now, direction="Bullish", spot=100.0,
    )
    assert out["state"] in {"PASS", "BELOW_GATE"}, out
    assert out["selected_contract"].startswith("TEST26OCT")
    assert out["controls_trading"] is False


def test_minute_latch_scores_once_per_minute_and_holds_trade_window():
    latch = v3_call_execution.MinuteDecisionLatch()
    calls = []
    probs = iter([0.70, 0.40, 0.40])

    def evaluate(scored_at):
        calls.append(scored_at)
        p = next(probs)
        passed = p >= v3_call_execution.MODEL_THRESHOLD
        return {"state": "PASS" if passed else "BELOW_GATE", "pass": passed, "probability": p,
                "selected_contract": "ABC26OCT100CE", "selected_ask": 5.05, "selected_mid": 5.0}

    t0 = dt.datetime(2026, 10, 2, 10, 31, 1)
    first = latch.decide("ABC", "Bullish", t0, evaluate)
    assert first["state"] == "PASS"
    assert first["trade_window"]["phase"] == "ENTRY_OPEN"
    assert calls == [dt.datetime(2026, 10, 2, 10, 31)]      # feature clock = minute

    # Same minute, many 2-second cycles: no re-scoring, decision held.
    for sec in range(3, 60, 2):
        out = latch.decide("ABC", "Bullish", t0.replace(second=sec), evaluate)
        assert out["state"] == "PASS"
    assert len(calls) == 1

    # Next minute scores below gate, but the open trade continues (HOLDING).
    later = latch.decide("ABC", "Bullish", dt.datetime(2026, 10, 2, 10, 32, 30), evaluate)
    assert later["state"] == "BELOW_GATE"
    assert later["trade_window"]["phase"] == "HOLDING"
    assert later["trade_window"]["contract"] == "ABC26OCT100CE"
    assert later["trade_window"]["reference_ask"] == 5.05

    # After the 5-minute horizon the window closes.
    done = latch.decide("ABC", "Bullish", dt.datetime(2026, 10, 2, 10, 36, 5), evaluate)
    assert done["trade_window"] is None
    assert len(calls) == 3


def test_dashboard_renders_call_v1_trade_window():
    from pathlib import Path
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "CALL V1 ENTRY OPEN" in html
    assert "CALL V1 HOLDING" in html
    assert "below gate (trade unchanged)" in html
