#!/usr/bin/env python3
"""Historical ATM stock-option overlay for underlying campaign events.

Uses Dhan rolling expired-option minute data only AFTER the underlying event
has been identified. The stock signal never sees option data.

Important: rolling ATM can change physical strike as spot moves. A hold is
counted only when entry and exit rows report the same strike.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import time
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROLLING_URL = "https://api.dhan.co/v2/charts/rollingoption"

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

SPREAD_SCENARIOS_PCT = {
    "recorder_median": 1.8059,
    "recorder_p75": 2.5532,
    "liquidity_gate": 4.0,
}


def headers():
    token = str(os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    client_id = str(os.environ.get("DHAN_CLIENT_ID") or "").strip()
    if not token or not client_id:
        raise RuntimeError("Dhan research secrets unavailable")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }


def post(payload, retries=7):
    last = None
    for attempt in range(retries):
        try:
            throttle()
            r = requests.post(ROLLING_URL, headers=headers(), json=payload, timeout=90)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict):
                    return d
                raise RuntimeError("non-dict rolling option response")
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:240]}")
            if r.status_code in (400, 401, 403):
                raise last
            if r.status_code == 429:
                time.sleep(min(8.0, 1.5 + attempt))
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(10.0, 0.7 * 1.7 ** attempt))
    raise RuntimeError(f"rolling option request failed: {last}")


def chunks(start, end, days=28):
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end) + pd.Timedelta(days=1)
    while cur < stop:
        nxt = min(cur + pd.Timedelta(days=days), stop)
        yield cur, nxt
        cur = nxt


def fetch_series(security_id: str, typ: str, start: str, end: str) -> pd.DataFrame:
    frames = []
    for a, b in chunks(start, end):
        payload = {
            "exchangeSegment": "NSE_FNO",
            "interval": "1",
            "securityId": str(security_id),
            "instrument": "OPTSTK",
            "expiryFlag": "MONTH",
            "expiryCode": 1,
            "strike": "ATM",
            "drvOptionType": typ,
            "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
            "fromDate": a.strftime("%Y-%m-%d"),
            "toDate": b.strftime("%Y-%m-%d"),
        }
        raw = post(payload)
        data = raw.get("data") or {}
        side = data.get("ce" if typ == "CALL" else "pe")
        if not isinstance(side, dict):
            # Some Dhan responses may return the requested side under the other
            # key shape; fail closed rather than infer.
            continue
        ts = side.get("timestamp") or []
        n = len(ts)
        if not n:
            continue
        def arr(name):
            x = side.get(name)
            return x if isinstance(x, list) and len(x) == n else [None] * n
        f = pd.DataFrame({
            "timestamp": ts,
            "open": arr("open"),
            "high": arr("high"),
            "low": arr("low"),
            "close": arr("close"),
            "iv": arr("iv"),
            "volume": arr("volume"),
            "strike": arr("strike"),
            "oi": arr("oi"),
            "spot": arr("spot"),
        })
        f["timestamp"] = pd.to_datetime(f.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    for c in ("open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["trade_date"] = out.timestamp.dt.date.astype(str)
    return out.reset_index(drop=True)


def get_at_or_after(f: pd.DataFrame, t: pd.Timestamp, max_lag_min=2):
    if f.empty:
        return None
    arr = f.timestamp.to_numpy(dtype="datetime64[ns]")
    k = int(np.searchsorted(arr, np.datetime64(t), side="left"))
    if k >= len(f):
        return None
    row = f.iloc[k]
    lag = (pd.Timestamp(row.timestamp) - t).total_seconds() / 60.0
    if lag < 0 or lag > max_lag_min:
        return None
    return row


def spread_adjusted_ret(entry: float, exit_: float, spread_pct: float):
    if not (np.isfinite(entry) and np.isfinite(exit_) and entry > 0 and exit_ > 0):
        return np.nan
    h = spread_pct / 200.0
    return ((exit_ * (1.0 - h)) / (entry * (1.0 + h)) - 1.0) * 100.0


def summarize(df: pd.DataFrame, col: str):
    z = pd.to_numeric(df[col], errors="coerce").dropna()
    if z.empty:
        return {"n": 0}
    return {
        "n": int(len(z)),
        "mean_pct": float(z.mean()),
        "median_pct": float(z.median()),
        "hit_rate": float((z > 0).mean()),
        "p_gt_10pct": float((z > 10).mean()),
        "p_gt_20pct": float((z > 20).mean()),
        "p_gt_30pct": float((z > 30).mean()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    root, out = Path(args.data_root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    universe = json.loads((root / "universe.json").read_text(encoding="utf-8"))
    stocks = universe["stocks"]
    ev = pd.read_parquet(args.events)
    ev = ev[ev.split == "OOS"].copy()
    ev = ev[(ev.trade_date >= "2025-10-01") & (ev.trade_date <= "2026-09-25")]
    if ev.empty:
        raise RuntimeError("No OOS events for option overlay")

    # Fetch only the symbol/type combinations actually required by events.
    needs = set()
    for r in ev.itertuples():
        needs.add((r.symbol, "CALL" if int(r.direction) > 0 else "PUT"))

    rawdir = out / "raw"
    rawdir.mkdir(exist_ok=True)
    errors = []

    def one(item):
        sym, typ = item
        try:
            f = fetch_series(stocks[sym], typ, "2025-10-01", "2026-09-25")
            p = rawdir / f"{sym}_{typ}.parquet"
            f.to_parquet(p, index=False, compression="zstd")
            return sym, typ, len(f), None
        except Exception as exc:  # noqa: BLE001
            return sym, typ, 0, str(exc)

    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for n, (sym, typ, count, err) in enumerate(ex.map(one, sorted(needs)), 1):
            if err:
                errors.append({"symbol": sym, "type": typ, "error": err})
            if n % 20 == 0:
                print(f"option fetch {n}/{len(needs)} errors={len(errors)}", flush=True)

    cache = {}
    rows = []
    for r in ev.itertuples():
        typ = "CALL" if int(r.direction) > 0 else "PUT"
        key = (r.symbol, typ)
        if key not in cache:
            p = rawdir / f"{r.symbol}_{typ}.parquet"
            cache[key] = pd.read_parquet(p) if p.exists() else pd.DataFrame()
        f = cache[key]
        for state, ts_name in (("impulse", "impulse_ts"), ("reclaim", "reclaim_ts")):
            ts = getattr(r, ts_name, None)
            if ts is None or pd.isna(ts):
                continue
            # Underlying 5-minute candle timestamp labels the bar; the signal is
            # known only after that bar closes.
            decision = pd.Timestamp(ts) + pd.Timedelta(minutes=5)
            ent = get_at_or_after(f, decision)
            if ent is None or not np.isfinite(ent.close) or ent.close <= 0 or not np.isfinite(ent.strike):
                continue
            base = {
                "symbol": r.symbol,
                "trade_date": r.trade_date,
                "direction": int(r.direction),
                "option_type": typ,
                "state": state,
                "underlying_event_ts": pd.Timestamp(ts),
                "option_entry_ts": pd.Timestamp(ent.timestamp),
                "entry_close": float(ent.close),
                "entry_strike": float(ent.strike),
                "entry_iv": float(ent.iv) if np.isfinite(ent.iv) else np.nan,
                "entry_spot": float(ent.spot) if np.isfinite(ent.spot) else np.nan,
            }
            for mins in (5, 10, 15, 30):
                exrow = get_at_or_after(f, decision + pd.Timedelta(minutes=mins))
                valid = (
                    exrow is not None
                    and np.isfinite(exrow.close)
                    and exrow.close > 0
                    and np.isfinite(exrow.strike)
                    and float(exrow.strike) == float(ent.strike)
                    and str(exrow.trade_date) == str(ent.trade_date)
                )
                if not valid:
                    base[f"raw_{mins}m_pct"] = np.nan
                    for sc in SPREAD_SCENARIOS_PCT:
                        base[f"{sc}_{mins}m_pct"] = np.nan
                    continue
                exprice = float(exrow.close)
                base[f"raw_{mins}m_pct"] = (exprice / float(ent.close) - 1.0) * 100.0
                for sc, spread in SPREAD_SCENARIOS_PCT.items():
                    base[f"{sc}_{mins}m_pct"] = spread_adjusted_ret(float(ent.close), exprice, spread)
            rows.append(base)

    z = pd.DataFrame(rows)
    z.to_parquet(out / "option_event_overlay.parquet", index=False, compression="zstd")
    summary = {
        "research_only": True,
        "production_changed": False,
        "oos_events_input": int(len(ev)),
        "option_rows": int(len(z)),
        "fetch_errors": errors,
        "spread_reference": {
            "source": "V12 frozen executable ATM-straddle recorder",
            "trading_days_frozen": 10,
            "slots_captured": 39,
            "median_straddle_spread_pct": 1.8059,
            "p75_straddle_spread_pct": 2.5532,
            "symbols_below_4pct": 195,
            "term_structure_coverage_pct": 95.1,
            "stale_quote_rate_pct": 50.48,
            "note": "Spread scenarios are sensitivity approximations; Dhan rolling history has OHLC/IV/OI/spot but not historical bid/ask.",
        },
        "states": {},
    }
    for state in ("impulse", "reclaim"):
        q = z[z.state == state]
        ss = {}
        for mins in (5, 10, 15, 30):
            ss[f"raw_{mins}m"] = summarize(q, f"raw_{mins}m_pct")
            ss[f"median_spread_{mins}m"] = summarize(q, f"recorder_median_{mins}m_pct")
            ss[f"p75_spread_{mins}m"] = summarize(q, f"recorder_p75_{mins}m_pct")
            ss[f"4pct_spread_{mins}m"] = summarize(q, f"liquidity_gate_{mins}m_pct")
        summary["states"][state] = ss

    (out / "option_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = [
        "# Historical option overlay",
        "",
        "**Underlying selects the event; option data is used only after selection.**",
        "",
        f"- OOS underlying events supplied: {len(ev)}",
        f"- Option event rows matched: {len(z)}",
        f"- Option fetch errors: {len(errors)}",
        "",
        "| State | Horizon | Raw premium mean | Median-spread adjusted | P75-spread adjusted | Median-spread hit |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for state in ("impulse", "reclaim"):
        for mins in (5, 10, 15, 30):
            a = summary["states"][state][f"raw_{mins}m"]
            b = summary["states"][state][f"median_spread_{mins}m"]
            c = summary["states"][state][f"p75_spread_{mins}m"]
            lines.append(
                f"| {state} | {mins}m | {a.get('mean_pct',np.nan):.2f}% | "
                f"{b.get('mean_pct',np.nan):.2f}% | {c.get('mean_pct',np.nan):.2f}% | "
                f"{100*b.get('hit_rate',np.nan):.1f}% |"
            )
    (out / "option_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
