"""Historical data ingestion and Stage-1 execution for index-option timing research.

This module is intentionally research-only. It accepts an already-authenticated Kite
client, fetches NIFTY 50 one-minute bars with strict research safeguards, runs the
preregistered timing grid, and writes reproducible CSV artifacts.

It does not authenticate users, place orders, alter recorder state, or call production
routes. The caller controls where artifacts are written.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

from . import scanner
from .index_option_research import evaluate_timing_grid, summarize_timing_grid


LOCKED_SPLITS = {
    "development": ("2019-01-01", "2023-12-31"),
    "validation": ("2024-01-01", "2025-12-31"),
    "historical_holdout": ("2026-01-01", "2026-08-31"),
}

_RESEARCH_CHUNK_DAYS = 20
_RESEARCH_PAUSE_SECONDS = 0.45
_RESEARCH_MAX_ATTEMPTS = 3
_FULL_SESSION_MINUTES = 375


def _as_naive_ist(value) -> dt.datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts.to_pydatetime()


def _fetch_research_minute_rows(kite, token, start_dt, end_dt):
    """Strict, throttled one-minute fetch.

    Unlike the live scanner's tolerant history fetcher, research must not silently skip
    a failed chunk because a missing historical window can create a false timing edge.
    Each 20-day chunk gets bounded retries and the research run fails closed if a chunk
    continues to error.
    """
    rows = []
    chunks = []
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + dt.timedelta(days=_RESEARCH_CHUNK_DAYS), end_dt)
        chunk_rows = None
        last_error = None
        for attempt in range(1, _RESEARCH_MAX_ATTEMPTS + 1):
            try:
                chunk_rows = kite.historical_data(
                    token,
                    chunk_start,
                    chunk_end,
                    "minute",
                    continuous=False,
                    oi=False,
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - surfaced after bounded retries
                last_error = exc
                if attempt < _RESEARCH_MAX_ATTEMPTS:
                    time.sleep(max(1.0, _RESEARCH_PAUSE_SECONDS))
        if last_error is not None:
            raise RuntimeError(
                f"NIFTY one-minute research chunk failed after {_RESEARCH_MAX_ATTEMPTS} "
                f"attempts: {chunk_start} -> {chunk_end}: {last_error}"
            ) from last_error

        chunk_rows = list(chunk_rows or [])
        rows.extend(chunk_rows)
        chunks.append({
            "start": chunk_start.isoformat(),
            "end": chunk_end.isoformat(),
            "rows": int(len(chunk_rows)),
        })
        chunk_start = chunk_end
        if chunk_start < end_dt:
            time.sleep(_RESEARCH_PAUSE_SECONDS)
    return rows, chunks


def fetch_nifty_1m_history(kite, start, end, *, return_fetch_report=False):
    """Fetch completed NIFTY 50 one-minute candles for [start, end].

    The research fetch is deliberately stricter than live scanning: it throttles every
    request, retries transient failures and refuses to silently skip an errored chunk.
    """
    start_dt = _as_naive_ist(start)
    end_dt = _as_naive_ist(end)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")

    token = scanner._load_index_token(kite, "NIFTY 50")
    if not token:
        raise RuntimeError("NIFTY 50 instrument token could not be resolved")

    rows, chunks = _fetch_research_minute_rows(kite, token, start_dt, end_dt)
    frame = pd.DataFrame(rows)
    if frame.empty:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        report = {"chunks": chunks, "row_count": 0, "first_timestamp": None, "last_timestamp": None}
        return (empty, report) if return_fetch_report else empty

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

    complete_before = pd.Timestamp(end_dt)
    index_for_compare = frame.index
    if index_for_compare.tz is not None:
        complete_before = complete_before.tz_localize(index_for_compare.tz)
    completed = (index_for_compare + pd.Timedelta(minutes=1)) <= complete_before
    frame = frame.loc[completed].copy()

    report = {
        "chunks": chunks,
        "row_count": int(len(frame)),
        "first_timestamp": pd.Timestamp(frame.index[0]).isoformat() if len(frame) else None,
        "last_timestamp": pd.Timestamp(frame.index[-1]).isoformat() if len(frame) else None,
    }
    return (frame, report) if return_fetch_report else frame


def _complete_regular_sessions(bars_1m: pd.DataFrame):
    """Keep only complete regular NSE sessions (09:15 through 15:29 inclusive)."""
    if bars_1m is None or bars_1m.empty:
        return bars_1m.copy(), {
            "candidate_sessions": 0,
            "complete_sessions": 0,
            "dropped_incomplete_sessions": 0,
            "dropped_dates": [],
        }

    idx = pd.DatetimeIndex(bars_1m.index)
    kept = []
    dropped = []
    candidate_dates = []
    for normalized, group in bars_1m.groupby(idx.normalize(), sort=True):
        candidate_dates.append(normalized)
        day_start = pd.Timestamp(normalized) + pd.Timedelta(hours=9, minutes=15)
        if idx.tz is not None and day_start.tzinfo is None:
            day_start = day_start.tz_localize(idx.tz)
        expected = pd.date_range(day_start, periods=_FULL_SESSION_MINUTES, freq="min")
        regular = group[(group.index >= day_start) & (group.index < day_start + pd.Timedelta(minutes=_FULL_SESSION_MINUTES))]
        if len(regular) == _FULL_SESSION_MINUTES and pd.DatetimeIndex(regular.index).equals(expected):
            kept.append(regular)
        else:
            dropped.append(pd.Timestamp(normalized).date().isoformat())

    filtered = pd.concat(kept).sort_index() if kept else bars_1m.iloc[0:0].copy()
    report = {
        "candidate_sessions": int(len(candidate_dates)),
        "complete_sessions": int(len(kept)),
        "dropped_incomplete_sessions": int(len(dropped)),
        "dropped_dates": dropped,
    }
    return filtered, report


def _split_trades(trades: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(columns=[] if trades is None else trades.columns)
    session_date = pd.to_datetime(trades["session"]).dt.date
    lo = pd.Timestamp(start).date()
    hi = pd.Timestamp(end).date()
    return trades[(session_date >= lo) & (session_date <= hi)].copy()


def _write_split_summaries(trades: pd.DataFrame, output: Path) -> tuple[dict, dict]:
    artifacts = {}
    coverage = {}
    for split_name, (start, end) in LOCKED_SPLITS.items():
        split = _split_trades(trades, start, end)
        coverage[split_name] = {
            "start": start,
            "end": end,
            "signal_rows": int(len(split)),
            "sessions": int(split["session"].nunique()) if not split.empty else 0,
        }
        for horizon in (60, 120):
            summary = summarize_timing_grid(split, horizon_minutes=horizon)
            path = output / f"stage1_{split_name}_summary_{horizon}m.csv"
            summary.to_csv(path, index=False)
            artifacts[f"{split_name}_summary_{horizon}m"] = path.name
    return artifacts, coverage


def run_stage1_from_bars(
    bars_1m: pd.DataFrame,
    *,
    output_dir,
    range_pct_bounds=None,
    fetch_report=None,
) -> dict:
    """Run the frozen Stage-1 grid and write transparent, machine-readable artifacts."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    complete_bars, session_quality = _complete_regular_sessions(bars_1m)
    if complete_bars.empty:
        raise RuntimeError("No complete regular NIFTY sessions available for Stage-1 research")

    trades = evaluate_timing_grid(
        complete_bars,
        range_pct_bounds=range_pct_bounds,
    )
    summary_60m = summarize_timing_grid(trades, horizon_minutes=60)
    summary_120m = summarize_timing_grid(trades, horizon_minutes=120)

    bars_path = output / "nifty_1m.csv"
    trades_path = output / "stage1_trades.csv"
    summary_60_path = output / "stage1_summary_60m.csv"
    summary_120_path = output / "stage1_summary_120m.csv"
    manifest_path = output / "stage1_manifest.json"

    complete_bars.to_csv(bars_path, index_label="timestamp")
    trades.to_csv(trades_path, index=False)
    summary_60m.to_csv(summary_60_path, index=False)
    summary_120m.to_csv(summary_120_path, index=False)
    split_artifacts, split_coverage = _write_split_summaries(trades, output)

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 1",
        "instrument": "NIFTY 50",
        "source_bars_raw": int(len(bars_1m)),
        "source_bars_complete_sessions": int(len(complete_bars)),
        "source_first_timestamp": (
            pd.Timestamp(complete_bars.index[0]).isoformat() if len(complete_bars) else None
        ),
        "source_last_timestamp": (
            pd.Timestamp(complete_bars.index[-1]).isoformat() if len(complete_bars) else None
        ),
        "session_quality": session_quality,
        "fetch_report": fetch_report,
        "signal_rows": int(len(trades)),
        "range_pct_bounds": list(range_pct_bounds) if range_pct_bounds is not None else None,
        "locked_splits": {
            name: {"start": start, "end": end}
            for name, (start, end) in LOCKED_SPLITS.items()
        },
        "split_coverage": split_coverage,
        "artifacts": {
            "bars": bars_path.name,
            "trades": trades_path.name,
            "summary_60m": summary_60_path.name,
            "summary_120m": summary_120_path.name,
            **split_artifacts,
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
    bars, fetch_report = fetch_nifty_1m_history(kite, start, end, return_fetch_report=True)
    if bars.empty:
        raise RuntimeError("No NIFTY 50 one-minute candles were returned")
    return run_stage1_from_bars(
        bars,
        output_dir=output_dir,
        range_pct_bounds=range_pct_bounds,
        fetch_report=fetch_report,
    )
