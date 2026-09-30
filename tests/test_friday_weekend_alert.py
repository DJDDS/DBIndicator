import datetime as dt

import pytest


def _mod():
    from app import friday_weekend_alert as mod
    return mod


def test_normal_friday_is_validated_session():
    mod = _mod()
    assert mod.session_mode(dt.date(2026, 10, 9), set()) == "FRIDAY_VALIDATED"


def test_thursday_is_shadow_only_when_next_friday_is_fo_holiday():
    mod = _mod()
    holidays = {dt.date(2026, 10, 2)}
    assert mod.session_mode(dt.date(2026, 10, 1), holidays) == "HOLIDAY_WEEKEND_SHADOW"
    assert mod.session_mode(dt.date(2026, 10, 8), holidays) is None


def test_signal_direction_uses_frozen_point_two_percent_threshold():
    mod = _mod()
    assert mod.signal_from_open(25000.0, 25050.0)["signal"] == "BUY CE"
    assert mod.signal_from_open(25000.0, 24950.0)["signal"] == "BUY PE"
    assert mod.signal_from_open(25000.0, 25049.0)["signal"] == "NO TRADE"
    assert mod.signal_from_open(25000.0, 24951.0)["signal"] == "NO TRADE"


def test_atm_strike_is_nearest_nifty_50_point_strike():
    mod = _mod()
    assert mod.atm_strike(24876.0) == 24900.0
    assert mod.atm_strike(24874.0) == 24850.0


def test_expiry_roll_uses_actual_trading_sessions_not_weekdays():
    mod = _mod()
    today = dt.date(2026, 9, 24)
    expiries = [dt.date(2026, 9, 29), dt.date(2026, 10, 27)]
    holidays = {dt.date(2026, 9, 25)}
    selected, sessions = mod.select_monthly_expiry(today, expiries, holidays, roll_days=5)
    assert selected == dt.date(2026, 10, 27)
    assert sessions > 5


def test_nse_payload_parser_reads_fo_holidays():
    mod = _mod()
    payload = {
        "FO": [
            {"tradingDate": "02-Oct-2026", "description": "Mahatma Gandhi Jayanti"},
            {"tradingDate": "25-Dec-2026", "description": "Christmas"},
        ]
    }
    assert mod.parse_nse_fo_holidays(payload) == {
        dt.date(2026, 10, 2),
        dt.date(2026, 12, 25),
    }


def test_capture_window_starts_at_1415_and_fails_closed_outside():
    mod = _mod()
    assert mod.in_capture_window(dt.datetime(2026, 10, 9, 14, 15))
    assert mod.in_capture_window(dt.datetime(2026, 10, 9, 15, 25))
    assert not mod.in_capture_window(dt.datetime(2026, 10, 9, 14, 14))
    assert not mod.in_capture_window(dt.datetime(2026, 10, 9, 15, 26))
