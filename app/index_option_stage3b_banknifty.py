"""BANK NIFTY zero-retune replication for Stage-3B.

Fetches Dhan's 1-minute BANKNIFTY index candles (security id 25, IDX_I, INDEX)
and applies the exact frozen Stage-3 signal unchanged.

Research-only. This module does not place orders, does not modify production,
and does not alter the V12/V12.1 or Trial-25 recorders.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

from .index_option_stage3 import (
    FROZEN_SPEC_SHA256,
    evaluate_frozen_spec,
    session_bootstrap_mean_ci,
)


DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
BANKNIFTY_SECURITY_ID = 25
BANKNIFTY_WEEKLY_LAST_EXPIRY = pd.Timestamp("2024-11-13")
BANKNIFTY_MONTHLY_ONLY_START = pd.Timestamp("2024-11-14")
DEFAULT_START = pd.Timestamp("2021-09-22")
DEFAULT_END = pd.Timestamp("2026-08-31")
EVIDENCE_CLASS = "DHAN_INDEX_OHLC_1M"


def _transport(url, *, headers, json, timeout):
    response = requests.post(url, headers=headers, json=json, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = (response.text or "").strip()
        if len(detail) > 800:
            detail = detail[:800] + "..."
        raise requests.HTTPError(
            f"{exc}; Dhan response={detail or '<empty>'}",
            response=response,
        ) from exc
    return response.json()


def normalize_dhan_intraday(payload: dict) -> pd.DataFrame:
    """Normalize Dhan intraday arrays into IST-indexed one-minute OHLC."""
    if not isinstance(payload, dict):
        raise ValueError("Dhan intraday response must be an object")
    if payload.get("status") == "failure":
        raise ValueError(f"Dhan intraday failure: {payload.get('remarks')}")

    required = ("timestamp", "open", "high", "low", "close")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"Dhan intraday response missing fields: {missing}")

    lengths = {k: len(payload.get(k) or []) for k in required}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"Dhan intraday arrays have inconsistent lengths: {lengths}")

    n = lengths["timestamp"]
    if n == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    ts = pd.to_datetime(payload["timestamp"], unit="s", utc=True, errors="coerce")
    frame = pd.DataFrame(
        {
            "timestamp": ts.tz_convert("Asia/Kolkata"),
            "open": pd.to_numeric(payload["open"], errors="coerce"),
            "high": pd.to_numeric(payload["high"], errors="coerce"),
            "low": pd.to_numeric(payload["low"], errors="coerce"),
            "close": pd.to_numeric(payload["close"], errors="coerce"),
            "volume": pd.to_numeric(payload.get("volume", [None] * n), errors="coerce"),
        }
    )
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    if frame.empty:
        return frame.set_index("timestamp")

    # Fail closed on malformed candles.
    valid = (
        frame["high"].ge(frame[["open", "close", "low"]].max(axis=1))
        & frame["low"].le(frame[["open", "close", "high"]].min(axis=1))
    )
    frame = frame[valid].copy()
    return (
        frame.drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp")
        .set_index("timestamp")
    )


def fetch_banknifty_intraday_chunk(
    *,
    access_token: str,
    from_ts,
    to_ts,
    transport: Callable = _transport,
    timeout: float = 30.0,
) -> pd.DataFrame:
    start = pd.Timestamp(from_ts)
    end = pd.Timestamp(to_ts)
    if end <= start:
        raise ValueError("to_ts must be after from_ts")
    if end - start > pd.Timedelta(days=90):
        raise ValueError("Dhan intraday API permits at most 90 days per request")
    if not str(access_token).strip():
        raise ValueError("access_token is required")

    body = {
        "securityId": str(BANKNIFTY_SECURITY_ID),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "interval": "1",
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = transport(
        DHAN_INTRADAY_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": str(access_token),
        },
        json=body,
        timeout=float(timeout),
    )
    if hasattr(payload, "json"):
        if hasattr(payload, "raise_for_status"):
            payload.raise_for_status()
        payload = payload.json()
    return normalize_dhan_intraday(payload)


def _chunk_bounds(start: pd.Timestamp, end: pd.Timestamp):
    """Yield non-overlapping <=89-day chunks; end is inclusive at session level."""
    cursor = pd.Timestamp(start).normalize()
    final = pd.Timestamp(end).normalize()
    while cursor <= final:
        chunk_last = min(cursor + pd.Timedelta(days=88), final)
        # Dhan accepts timestamps. End at market close of the final calendar day.
        yield (
            cursor + pd.Timedelta(hours=9, minutes=15),
            chunk_last + pd.Timedelta(hours=15, minutes=30),
        )
        cursor = chunk_last + pd.Timedelta(days=1)


def fetch_banknifty_history(
    *,
    access_token: str,
    output_dir,
    start=DEFAULT_START,
    end=DEFAULT_END,
    transport: Callable = _transport,
    sleep_seconds: float = 1.05,
    progress_callback: Callable | None = None,
) -> pd.DataFrame:
    """Checkpointed five-year BANKNIFTY minute history fetch."""
    output = Path(output_dir)
    chunks_dir = output / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    bounds = list(_chunk_bounds(pd.Timestamp(start), pd.Timestamp(end)))
    frames = []
    for i, (chunk_start, chunk_end) in enumerate(bounds, start=1):
        key = f"{chunk_start.date()}__{chunk_end.date()}"
        path = chunks_dir / f"{key}.csv"
        resumed = path.exists() and path.stat().st_size > 0
        if resumed:
            frame = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
            frame.index = pd.to_datetime(frame.index, utc=True).tz_convert("Asia/Kolkata")
        else:
            frame = fetch_banknifty_intraday_chunk(
                access_token=access_token,
                from_ts=chunk_start,
                to_ts=chunk_end,
                transport=transport,
            )
            frame.reset_index().to_csv(path, index=False)
            if sleep_seconds:
                time.sleep(float(sleep_seconds))
        frames.append(frame)
        if progress_callback:
            progress_callback(
                {
                    "chunk_number": i,
                    "chunks_total": len(bounds),
                    "chunk_start": str(chunk_start),
                    "chunk_end": str(chunk_end),
                    "rows": int(len(frame)),
                    "resumed": bool(resumed),
                }
            )

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    bars = pd.concat(frames)
    bars.index = pd.to_datetime(bars.index, utc=True).tz_convert("Asia/Kolkata")
    bars = bars.sort_index()
    bars = bars[~bars.index.duplicated(keep="last")]
    local_idx = pd.to_datetime(bars.index)
    # CSV resume can yield fixed-offset tz; compare via naive local wall time.
    if getattr(local_idx, "tz", None) is not None:
        naive_local = local_idx.tz_convert("Asia/Kolkata").tz_localize(None)
    else:
        naive_local = local_idx
    start_naive = pd.Timestamp(start).normalize()
    end_naive = pd.Timestamp(end).normalize() + pd.Timedelta(hours=23, minutes=59)
    mask = (naive_local >= start_naive) & (naive_local <= end_naive)
    bars = bars.loc[mask].copy()

    output.mkdir(parents=True, exist_ok=True)
    bars.reset_index().rename(columns={"index": "timestamp"}).to_csv(
        output / "banknifty_1m.csv",
        index=False,
    )
    return bars


def _summary(ledger: pd.DataFrame) -> dict:
    if ledger is None or ledger.empty:
        return {
            "trade_count": 0,
            "mean_120m_points": None,
            "median_120m_points": None,
            "bootstrap_ci_low": None,
            "bootstrap_ci_high": None,
        }
    x = pd.to_numeric(ledger["return_120m_points"], errors="coerce").dropna()
    ci = session_bootstrap_mean_ci(
        ledger.assign(session=pd.to_datetime(ledger["session"]).astype(str)),
        "return_120m_points",
        samples=5000,
        seed=3310,
    )
    return {
        "trade_count": int(len(x)),
        "mean_120m_points": float(x.mean()) if len(x) else None,
        "median_120m_points": float(x.median()) if len(x) else None,
        "win_rate_pct": float((x > 0).mean() * 100.0) if len(x) else None,
        "bootstrap_ci_low": ci.get("ci_low"),
        "bootstrap_ci_high": ci.get("ci_high"),
        "bootstrap_n_sessions": ci.get("n_sessions"),
    }


def run_banknifty_replication(bars_1m: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    ledger = evaluate_frozen_spec(bars_1m, instrument="BANK NIFTY")
    if ledger.empty:
        return ledger, {
            "status": "READY",
            "trade_count": 0,
            "mean_120m_points": None,
            "positive_gross": False,
            "retuned": False,
            "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        }

    ledger["session"] = pd.to_datetime(ledger["session"]).dt.normalize()
    pre = ledger[ledger["session"] <= BANKNIFTY_WEEKLY_LAST_EXPIRY].copy()
    post = ledger[ledger["session"] >= BANKNIFTY_MONTHLY_ONLY_START].copy()

    overall = _summary(ledger)
    report = {
        "status": "READY",
        **overall,
        "positive_gross": bool(
            overall["mean_120m_points"] is not None
            and float(overall["mean_120m_points"]) > 0
        ),
        "weekly_contract_regime_through": str(BANKNIFTY_WEEKLY_LAST_EXPIRY.date()),
        "monthly_only_regime_from": str(BANKNIFTY_MONTHLY_ONLY_START.date()),
        "weekly_regime": _summary(pre),
        "monthly_only_regime": _summary(post),
        "evidence_class": EVIDENCE_CLASS,
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "retuned": False,
        "production_deployed": False,
    }
    return ledger, report


def write_banknifty_artifacts(output_dir, *, bars, ledger, report):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    bars_path = output / "banknifty_1m.csv"
    ledger_path = output / "banknifty_stage3b_signals.csv"
    report_path = output / "banknifty_stage3b_replication.json"

    if not bars_path.exists():
        bars.reset_index().rename(columns={"index": "timestamp"}).to_csv(bars_path, index=False)
    ledger.to_csv(ledger_path, index=False)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return {
        "bars_path": bars_path,
        "ledger_path": ledger_path,
        "report_path": report_path,
        "report": report,
    }
