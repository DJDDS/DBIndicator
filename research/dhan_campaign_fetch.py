#!/usr/bin/env python3
"""Research-only long-history downloader for underlying campaign research.

Purpose:
- 5 years of Dhan 5-minute cash data for current pre-30-Sep-2026 F&O stocks
- 10 years of Dhan daily cash data for long-regime context
- matching NIFTY 50 history

No orders, no production writes, no Railway mutations.
Credentials are read only from GitHub Actions secrets:
DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

from dhan_v11_fetch import discover_universe, get_master

INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
DAILY_URL = "https://api.dhan.co/v2/charts/historical"


def _headers():
    token = str(os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    client_id = str(os.environ.get("DHAN_CLIENT_ID") or "").strip()
    if not token or not client_id:
        raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are required")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


def _post(url: str, payload: dict, retries: int = 7) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=_headers(), json=payload, timeout=90)
            if r.status_code == 200:
                out = r.json()
                if isinstance(out, dict):
                    return out
                raise RuntimeError("non-dict Dhan response")
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:240]}")
            if r.status_code in (400, 401, 403):
                raise last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(12.0, 0.75 * (1.7 ** attempt)))
    raise RuntimeError(f"Dhan request failed after retries: {last}")


def _frame(data: dict) -> pd.DataFrame:
    ts = data.get("timestamp") or []
    n = len(ts)
    if not n:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    def arr(name):
        x = data.get(name)
        return x if isinstance(x, list) and len(x) == n else [None] * n
    out = pd.DataFrame({
        "timestamp": ts,
        "open": arr("open"),
        "high": arr("high"),
        "low": arr("low"),
        "close": arr("close"),
        "volume": arr("volume"),
    })
    out["timestamp"] = pd.to_datetime(out["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = out.drop_duplicates("timestamp").sort_values("timestamp")
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.reset_index(drop=True)


def _chunks(start: str, end: str, days: int):
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end) + pd.Timedelta(days=1)
    while cur < stop:
        nxt = min(cur + pd.Timedelta(days=days), stop)
        yield cur, nxt
        cur = nxt


def fetch_intraday(security_id: str, exchange_segment: str, instrument: str,
                   start: str, end: str, interval: str = "5") -> pd.DataFrame:
    frames = []
    for a, b in _chunks(start, end, 89):
        data = _post(INTRADAY_URL, {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval),
            "oi": False,
            "fromDate": a.strftime("%Y-%m-%d 09:15:00"),
            "toDate": b.strftime("%Y-%m-%d 09:15:00"),
        })
        frames.append(_frame(data))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    out = out[(out.timestamp >= lo) & (out.timestamp < hi)]
    tod = out.timestamp.dt.time
    out = out[(tod >= dt.time(9, 15)) & (tod <= dt.time(15, 30)) & (out.timestamp.dt.dayofweek < 5)]
    out["trade_date"] = out.timestamp.dt.date.astype(str)
    return out.reset_index(drop=True)


def fetch_daily(security_id: str, exchange_segment: str, instrument: str,
                start: str, end: str) -> pd.DataFrame:
    # Daily API is available back to instrument inception. Use annual chunks
    # so retries remain bounded even if one historical segment has an issue.
    frames = []
    for a, b in _chunks(start, end, 365):
        data = _post(DAILY_URL, {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "expiryCode": 0,
            "oi": False,
            "fromDate": a.strftime("%Y-%m-%d"),
            "toDate": b.strftime("%Y-%m-%d"),
        })
        frames.append(_frame(data))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    out["trade_date"] = out.timestamp.dt.date.astype(str)
    return out.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intraday-start", default="2021-10-01")
    ap.add_argument("--intraday-end", default="2026-09-25")
    ap.add_argument("--daily-start", default="2016-10-01")
    ap.add_argument("--daily-end", default="2026-09-25")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    root = Path(args.out)
    p5 = root / "intraday5"
    pd1 = root / "daily"
    pctx = root / "context"
    for p in (p5, pd1, pctx):
        p.mkdir(parents=True, exist_ok=True)

    master = get_master()
    stocks, indices = discover_universe(master)
    nifty = indices["NIFTY 50"]
    universe = {
        "stocks": stocks,
        "indices": indices,
        "intraday_start": args.intraday_start,
        "intraday_end": args.intraday_end,
        "daily_start": args.daily_start,
        "daily_end": args.daily_end,
    }
    (root / "universe.json").write_text(json.dumps(universe, indent=2), encoding="utf-8")

    manifest = {"stocks": {}, "nifty": {}, "errors": []}

    def one(item):
        sym, sid = item
        try:
            f5 = fetch_intraday(sid, "NSE_EQ", "EQUITY", args.intraday_start, args.intraday_end, "5")
            d1 = fetch_daily(sid, "NSE_EQ", "EQUITY", args.daily_start, args.daily_end)
            f5.to_parquet(p5 / f"{sym}.parquet", index=False, compression="zstd")
            d1.to_parquet(pd1 / f"{sym}.parquet", index=False, compression="zstd")
            return sym, {
                "bars_5m": int(len(f5)),
                "days_5m": int(f5.trade_date.nunique()) if len(f5) else 0,
                "daily_bars": int(len(d1)),
                "first_5m": str(f5.timestamp.min()) if len(f5) else None,
                "last_5m": str(f5.timestamp.max()) if len(f5) else None,
                "first_daily": str(d1.timestamp.min()) if len(d1) else None,
                "last_daily": str(d1.timestamp.max()) if len(d1) else None,
            }, None
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

    # NIFTY 50 context.
    sid = str(nifty["security_id"])
    nf5 = fetch_intraday(sid, "IDX_I", "INDEX", args.intraday_start, args.intraday_end, "5")
    nd1 = fetch_daily(sid, "IDX_I", "INDEX", args.daily_start, args.daily_end)
    nf5.to_parquet(pctx / "NIFTY50_5m.parquet", index=False, compression="zstd")
    nd1.to_parquet(pctx / "NIFTY50_daily.parquet", index=False, compression="zstd")
    manifest["nifty"] = {
        "bars_5m": int(len(nf5)),
        "days_5m": int(nf5.trade_date.nunique()) if len(nf5) else 0,
        "daily_bars": int(len(nd1)),
    }

    (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({
        "stocks_ok": len(manifest["stocks"]),
        "stock_errors": len(manifest["errors"]),
        "nifty": manifest["nifty"],
    }, indent=2))


if __name__ == "__main__":
    main()
