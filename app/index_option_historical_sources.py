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
