#!/usr/bin/env python3
"""Fast research-only Dhan downloader for the underlying campaign stress test.

Downloads:
- 5-minute NSE cash history for the frozen 210 F&O underlyings
- NIFTY 50 5-minute history for market residualisation
- NIFTY 50 daily history for long-regime diagnostics

It deliberately skips per-stock daily history to minimise API load. No orders
or production writes are possible from this script.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import io
import json
import os
import re
import time
import threading
from pathlib import Path

import pandas as pd
import requests

from dhan_v11_fetch import discover_universe, get_master

INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
DAILY_URL = "https://api.dhan.co/v2/charts/historical"

# Process-wide throttle: Dhan v2 Data APIs allow 5 requests/second.
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_REQUEST_SPACING_SECONDS = 0.26

def throttle():
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        if now < _NEXT_REQUEST_AT:
            time.sleep(_NEXT_REQUEST_AT - now)
            now = time.monotonic()
        _NEXT_REQUEST_AT = now + _REQUEST_SPACING_SECONDS



def headers():
    token = str(os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    client_id = str(os.environ.get("DHAN_CLIENT_ID") or "").strip()
    if not token or not client_id:
        raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


def post(url, payload, retries=8):
    last = None
    for attempt in range(retries):
        try:
            throttle()
            r = requests.post(url, headers=headers(), json=payload, timeout=90)
            if r.status_code == 200:
                out = r.json()
                if isinstance(out, dict):
                    return out
                raise RuntimeError("non-dict Dhan payload")
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code in (400, 401, 403):
                raise last
            if r.status_code == 429:
                time.sleep(min(8.0, 1.5 + attempt))
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(12.0, 0.8 * (1.7 ** attempt)))
    raise RuntimeError(f"Dhan request failed: {last}")


def frame(data):
    ts = data.get("timestamp") or []
    n = len(ts)
    if not n:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    def arr(k):
        x = data.get(k)
        return x if isinstance(x, list) and len(x) == n else [None] * n
    z = pd.DataFrame({
        "timestamp": ts,
        "open": arr("open"),
        "high": arr("high"),
        "low": arr("low"),
        "close": arr("close"),
        "volume": arr("volume"),
    })
    z["timestamp"] = pd.to_datetime(z.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    for c in ("open", "high", "low", "close", "volume"):
        z[c] = pd.to_numeric(z[c], errors="coerce")
    return z.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def chunks(start, end, days=89):
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end) + pd.Timedelta(days=1)
    while cur < stop:
        nxt = min(cur + pd.Timedelta(days=days), stop)
        yield cur, nxt
        cur = nxt


def fetch_intraday(sid, segment, instrument, start, end):
    fs = []
    for a, b in chunks(start, end):
        d = post(INTRADAY_URL, {
            "securityId": str(sid),
            "exchangeSegment": segment,
            "instrument": instrument,
            "interval": "5",
            "oi": False,
            "fromDate": a.strftime("%Y-%m-%d 09:15:00"),
            "toDate": b.strftime("%Y-%m-%d 09:15:00"),
        })
        fs.append(frame(d))
    if not fs:
        return pd.DataFrame()
    z = pd.concat(fs, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) + pd.Timedelta(days=1)
    z = z[(z.timestamp >= lo) & (z.timestamp < hi)]
    tod = z.timestamp.dt.time
    z = z[(tod >= dt.time(9, 15)) & (tod <= dt.time(15, 30)) & (z.timestamp.dt.dayofweek < 5)]
    z["trade_date"] = z.timestamp.dt.date.astype(str)
    return z.reset_index(drop=True)


def fetch_daily(sid, segment, instrument, start, end):
    # Daily history is not subject to the 90-day intraday response-size limit.
    d = post(DAILY_URL, {
        "securityId": str(sid),
        "exchangeSegment": segment,
        "instrument": instrument,
        "expiryCode": 0,
        "oi": False,
        "fromDate": start,
        "toDate": (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
    })
    z = frame(d)
    z["trade_date"] = z.timestamp.dt.date.astype(str) if len(z) else []
    return z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-09-25")
    ap.add_argument("--daily-start", default="2016-10-01")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    root = Path(args.out)
    p5 = root / "intraday5"
    pctx = root / "context"
    p5.mkdir(parents=True, exist_ok=True)
    pctx.mkdir(parents=True, exist_ok=True)

    stocks, indices = discover_universe(get_master())
    nifty = indices["NIFTY 50"]
    (root / "universe.json").write_text(json.dumps({
        "stocks": stocks,
        "indices": indices,
        "intraday_start": args.start,
        "intraday_end": args.end,
        "daily_start": args.daily_start,
    }, indent=2), encoding="utf-8")

    manifest = {"stocks": {}, "errors": []}

    def one(item):
        sym, sid = item
        try:
            z = fetch_intraday(sid, "NSE_EQ", "EQUITY", args.start, args.end)
            z.to_parquet(p5 / f"{sym}.parquet", index=False, compression="zstd")
            return sym, {"bars_5m": int(len(z)), "days_5m": int(z.trade_date.nunique()) if len(z) else 0}, None
        except Exception as exc:  # noqa: BLE001
            return sym, None, str(exc)

    items = sorted(stocks.items())
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for n, (sym, info, err) in enumerate(ex.map(one, items), 1):
            if err:
                manifest["errors"].append({"symbol": sym, "error": err})
            else:
                manifest["stocks"][sym] = info
            if n % 10 == 0:
                print(f"stocks {n}/{len(items)} errors={len(manifest['errors'])}", flush=True)

    nsid = str(nifty["security_id"])
    nf5 = fetch_intraday(nsid, "IDX_I", "INDEX", args.start, args.end)
    nd1 = fetch_daily(nsid, "IDX_I", "INDEX", args.daily_start, args.end)
    nf5.to_parquet(pctx / "NIFTY50_5m.parquet", index=False, compression="zstd")
    nd1.to_parquet(pctx / "NIFTY50_daily.parquet", index=False, compression="zstd")
    manifest["nifty"] = {
        "bars_5m": int(len(nf5)),
        "days_5m": int(nf5.trade_date.nunique()) if len(nf5) else 0,
        "daily_bars": int(len(nd1)),
    }
    (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if len(manifest["stocks"]) < 200:
        raise RuntimeError(f"Insufficient stock history coverage: {len(manifest['stocks'])}/210; errors={len(manifest['errors'])}")
    print(json.dumps({"stocks_ok": len(manifest["stocks"]), "errors": len(manifest["errors"]), "nifty": manifest["nifty"]}, indent=2))


if __name__ == "__main__":
    main()
