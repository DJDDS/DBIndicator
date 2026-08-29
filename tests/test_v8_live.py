import sys
import types

import numpy as np
import pandas as pd

if "kiteconnect" not in sys.modules:
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = type("KiteConnect", (), {})
    mod.KiteTicker = type("KiteTicker", (), {})
    sys.modules["kiteconnect"] = mod

from app import background, indicators


def _row(symbol, direction, *, close, high, low, rvol, oi, ret4, rs, sector_lead, ext=0.3):
    return {
        "symbol": symbol,
        "direction": direction,
        "breakout_direction": direction,
        "breakout_source": "Recent Range",
        "breakout_extension_atr": ext,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100000,
        "tod_rvol": rvol,
        "opening_rvol": rvol,
        "bar_range_atr": 1.0,
        "gap_atr": 0.6 if direction == "Bullish" else -0.6,
        "ret_4": ret4,
        "rs_pct": rs,
        "stock_sector_lead_pct": sector_lead,
        "oi_chg_60m_pct": oi,
        "basis_acceleration": 0.05 if direction == "Bullish" else -0.05,
    }


def test_apply_v8_dual_alpha_mutates_live_rows_and_keeps_both_sides():
    rows = [
        _row("BULL", "Bullish", close=109, high=110, low=100, rvol=2.2, oi=3.0, ret4=1.2, rs=2.0, sector_lead=1.5),
        _row("BEAR", "Bearish", close=101, high=110, low=100, rvol=2.1, oi=2.8, ret4=-1.1, rs=-1.8, sector_lead=-1.4),
        _row("DULL", "Bullish", close=105, high=110, low=100, rvol=0.8, oi=0.1, ret4=0.1, rs=-0.5, sector_lead=-0.2),
    ]
    background._apply_v8_dual_alpha(rows)
    by = {r["symbol"]: r for r in rows}
    assert by["BULL"]["price_chg_60m_pct"] == 1.2
    assert by["BEAR"]["price_chg_60m_pct"] == -1.1
    assert by["BULL"]["v8_direction"] == "Bullish"
    assert by["BEAR"]["v8_direction"] == "Bearish"
    assert by["BULL"]["v8_alpha"] > by["DULL"]["v8_alpha"]
    assert by["BEAR"]["v8_oi_state"] == "Fresh Short Buildup"


def test_compute_signal_exposes_60_minute_return_for_v8_derivatives():
    idx = pd.date_range("2026-08-20 09:15", periods=120, freq="15min")
    close = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.linspace(1000, 2000, len(idx)),
    }, index=idx)
    result = indicators.compute_signal(df, "15minute")
    assert "ret_4" in result
    expected = round((close.iloc[-1] / close.iloc[-5] - 1.0) * 100.0, 2)
    assert result["ret_4"] == expected


def test_live_v8_attaches_separate_swing_state_after_1415():
    rows = [
        _row("BULL", "Bullish", close=109, high=110, low=100, rvol=3.0, oi=4.0, ret4=1.5, rs=2.5, sector_lead=2.0),
        _row("MID", "Bullish", close=105, high=110, low=100, rvol=1.0, oi=0.2, ret4=0.1, rs=0.0, sector_lead=0.0),
        _row("LOW", "Bearish", close=104, high=110, low=100, rvol=0.8, oi=0.1, ret4=-0.1, rs=0.1, sector_lead=0.1),
    ]
    rows[0]["breakout_retained"] = True
    rows[0]["close_position_pct"] = 95
    background._apply_v8_dual_alpha(rows, now="14:30")
    assert "v8_swing_alpha" in rows[0]
    assert rows[0]["v8_swing_state"] in ("TRADE CANDIDATE", "WATCH")
    assert rows[0]["v8_swing_late_session"] is True
