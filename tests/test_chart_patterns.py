"""Synthetic-shape tests for the timeframe-matched chart pattern engine."""
import numpy as np
import pandas as pd
import pytest

from app import chart_patterns as cp


def _series(anchors, noise=0.15, seed=1, vol=None):
    """Piecewise-linear close path through (bar, price) anchors, with OHLC wiggle."""
    rng = np.random.default_rng(seed)
    xs, ys = zip(*anchors)
    idx = np.arange(xs[-1] + 1)
    close = np.interp(idx, xs, ys) + rng.normal(0, noise, len(idx))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 0.4
    low = np.minimum(open_, close) - 0.4
    volume = np.full(len(idx), 1_000_000.0) if vol is None else vol(idx)
    ts = pd.date_range("2024-01-01", periods=len(idx), freq="B", tz="Asia/Kolkata")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=ts)


def _mirror_df(df):
    m = df.copy()
    m["open"], m["close"] = 400 - df["open"], 400 - df["close"]
    m["high"], m["low"] = 400 - df["low"], 400 - df["high"]
    return m


def _hits(df, tf="day"):
    fr = cp.make_frame(df)
    return cp.detect_all("TEST", {tf: fr}, tfs=[tf])


def _find(hits, name):
    return [h for h in hits if h["pattern"] == name]


INV_HS = [(0, 130), (30, 110), (45, 100), (60, 110), (80, 92), (100, 110),
          (115, 101), (131, 109), (134, 112.5)]


def test_inverse_head_and_shoulders_breakout():
    hits = _hits(_series(INV_HS))
    got = _find(hits, "Inverse Head & Shoulders")
    assert got, [h["pattern"] for h in hits]
    h = got[0]
    assert h["direction"] == "BULL"
    assert h["status"] in ("BREAKOUT", "EXTENDED")
    assert h["stop"] < h["trigger"] < h["target"]
    assert 0 <= h["score"] <= 100


def test_head_and_shoulders_is_exact_mirror():
    hits = _hits(_mirror_df(_series(INV_HS)))
    got = _find(hits, "Head & Shoulders")
    assert got, [h["pattern"] for h in hits]
    h = got[0]
    assert h["direction"] == "BEAR"
    assert h["target"] < h["trigger"] < h["stop"]
    # candles are real prices (high >= low) after un-mirroring
    assert all(c["high"] >= c["low"] for c in h["candles"])


def test_double_bottom_forming():
    anchors = [(0, 130), (30, 100), (50, 112), (75, 100.3), (98, 111.9)]
    hits = _hits(_series(anchors, noise=0.1))
    got = _find(hits, "Double Bottom")
    assert got, [h["pattern"] for h in hits]
    assert got[0]["status"] in ("FORMING", "BREAKOUT")


def test_ascending_triangle():
    anchors = [(0, 80), (20, 100), (30, 90), (40, 100), (50, 93), (60, 100), (70, 96), (80, 100), (84, 99.7)]
    hits = _hits(_series(anchors, noise=0.05))
    names = [h["pattern"] for h in hits]
    assert "Ascending Triangle" in names, names


def test_bull_flag_breakout_with_volume():
    anchors = [(0, 100), (40, 101), (50, 125), (60, 120), (63, 121), (66, 127)]
    vol = lambda i: np.where((i >= 40) & (i <= 50), 3e6, np.where(i >= 65, 4e6, 1e6))
    hits = _hits(_series(anchors, noise=0.05, vol=vol))
    flags = [h for h in hits if h["family"] == "flag"]
    assert flags, [h["pattern"] for h in hits]
    assert flags[0]["direction"] == "BULL"


def test_cup_and_handle_daily():
    xs = np.arange(0, 121)
    cup = 150 - 35 * np.sin(np.pi * xs / 120)       # rounded U from 150 down to 115 and back
    anchors = [(-40 + i, 120 + i * 0.75) for i in range(0, 40, 10)] + [(40 + x, y) for x, y in zip(xs[::6], cup[::6])]
    anchors = [(x + 40, y) for x, y in anchors]
    last = anchors[-1][0]
    anchors += [(last + 6, 143), (last + 12, 146), (last + 14, 152)]
    anchors = sorted({a[0]: a for a in anchors if a[0] >= 0}.values())
    hits = _hits(_series(anchors, noise=0.1))
    got = [h for h in hits if h["family"] == "cup_handle"]
    assert got, [h["pattern"] for h in hits]


def test_pattern_only_on_matrix_timeframes():
    hits = _hits(_series(INV_HS), tf="60minute")
    assert not [h for h in hits if h["family"] in ("head_shoulders", "cup_handle", "double")]


def test_random_walk_does_not_crash_and_is_bounded():
    rng = np.random.default_rng(7)
    for seed in range(15):
        steps = rng.normal(0, 1, 400).cumsum() + 200
        anchors = list(enumerate(steps))
        df = _series(anchors, noise=0.3, seed=seed)
        for tf in cp.TIMEFRAMES:
            for h in _hits(df, tf):
                assert 0 <= h["score"] <= 100
                assert h["grade"] in "ABC"
                assert h["rr"] > 0


def test_public_rows_strips_chart_payload():
    rows = cp.public_rows({"results": [{"symbol": "X", "candles": [1], "segments": [], "points": []}]})
    assert rows == [{"symbol": "X"}]


def test_matrix_for_ui_covers_every_family():
    fams = {m["family"] for m in cp.matrix_for_ui()}
    assert fams == set(cp.PATTERN_MATRIX)
