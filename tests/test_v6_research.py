import numpy as np
import pandas as pd
import pytest

from app import early_research, v6_edge


def make_intraday(days=65, bars_per_day=10):
    rows = []
    idx = []
    base = 100.0
    bdays = pd.bdate_range("2026-04-01", periods=days)
    for d_i, day in enumerate(bdays):
        start = pd.Timestamp(day.date()) + pd.Timedelta(hours=9, minutes=15)
        for b in range(bars_per_day):
            t = start + pd.Timedelta(minutes=15*b)
            drift = d_i * 0.08 + b * 0.02
            close = base + drift
            rows.append((close-0.05, close+0.15, close-0.15, close, 1000 + d_i*8 + b*30))
            idx.append(t)
    return pd.DataFrame(rows, columns=["open","high","low","close","volume"], index=pd.DatetimeIndex(idx))


def test_build_feature_frame_adds_v6_location_catalyst_regime_and_basis():
    df = make_intraday()
    index_df = df.copy()
    index_df["close"] = 200 + np.arange(len(df)) * 0.01
    sector_df = df.copy()
    sector_df["close"] = 300 + np.arange(len(df)) * 0.015
    sector_rank = pd.Series(85.0, index=df.index)

    fut = df.iloc[-120:].copy()
    # Expanding premium so basis acceleration is measurable on covered bars.
    fut["close"] = df.loc[fut.index, "close"] * (1.001 + np.linspace(0, 0.001, len(fut)))

    feat = early_research.build_feature_frame(
        df, "15minute", index_df=index_df, sector_df=sector_df,
        sector_rank_series=sector_rank, futures_df=fut,
    )
    assert "price_location_score" in feat
    assert "catalyst_score" in feat
    assert "market_regime" in feat
    assert "sector_rank_percentile" in feat
    assert "basis_pct" in feat
    assert "basis_acceleration" in feat
    assert feat["price_location_score"].notna().sum() > 0
    assert feat["sector_rank_percentile"].dropna().iloc[-1] == pytest.approx(85.0)
    assert feat["basis_pct"].iloc[-1] > 0
    assert feat["basis_pct"].iloc[0] != feat["basis_pct"].iloc[0]  # partial coverage is honest NaN


def test_price_location_uses_prior_completed_sessions_not_current_high():
    df = make_intraday(days=25)
    feat = early_research.build_feature_frame(df, "15minute")
    # Prior 20-session high should be constant through a session and should not
    # jump merely because the current bar prints a new high.
    last_day = df.index.normalize()[-1]
    mask = df.index.normalize() == last_day
    vals = feat.loc[mask, "prior_high_20d"].dropna().unique()
    assert len(vals) <= 1


def test_v6_three_way_report_keeps_final_locked():
    events = []
    for i in range(30):
        events.append({
            "entry_time": f"2026-05-{(i%28)+1:02d}T{9 + (i//28):02d}:30:00",
            "swing_returns": {"1D": 0.2 if i < 24 else -0.5},
            "direction": "Bullish",
            "breakout_source": "Recent Range",
        })
    report = early_research.v6_edge_report(events)
    assert report["recent_range_long"]["development"]["trade_count"] > 0
    assert report["recent_range_long"]["validation"]["trade_count"] > 0
    assert report["recent_range_long"]["final_test"]["locked"] is True


def test_v6_path_exit_summary_reports_target_stop_grid():
    df = make_intraday(days=2, bars_per_day=10)
    # force a simple bullish path after entry
    df.iloc[2:8, df.columns.get_loc("high")] += np.linspace(0.2, 1.5, 6)
    event = {
        "entry_time": df.index[1].isoformat(),
        "signal_time": df.index[0].isoformat(),
        "direction": "Bullish",
        "entry_pos": 1,
        "entry_price": float(df["open"].iloc[1]),
        "atr_value": 1.0,
    }
    early_research.attach_v6_path_exits(df, event, max_bars=8)
    assert "path_exits" in event
    assert "T1.00_S0.50" in event["path_exits"]
    assert "breakeven_0.50" in event["path_exits"]


def test_near_futures_history_helper_uses_current_near_contract(monkeypatch):
    from app import backtest
    monkeypatch.setattr(backtest.scanner_mod, '_load_fut_contracts_map', lambda kite: {
        'ABC': [{'instrument_token': 999, 'tradingsymbol': 'ABCSEP26FUT'}]
    })
    seen = {}
    def fake_history(token, timeframe, days, kite):
        seen.update(token=token, timeframe=timeframe, days=days)
        return make_intraday(days=3)
    monkeypatch.setattr(backtest, '_fetch_history', fake_history)
    out = backtest._fetch_near_futures_history_for_research(object(), 'ABC', '15minute', 30)
    assert out is not None and not out.empty
    assert seen['token'] == 999
    assert seen['timeframe'] == '15minute'
    assert seen['days'] > 30
