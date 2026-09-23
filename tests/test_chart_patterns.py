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


def test_large_slow_structures_are_not_in_fast_swing_production():
    assert "head_shoulders" not in cp.PATTERN_MATRIX
    assert "cup_handle" not in cp.PATTERN_MATRIX
    assert "triple" not in cp.PATTERN_MATRIX
    assert "wedge" not in cp.PATTERN_MATRIX


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


def test_public_rows_strips_chart_payload_and_adds_nse_evidence():
    rows = cp.public_rows(
        {"results": [{"symbol": "X", "pattern": "Bull Flag", "direction": "BULL",
                      "candles": [1], "segments": [], "points": []}]},
        ledger={},
    )
    assert len(rows) == 1
    assert "candles" not in rows[0] and "segments" not in rows[0] and "points" not in rows[0]
    assert rows[0]["nse_evidence"]["stage"] == "BUILDING"
    assert rows[0]["nse_evidence"]["resolved"] == 0


def test_matrix_for_ui_is_the_crisp_fast_swing_universe():
    rows = cp.matrix_for_ui()
    fams = {m["family"] for m in rows}
    assert fams == {"vcp", "rectangle", "flag", "triangle", "double", "three_valleys"}
    assert cp.TIMEFRAMES == ["day"]
    assert all(m["research_basis"] in {"DIRECT", "MECHANISM", "LIMITED"} for m in rows)
    assert all("evidence_bull" not in m and "evidence_bear" not in m for m in rows)


# --- research-basis + NSE D1-D5 evidence, 3RV, VCP, FAILED, forward ledger ---

def test_three_rising_valleys_and_mirror():
    anchors = [(0, 140), (20, 100), (32, 115), (45, 104), (58, 116), (72, 108), (86, 115.6)]
    hits = _hits(_series(anchors, noise=0.05))
    got = [h for h in hits if h["family"] == "three_valleys"]
    assert got and got[0]["pattern"] == "Three Rising Valleys", [h["pattern"] for h in hits]
    hits_b = _hits(_mirror_df(_series(anchors, noise=0.05)))
    got_b = [h for h in hits_b if h["family"] == "three_valleys"]
    assert got_b and got_b[0]["pattern"] == "Three Falling Peaks"


def test_vcp_tight_base_is_symmetric_for_bull_and_bear():
    anchors = [(0, 70), (40, 100), (50, 88), (60, 100.5), (68, 94), (76, 100.8), (81, 97.6), (86, 100.3)]
    hits = _hits(_series(anchors, noise=0.05))
    got = [h for h in hits if h["family"] == "vcp"]
    assert got and got[0]["direction"] == "BULL", [h["pattern"] for h in hits]

    hits_b = _hits(_mirror_df(_series(anchors, noise=0.05)))
    got_b = [h for h in hits_b if h["family"] == "vcp"]
    assert got_b and got_b[0]["direction"] == "BEAR", [h["pattern"] for h in hits_b]


def test_failed_breakout_is_recorded_not_dropped():
    anchors = [(0, 80), (20, 100), (30, 90), (40, 100), (50, 93), (60, 100), (70, 96), (80, 100),
               (83, 103), (86, 98)]
    hits = _hits(_series(anchors, noise=0.05))
    failed = [h for h in hits if h["status"] == "FAILED"]
    assert failed, [(h["pattern"], h["status"]) for h in hits]
    assert all(h["grade"] == "C" for h in failed)


def test_every_hit_has_research_basis_and_explicit_daily_formation_window():
    anchors = [(0, 80), (20, 100), (30, 90), (40, 100), (50, 93), (60, 100), (70, 96), (80, 100), (84, 103)]
    for h in _hits(_series(anchors, noise=0.05)):
        assert h["research_basis"] in {"DIRECT", "MECHANISM", "LIMITED"}
        assert "evidence" not in h
        assert h["horizon"] == "1-5 trading days"
        assert h["pattern_timeframe"] == "Daily"
        assert h["formation_sessions"] == h["bars"] + 1
        assert h["formation_start_time"] <= h["bar_time"]
        if h["status"] == "FORMING":
            assert h["grade"] != "A"


def test_forward_ledger_records_and_scores_breakout(tmp_path):
    anchors = [(0, 100), (40, 101), (50, 125), (60, 120), (63, 121), (66, 127)]
    df = _series(anchors, noise=0.05)
    fr = cp.make_frame(df)
    hits = cp.detect_all("TEST", {"day": fr}, tfs=["day"])
    brk = [h for h in hits if h["status"] in ("BREAKOUT", "RETEST_HOLD", "EXTENDED")]
    assert brk
    ledger = {}
    assert cp.record_breakouts(ledger, brk) == len(brk)
    assert cp.record_breakouts(ledger, brk) == 0            # recorded once only
    # extend the series: strong follow-through after the breakout
    more = _series(anchors + [(72, 132), (78, 136)])
    frames = {"day": cp.make_frame(more)}
    cp.update_forward(ledger, "TEST", frames)
    ev = next(iter(ledger.values()))
    assert ev["outcome"] in ("SUCCESS", "FAIL", "OPEN", "TIMEOUT")
    assert ev["fast_outcome"] in ("SUCCESS", "FAIL", "OPEN", "TIMEOUT")
    assert ev["bars_seen"] > 0
    assert ev["entry"] > 0 and ev["atr"] > 0
    path = tmp_path / "fwd.json"
    cp.save_forward(ledger, str(path))
    summ = cp.forward_summary(cp.load_forward(str(path)))
    assert summ["events"] == len(ledger) and summ["rows"]
    row = summ["rows"][0]
    assert all(("median_mfe_d" + str(d)) in row for d in range(1, 6))
    assert all(r["direction"] in ("BULL", "BEAR") for r in summ["rows"])


def test_lower_timeframes_do_not_emit_production_patterns():
    anchors = [(0, 100), (40, 101), (50, 125), (60, 120), (63, 121), (66, 127)]
    fr = cp.make_frame(_series(anchors, noise=0.05))
    assert cp.detect_all("X", {"60minute": fr}, tfs=["60minute"]) == []
    assert cp.detect_all("X", {"4hour": fr}, tfs=["4hour"]) == []
