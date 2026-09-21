"""Historical option-data sources for Index Option Buying Stage 3.

Important research boundary:
- Dhan/Breeze-style rolling historical OHLC is useful for premium-path screening,
  but it is NOT an executable bid/ask archive and cannot satisfy the Stage-3
  historical executable-P&L gate.
- Only validated bid/ask snapshots are tagged EXECUTABLE_BID_ASK.

No function here places orders or changes the frozen Stage-3 signal.
"""
from __future__ import annotations

import re
import time
from typing import Callable

import pandas as pd
import requests


DHAN_ROLLING_URL = "https://api.dhan.co/v2/charts/rollingoption"
DHAN_NIFTY_SECURITY_ID = 13
_PROXY_FIELDS = ("open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot")
_EXECUTABLE_REQUIRED = {
    "snapshot_ts",
    "tradingsymbol",
    "instrument_token",
    "expiry",
    "strike",
    "type",
    "lot_size",
    "best_bid",
    "best_ask",
    "bid_qty",
    "ask_qty",
    "spot",
}


def _option_key(option_type: str) -> str:
    typ = str(option_type).upper()
    if typ == "CALL":
        return "ce"
    if typ == "PUT":
        return "pe"
    raise ValueError("option_type must be CALL or PUT")


def _validate_expression(expression: str) -> str:
    value = str(expression).upper().strip()
    if not re.fullmatch(r"ATM(?:[+-](?:10|[1-9]))?", value):
        raise ValueError("expression must be ATM or ATM+/-1..10")
    return value


