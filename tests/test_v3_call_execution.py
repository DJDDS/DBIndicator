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
