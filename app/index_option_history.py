"""Historical data ingestion and Stage-1 execution for index-option timing research.

This module is intentionally research-only. It accepts an already-authenticated Kite
client, fetches NIFTY 50 one-minute bars through the repository's existing safe chunker,
runs the preregistered timing grid, and writes reproducible CSV artifacts.

It does not authenticate users, place orders, alter recorder state, or call production
routes. The caller controls where artifacts are written.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from . import scanner
from .index_option_research import evaluate_timing_grid, summarize_timing_grid


def _as_naive_ist(value) -> dt.datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts.to_pydatetime()


def fetch_nifty_1m_history(kite, start, end) -> pd.DataFrame:
    """Fetch completed NIFTY 50 one-minute candles for [start, end].

    Uses scanner._load_index_token and scanner._fetch_historical_chunked so rate-limit
    chunking and retry behavior stay consistent with DBIndicator's existing research code.
    """
    start_dt = _as_naive_ist(start)
    end_dt = _as_naive_ist(end)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")

    token = scanner._load_index_token(kite, "NIFTY 50")
    if not token:
        raise RuntimeError("NIFTY 50 instrument token could not be resolved")

    rows = scanner._fetch_historical_chunked(
        kite,
        token,
        start_dt,
        end_dt,
        "minute",
        oi=False,
        continuous=False,
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    if "date" not in frame.columns:
        raise ValueError("Kite history response is missing date")

    frame = frame.rename(columns={"date": "timestamp"}).set_index("timestamp")
    frame.index = pd.to_datetime(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    required = ["open", "high", "low", "close"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"Kite history response missing OHLC columns: {missing}")

    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in frame.columns]
    frame = frame[keep].copy()

    # Historical API requests can include the current forming minute. A row whose
    # full minute has not elapsed by the requested end timestamp is not admissible.
    complete_before = pd.Timestamp(end_dt)
    index_for_compare = frame.index
    if index_for_compare.tz is not None:
        complete_before = complete_before.tz_localize(index_for_compare.tz)
    completed = (index_for_compare + pd.Timedelta(minutes=1)) <= complete_before
    return frame.loc[completed].copy()


def run_stage1_from_bars(
    bars_1m: pd.DataFrame,
    *,
    output_dir,
    range_pct_bounds=None,
) -> dict:
    """Run the frozen Stage-1 grid and write transparent, machine-readable artifacts."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    trades = evaluate_timing_grid(
        bars_1m,
        range_pct_bounds=range_pct_bounds,
    )
    summary_60m = summarize_timing_grid(trades, horizon_minutes=60)
    summary_120m = summarize_timing_grid(trades, horizon_minutes=120)

    bars_path = output / "nifty_1m.csv"
    trades_path = output / "stage1_trades.csv"
    summary_60_path = output / "stage1_summary_60m.csv"
    summary_120_path = output / "stage1_summary_120m.csv"
    manifest_path = output / "stage1_manifest.json"

    bars_1m.to_csv(bars_path, index_label="timestamp")
    trades.to_csv(trades_path, index=False)
    summary_60m.to_csv(summary_60_path, index=False)
    summary_120m.to_csv(summary_120_path, index=False)

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 1",
        "instrument": "NIFTY 50",
        "source_bars": int(len(bars_1m)),
        "source_first_timestamp": (
            pd.Timestamp(bars_1m.index[0]).isoformat() if len(bars_1m) else None
        ),
        "source_last_timestamp": (
            pd.Timestamp(bars_1m.index[-1]).isoformat() if len(bars_1m) else None
        ),
        "signal_rows": int(len(trades)),
        "range_pct_bounds": list(range_pct_bounds) if range_pct_bounds is not None else None,
        "artifacts": {
            "bars": bars_path.name,
            "trades": trades_path.name,
            "summary_60m": summary_60_path.name,
            "summary_120m": summary_120_path.name,
        },
        "production_deployed": False,
        "v12_recorder_used_for_tuning": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "manifest": manifest,
        "bars_path": bars_path,
        "trades_path": trades_path,
        "summary_60m_path": summary_60_path,
        "summary_120m_path": summary_120_path,
        "manifest_path": manifest_path,
    }


def fetch_and_run_stage1(
    kite,
    *,
    start,
    end,
    output_dir,
    range_pct_bounds=None,
) -> dict:
    bars = fetch_nifty_1m_history(kite, start, end)
    if bars.empty:
        raise RuntimeError("No NIFTY 50 one-minute candles were returned")
    return run_stage1_from_bars(
        bars,
        output_dir=output_dir,
        range_pct_bounds=range_pct_bounds,
    )