def normalize_dhan_rolling_response(
    payload: dict,
    *,
    expression: str,
    option_type: str,
) -> pd.DataFrame:
    """Normalize Dhan rolling expired-option OHLC without inventing quotes."""
    expression = _validate_expression(expression)
    key = _option_key(option_type)
    root = (payload or {}).get("data") or {}
    series = root.get(key)
    if not isinstance(series, dict):
        return pd.DataFrame(
            columns=[
                "timestamp",
                *_PROXY_FIELDS,
                "expression",
                "option_type",
                "data_source",
                "data_quality",
                "executable_quote",
            ]
        )

    timestamps = list(series.get("timestamp") or [])
    lengths = {"timestamp": len(timestamps)}
    for field in _PROXY_FIELDS:
        if field in series and series.get(field) is not None:
            lengths[field] = len(list(series.get(field) or []))
    nonzero_lengths = set(lengths.values())
    if len(nonzero_lengths) > 1:
        raise ValueError(f"Dhan rolling response array lengths do not match: {lengths}")

    if not timestamps:
        return pd.DataFrame(
            columns=[
                "timestamp",
                *_PROXY_FIELDS,
                "expression",
                "option_type",
                "data_source",
                "data_quality",
                "executable_quote",
            ]
        )

    rows = {"timestamp": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Kolkata")}
    for field in _PROXY_FIELDS:
        values = list(series.get(field) or [])
        rows[field] = values if values else [None] * len(timestamps)

    frame = pd.DataFrame(rows)
    frame["expression"] = expression
    frame["option_type"] = str(option_type).upper()
    frame["data_source"] = "DHAN_ROLLING_EXPIRED_OPTIONS"
    frame["data_quality"] = "PROXY_OHLC"
    frame["executable_quote"] = False
    return frame.sort_values("timestamp").reset_index(drop=True)


def _requests_transport(url, *, headers, json, timeout):
    response = requests.post(url, headers=headers, json=json, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_dhan_expired_options(
    *,
    access_token: str,
    from_date: str,
    to_date: str,
    option_type: str,
    expression: str,
    expiry_flag: str = "WEEK",
    expiry_code: int = 0,
    security_id: int = DHAN_NIFTY_SECURITY_ID,
    interval: int = 1,
    transport: Callable = _requests_transport,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch up to 30 days of Dhan rolling expired NIFTY option minute OHLC.

    The returned frame is always PROXY_OHLC and executable_quote=False because
    Dhan's expired-options endpoint provides OHLC/IV/OI/volume/spot, not
    historical best bid/ask.
    """
    start = pd.Timestamp(from_date)
    end = pd.Timestamp(to_date)
    if end <= start:
        raise ValueError("to_date must be after from_date")
    if (end - start) > pd.Timedelta(days=30):
        raise ValueError("Dhan expired-options API permits at most 30 days per request")
    if not str(access_token).strip():
        raise ValueError("access_token is required")

    typ = str(option_type).upper()
    _option_key(typ)
    expression = _validate_expression(expression)
    expiry_flag = str(expiry_flag).upper()
    if expiry_flag not in {"WEEK", "MONTH"}:
        raise ValueError("expiry_flag must be WEEK or MONTH")
    if int(expiry_code) not in {0, 1, 2}:
        raise ValueError("expiry_code must be 0, 1, or 2")
    if int(interval) not in {1, 5, 15, 25, 60}:
        raise ValueError("interval must be 1, 5, 15, 25, or 60")

    body = {
        "exchangeSegment": "NSE_FNO",
        "interval": str(int(interval)),
        "securityId": int(security_id),
        "instrument": "OPTIDX",
        "expiryFlag": expiry_flag,
        "expiryCode": int(expiry_code),
        "strike": expression,
        "drvOptionType": typ,
        "requiredData": list(_PROXY_FIELDS),
        "fromDate": start.date().isoformat(),
        "toDate": end.date().isoformat(),
    }
    payload = transport(
        DHAN_ROLLING_URL,
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
    return normalize_dhan_rolling_response(
        payload,
        expression=expression,
        option_type=typ,
    )


def validate_executable_quote_archive(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a normalized historical best-bid/ask archive.

    This validator deliberately does not derive bid/ask from OHLC or LTP.
    """
    if frame is None:
        raise ValueError("archive is required")
    missing = sorted(_EXECUTABLE_REQUIRED.difference(frame.columns))
    if missing:
        raise ValueError(f"historical executable archive missing columns: {missing}")
    out = frame.copy()
    out["snapshot_ts"] = pd.to_datetime(out["snapshot_ts"], errors="coerce")
    if out["snapshot_ts"].isna().any():
        raise ValueError("historical executable archive contains invalid snapshot_ts")

    numeric = (
        "instrument_token",
        "strike",
        "lot_size",
        "best_bid",
        "best_ask",
        "bid_qty",
        "ask_qty",
        "spot",
    )
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if out[list(numeric)].isna().any().any():
        raise ValueError("historical executable archive contains non-numeric required values")
    if (out["lot_size"] <= 0).any():
        raise ValueError("historical executable archive contains invalid lot_size")
    if (out["best_bid"] <= 0).any() or (out["best_ask"] <= 0).any():
        raise ValueError("historical executable archive contains non-positive quotes")
    if (out["best_bid"] > out["best_ask"]).any():
        raise ValueError("historical executable archive contains crossed quotes")
    if (out["bid_qty"] < 0).any() or (out["ask_qty"] < 0).any():
        raise ValueError("historical executable archive contains negative top quantity")

    out["data_quality"] = "EXECUTABLE_BID_ASK"
    out["executable_quote"] = True
    return out.sort_values(["snapshot_ts", "strike", "type"]).reset_index(drop=True)


def dhan_proxy_expressions() -> tuple[str, ...]:
    """Return the complete Dhan near-expiry index rolling strike grid."""
    return tuple(
        [f"ATM-{n}" for n in range(10, 0, -1)]
        + ["ATM"]
        + [f"ATM+{n}" for n in range(1, 11)]
    )


def _first_proxy_row_at_or_after(
    frame: pd.DataFrame,
    target,
    *,
    max_delay_seconds: int = 60,
) -> dict | None:
    if frame is None or frame.empty:
        return None
    work = frame.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp")
    target = pd.Timestamp(target)
    eligible = work[work["timestamp"] >= target]
    if eligible.empty:
        return None
    row = eligible.iloc[0]
    if (pd.Timestamp(row["timestamp"]) - target).total_seconds() > max_delay_seconds:
        return None
    return row.to_dict()


def map_signal_to_dhan_proxy_pnl(
    signal: dict,
    rolling: pd.DataFrame,
    *,
    moneyness: str,
    hold_minutes: int = 120,
    max_delay_seconds: int = 60,
) -> dict:
    """Map one frozen signal to same-contract Dhan rolling OHLC proxy P&L.

    This is deliberately NON-EXECUTABLE evidence.  Entry/exit use the OPEN of
    the first completed minute bucket at/after the target timestamp.  The exit
    is located by the same absolute strike across all relative rolling buckets,
    so a contract that migrates from ATM to ATM-2 is not accidentally replaced
    by the new ATM contract.
    """
    if rolling is None or rolling.empty:
        return {"status": "NO_PROXY_DATA", "moneyness": str(moneyness).upper()}

    direction = str(signal.get("direction"))
    option_type = "CALL" if direction == "Bullish" else "PUT" if direction == "Bearish" else None
    if option_type is None:
        return {"status": "INVALID_DIRECTION", "moneyness": str(moneyness).upper()}

    m = str(moneyness).upper()
    if m == "ATM":
        entry_expression = "ATM"
    elif m == "ITM1":
        entry_expression = "ATM-1" if option_type == "CALL" else "ATM+1"
    else:
        raise ValueError("moneyness must be ATM or ITM1")

    work = rolling.copy()
    if "option_type" not in work.columns or "expression" not in work.columns:
        return {"status": "INVALID_PROXY_SCHEMA", "moneyness": m}
    work = work[
        work["option_type"].astype(str).str.upper().eq(option_type)
    ].copy()
    if work.empty:
        return {"status": "NO_PROXY_OPTION_SIDE", "moneyness": m}

    signal_time = pd.Timestamp(signal["signal_time"])
    entry_pool = work[work["expression"].astype(str).str.upper().eq(entry_expression)]
    entry = _first_proxy_row_at_or_after(
        entry_pool,
        signal_time,
        max_delay_seconds=max_delay_seconds,
    )
    if entry is None:
        return {"status": "NO_PROXY_ENTRY", "moneyness": m}

    try:
        entry_price = float(entry["open"])
        strike = float(entry["strike"])
    except (TypeError, ValueError, KeyError):
        return {"status": "INVALID_PROXY_ENTRY", "moneyness": m}
    if not pd.notna(entry_price) or entry_price <= 0 or not pd.notna(strike):
        return {"status": "INVALID_PROXY_ENTRY", "moneyness": m}

    exit_target = signal_time + pd.Timedelta(minutes=int(hold_minutes))
    strikes = pd.to_numeric(work.get("strike"), errors="coerce")
    same_contract = work[strikes.eq(strike)].copy()
    exit_row = _first_proxy_row_at_or_after(
        same_contract,
        exit_target,
        max_delay_seconds=max_delay_seconds,
    )
    if exit_row is None:
        return {
            "status": "SAME_STRIKE_NOT_FOUND_AT_EXIT",
            "moneyness": m,
            "strike": strike,
        }

    try:
        exit_price = float(exit_row["open"])
    except (TypeError, ValueError, KeyError):
        return {"status": "INVALID_PROXY_EXIT", "moneyness": m, "strike": strike}
    if not pd.notna(exit_price) or exit_price <= 0:
        return {"status": "INVALID_PROXY_EXIT", "moneyness": m, "strike": strike}

    premium_points = exit_price - entry_price
    return {
        "status": "OK_PROXY",
        "session": str(signal.get("session")),
        "direction": direction,
        "signal_time": signal_time.isoformat(),
        "exit_target": exit_target.isoformat(),
        "moneyness": m,
        "option_type": option_type,
        "strike": strike,
        "entry_expression": str(entry.get("expression")),
        "exit_expression": str(exit_row.get("expression")),
        "entry_timestamp": pd.Timestamp(entry["timestamp"]).isoformat(),
        "exit_timestamp": pd.Timestamp(exit_row["timestamp"]).isoformat(),
        "entry_proxy_price": entry_price,
        "exit_proxy_price": exit_price,
        "premium_points": float(premium_points),
        "premium_return_pct": float(premium_points / entry_price * 100.0),
        "data_quality": "PROXY_OHLC_NO_BID_ASK",
        "executable": False,
        "can_satisfy_stage3_executable_gate": False,
    }


def build_dhan_proxy_ledger(
    signals: pd.DataFrame,
    *,
    access_token: str,
    fetcher=fetch_dhan_expired_options,
    sleep_fn=time.sleep,
    throttle_seconds: float = 0.22,
    progress_callback=None,
) -> pd.DataFrame:
    """Fetch Dhan rolling proxy data only on frozen-signal dates and score ATM/ITM1.

    To preserve the same absolute contract through a 120-minute hold, the
    complete ATM-10..ATM+10 near-weekly grid is fetched for the required option
    side on each signal date.  Only the two entry expressions (ATM and one
    strike ITM) are scored; the wider grid exists solely to find that same
    absolute strike at exit after the rolling moneyness label changes.

    The returned rows remain non-executable proxy evidence.
    """
    if signals is None or signals.empty:
        return pd.DataFrame()
    if not str(access_token).strip():
        raise ValueError("access_token is required")
    required = {"session", "direction", "signal_time"}
    missing = required.difference(signals.columns)
    if missing:
        raise ValueError(f"signals missing columns: {sorted(missing)}")

    outputs = []
    expressions = dhan_proxy_expressions()
    total = len(signals)

    for seq, (_, signal_row) in enumerate(signals.iterrows(), start=1):
        signal = signal_row.to_dict()
        session_date = pd.Timestamp(signal["session"]).date()
        next_date = session_date + pd.Timedelta(days=1)
        direction = str(signal["direction"])
        option_type = "CALL" if direction == "Bullish" else "PUT" if direction == "Bearish" else None
        if option_type is None:
            outputs.append(
                {
                    "status": "INVALID_DIRECTION",
                    "session": str(signal.get("session")),
                    "direction": direction,
                    "moneyness": "ATM",
                    "executable": False,
                    "can_satisfy_stage3_executable_gate": False,
                }
            )
            continue

        frames = []
        for i, expression in enumerate(expressions):
            frame = fetcher(
                access_token=access_token,
                from_date=session_date.isoformat(),
                to_date=next_date.date().isoformat()
                if hasattr(next_date, "date")
                else str(next_date),
                option_type=option_type,
                expression=expression,
            )
            if frame is not None and not frame.empty:
                frames.append(frame)
            if throttle_seconds > 0 and i + 1 < len(expressions):
                sleep_fn(float(throttle_seconds))

        rolling = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        for moneyness in ("ATM", "ITM1"):
            row = map_signal_to_dhan_proxy_pnl(
                signal,
                rolling,
                moneyness=moneyness,
            )
            # Retain the frozen-signal audit fields without changing the proxy status.
            for key in (
                "opening_range_minutes",
                "trigger_minutes",
                "confirmation",
                "range_pct",
                "range_width",
                "signal_close",
                "return_120m_points",
                "frozen_spec_sha256",
            ):
                if key in signal and key not in row:
                    row[key] = signal[key]
            outputs.append(row)

        if progress_callback is not None:
            progress_callback(
                {
                    "signal_number": seq,
                    "signals_total": total,
                    "session": session_date.isoformat(),
                    "direction": direction,
                    "api_calls": len(expressions),
                }
            )

    return pd.DataFrame.from_records(outputs)
