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
