import sys
import types

import pandas as pd

if "kiteconnect" not in sys.modules:
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = type("KiteConnect", (), {})
    mod.KiteTicker = type("KiteTicker", (), {})
    sys.modules["kiteconnect"] = mod

from app import backtest


def _frame(idx, *, rvol, opening, rng, gap, turnover, oi, rs, sector):
    return pd.DataFrame({
        "tod_rvol": rvol,
        "opening_rvol": opening,
        "bar_range_atr": rng,
        "gap_atr": gap,
        "turnover_notional": turnover,
        "oi_chg_60m_pct": oi,
        "rs_pct": rs,
        "stock_sector_lead_pct": sector,
    }, index=idx)


def test_attach_v8_full_universe_scores_uses_all_symbols_not_only_breakouts():
    idx = pd.to_datetime(["2026-08-28 10:30", "2026-08-28 10:45"])
    frames = {
        "BULL": _frame(idx, rvol=[3.0, 3.1], opening=[2.0, 2.0], rng=[1.5, 1.6], gap=[0.8, 0.8], turnover=[1000, 1100], oi=[4, 4], rs=[2, 2], sector=[1.5, 1.5]),
        "BEAR": _frame(idx, rvol=[2.8, 2.9], opening=[1.9, 1.9], rng=[1.4, 1.5], gap=[-0.7, -0.7], turnover=[900, 950], oi=[3.5, 3.5], rs=[-2, -2], sector=[-1.4, -1.4]),
        "MID": _frame(idx, rvol=[1.2, 1.1], opening=[1.0, 1.0], rng=[0.6, 0.6], gap=[0.1, 0.1], turnover=[400, 420], oi=[0.4, 0.4], rs=[0.2, 0.2], sector=[0.1, 0.1]),
        "LOW": _frame(idx, rvol=[0.7, 0.8], opening=[0.8, 0.8], rng=[0.4, 0.5], gap=[0.05, 0.05], turnover=[100, 120], oi=[0.1, 0.1], rs=[-0.1, -0.1], sector=[-0.1, -0.1]),
    }
    replays = [
        {"ignition_events": [{
            "symbol": "BULL", "signal_time": idx[0].isoformat(), "entry_time": idx[1].isoformat(),
            "direction": "Bullish", "breakout_source": "Recent Range", "breakout_extension_atr": 0.4,
            "high": 110, "low": 100, "close": 109.5, "price_chg_60m_pct": 1.2,
            "oi_chg_60m_pct": 4, "basis_acceleration": 0.05,
        }]},
        {"ignition_events": [{
            "symbol": "BEAR", "signal_time": idx[0].isoformat(), "entry_time": idx[1].isoformat(),
            "direction": "Bearish", "breakout_source": "Recent Range", "breakout_extension_atr": 0.45,
            "high": 110, "low": 100, "close": 100.3, "price_chg_60m_pct": -1.3,
            "oi_chg_60m_pct": 3.5, "basis_acceleration": -0.05,
        }]},
    ]
    backtest._attach_v8_full_universe_scores(replays, frames)
    bull = replays[0]["ignition_events"][0]
    bear = replays[1]["ignition_events"][0]
    assert bull["v8_participation"] >= 75
    assert bear["v8_participation"] >= 70
    assert bull["v8_relative"] >= 75
    assert bear["v8_relative"] >= 75
    assert bull["v8_oi_state"] == "Long Buildup"
    assert bear["v8_oi_state"] == "Fresh Short Buildup"
    assert bull["v8_alpha"] is not None
    assert bear["v8_alpha"] is not None
