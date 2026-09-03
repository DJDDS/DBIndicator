import math

import numpy as np
import pandas as pd
import pytest

from app import nse_futures_history as nf
from app import v95_daily_evidence as v95


class _FakeFOClient:
    def fetch_day(self, day):
        d = pd.Timestamp(day).normalize()
        return pd.DataFrame([
            {"date": d, "symbol": "AAA", "expiry": d + pd.Timedelta(days=10), "open_interest": 1000.0,
             "oi_share_equivalent": 1000.0, "volume": 10.0, "lot_size": 50.0, "source_format": "TEST"},
            {"date": d, "symbol": "AAA", "expiry": d + pd.Timedelta(days=40), "open_interest": 2000.0,
             "oi_share_equivalent": 2000.0, "volume": 4.0, "lot_size": 50.0, "source_format": "TEST"},
        ])


def test_v980_futures_history_aggregates_share_equivalent_volume():
    days = pd.bdate_range("2020-01-02", periods=2)
    out = nf.build_symbol_histories(days, ["AAA"], _FakeFOClient())
    hist = out["AAA"]
    assert "total_volume" in hist and "near_volume" in hist
    # 10*50 + 4*50 share-equivalent units; near expiry is first row.
    assert hist["total_volume"].iloc[0] == pytest.approx(700.0)
    assert hist["near_volume"].iloc[0] == pytest.approx(500.0)


def _price(periods=40):
    idx = pd.bdate_range("2020-01-01", periods=periods)
    close = pd.Series(100.0 * np.exp(np.linspace(0, 0.12, periods)), index=idx)
    open_ = close.shift(1).fillna(close.iloc[0] * 0.995) * 1.002
    high = pd.concat([open_, close], axis=1).max(axis=1) * 1.01
    low = pd.concat([open_, close], axis=1).min(axis=1) * 0.99
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def _gk_one(row):
    hl = math.log(row.high / row.low)
    co = math.log(row.close / row.open)
    return max(0.0, 0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co)


def test_v980_daily_frame_adds_next_session_variance_har_and_volume_without_lookahead():
    price = _price()
    oi = pd.Series(np.linspace(1_000_000, 1_500_000, len(price)), index=price.index)
    volume = pd.Series(np.linspace(10_000, 20_000, len(price)), index=price.index)
    frame = v95.build_symbol_daily_frame(price, oi, futures_volume_series=volume)
    required = {"next_yz_var", "next_gk_var", "har_daily_var", "har_weekly_var", "har_monthly_var", "futures_volume_z"}
    assert required.issubset(frame.columns)

    d = price.index[30]
    nxt = price.loc[price.index[31]]
    assert frame.loc[d, "next_gk_var"] == pytest.approx(_gk_one(nxt))

    logret = np.log(price["close"] / price["close"].shift(1))
    daily_var = logret.pow(2)
    assert frame.loc[d, "har_daily_var"] == pytest.approx(daily_var.loc[d])
    assert frame.loc[d, "har_weekly_var"] == pytest.approx(daily_var.rolling(5, min_periods=5).mean().loc[d])
    assert frame.loc[d, "har_monthly_var"] == pytest.approx(daily_var.rolling(22, min_periods=15).mean().loc[d])

    # Changing future prices/volume cannot alter event-date covariates.
    changed_price = price.copy(); changed_volume = volume.copy()
    changed_price.loc[price.index[31]:, "high"] *= 1.5
    changed_price.loc[price.index[31]:, "low"] *= 0.7
    changed_volume.loc[price.index[31]:] *= 100.0
    frame2 = v95.build_symbol_daily_frame(changed_price, oi, futures_volume_series=changed_volume)
    for col in ("har_daily_var", "har_weekly_var", "har_monthly_var", "futures_volume_z"):
        assert frame2.loc[d, col] == pytest.approx(frame.loc[d, col])
    # Outcome is allowed to change because it is explicitly next-session.
    assert frame2.loc[d, "next_gk_var"] != pytest.approx(frame.loc[d, "next_gk_var"])


def test_v980_volume_z_is_current_volume_against_prior_history_only():
    price = _price(80)
    oi = pd.Series(np.linspace(1_000_000, 1_500_000, len(price)), index=price.index)
    volume = pd.Series(1000.0 + np.arange(len(price)) * 7.0, index=price.index)
    volume.iloc[70] = 5000.0
    frame = v95.build_symbol_daily_frame(price, oi, futures_volume_series=volume)
    assert frame["futures_volume_z"].iloc[70] > 3.0
    # The spike must not contaminate the same day's rolling mean/std denominator.
    prior = np.log1p(volume).rolling(60, min_periods=20).mean().shift(1)
    sd = np.log1p(volume).rolling(60, min_periods=20).std(ddof=1).shift(1)
    expected = (np.log1p(volume.iloc[70]) - prior.iloc[70]) / sd.iloc[70]
    assert frame["futures_volume_z"].iloc[70] == pytest.approx(expected)
