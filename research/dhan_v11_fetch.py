#!/usr/bin/env python3
"""Research-only Dhan V1.1 historical downloader.

Uses Dhan v2 historical 1-minute candles. Does not place orders and does not
touch production services.

Environment:
  DHAN_CLIENT_ID
  DHAN_ACCESS_TOKEN
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"
EXCLUDE_POST_SAMPLE_FNO = {"ANANDRATHI", "ENRIN", "UJJIVANSFB"}

# The 21 established sectoral indices used for the auditor-period comparison.
# New 15-Jun-2026 sectoral launches are deliberately excluded.
SECTOR_INDEX_ALIASES = {
    "NIFTY AUTO": ["NIFTY AUTO"],
    "NIFTY BANK": ["NIFTY BANK", "BANK NIFTY"],
    "NIFTY FINANCIAL SERVICES": ["NIFTY FINANCIAL SERVICES", "NIFTY FIN SERVICE", "FINNIFTY"],
    "NIFTY FINANCIAL SERVICES 25/50": ["NIFTY FINANCIAL SERVICES 25/50", "NIFTY FINSRV25 50", "NIFTY FINSRV25/50"],
    "NIFTY FINANCIAL SERVICES EX BANK": ["NIFTY FINANCIAL SERVICES EX BANK", "NIFTY FINANCIAL SERVICES EX-BANK"],
    "NIFTY FMCG": ["NIFTY FMCG"],
    "NIFTY HEALTHCARE": ["NIFTY HEALTHCARE", "NIFTY HEALTHCARE INDEX"],
    "NIFTY IT": ["NIFTY IT"],
    "NIFTY MEDIA": ["NIFTY MEDIA"],
    "NIFTY METAL": ["NIFTY METAL"],
    "NIFTY PHARMA": ["NIFTY PHARMA"],
    "NIFTY PRIVATE BANK": ["NIFTY PRIVATE BANK", "NIFTY PVT BANK"],
    "NIFTY PSU BANK": ["NIFTY PSU BANK"],
    "NIFTY REALTY": ["NIFTY REALTY"],
    "NIFTY CONSUMER DURABLES": ["NIFTY CONSUMER DURABLES"],
    "NIFTY OIL & GAS": ["NIFTY OIL & GAS", "NIFTY OIL AND GAS"],
    "NIFTY MIDSMALL FINANCIAL SERVICES": ["NIFTY MIDSMALL FINANCIAL SERVICES"],
    "NIFTY MIDSMALL HEALTHCARE": ["NIFTY MIDSMALL HEALTHCARE"],
    "NIFTY MIDSMALL IT & TELECOM": ["NIFTY MIDSMALL IT & TELECOM", "NIFTY MIDSMALL IT AND TELECOM"],
    "NIFTY CHEMICALS": ["NIFTY CHEMICALS"],
    "NIFTY500 HEALTHCARE": ["NIFTY500 HEALTHCARE", "NIFTY 500 HEALTHCARE"],
}
NIFTY_ALIASES = ["NIFTY 50", "NIFTY50"]


def norm(s: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_master() -> pd.DataFrame:
    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), low_memory=False)


def col(df: pd.DataFrame, *names: str) -> str | None:
    by_norm = {norm(x): x for x in df.columns}
    for name in names:
        hit = by_norm.get(norm(name))
        if hit:
            return hit
    return None


def discover_universe(master: pd.DataFrame) -> tuple[dict, dict]:
    c_exch = col(master, "EXCH_ID", "SEM_EXM_EXCH_ID")
    c_seg = col(master, "SEGMENT", "SEM_SEGMENT")
    c_inst = col(master, "INSTRUMENT", "SEM_INSTRUMENT_NAME")
    c_under_sym = col(master, "UNDERLYING_SYMBOL")
    c_under_id = col(master, "UNDERLYING_SECURITY_ID")
    c_sec_id = col(master, "SECURITY_ID", "SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID")
    c_sym = col(master, "SYMBOL_NAME", "SM_SYMBOL_NAME")
    c_display = col(master, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL")
    c_type = col(master, "INSTRUMENT_TYPE", "SEM_EXCH_INSTRUMENT_TYPE")

    required = [c_inst, c_under_sym, c_under_id, c_sec_id]
    if any(x is None for x in required):
        raise RuntimeError(f"Unexpected Dhan master columns: {list(master.columns)}")

    inst = master[c_inst].astype(str).str.upper()
    exch_mask = pd.Series(True, index=master.index)
    if c_exch:
        exch_mask &= master[c_exch].astype(str).str.upper().eq("NSE")
    if c_seg:
        seg = master[c_seg].astype(str).str.upper()
        deriv_mask = seg.isin(["D", "FNO", "NSE_FNO", "DERIVATIVES"])
    else:
        deriv_mask = pd.Series(True, index=master.index)
    stock_deriv = master[
        exch_mask
        & deriv_mask
        & inst.isin(["FUTSTK", "OPTSTK"])
        & master[c_under_sym].notna()
        & master[c_under_id].notna()
    ].copy()
    if stock_deriv.empty:
        # Instrument master variants can label derivatives by INSTRUMENT_TYPE.
        type_s = master[c_type].astype(str).str.upper() if c_type else inst
        stock_deriv = master[
            exch_mask
            & type_s.str.contains("STK", na=False)
            & master[c_under_sym].notna()
            & master[c_under_id].notna()
        ].copy()

    stocks = {}
    for _, row in stock_deriv.iterrows():
        sym = str(row[c_under_sym]).strip().upper()
        if (
            not sym
            or sym in EXCLUDE_POST_SAMPLE_FNO
            or "NSETEST" in sym
        ):
            continue
        sid = str(row[c_under_id]).split(".")[0].strip()
        if sid and sid.lower() != "nan":
            stocks[sym] = sid

    # Historical auditor universe must be the pre-30-Sep set.
    if len(stocks) != 210:
        raise RuntimeError(
            f"Expected 210 pre-30-Sep F&O underlyings, discovered {len(stocks)}. "
            f"First symbols={sorted(stocks)[:20]}, excluded={sorted(EXCLUDE_POST_SAMPLE_FNO)}"
        )

    # Index discovery is name based across all INDEX rows.
    idx_mask = inst.eq("INDEX")
    if c_type:
        idx_mask |= master[c_type].astype(str).str.upper().str.contains("INDEX", na=False)
    idx = master[idx_mask].copy()
    name_cols = [x for x in (c_sym, c_display) if x]
    if not name_cols:
        raise RuntimeError("Dhan master has no usable index name column")

    candidates = []
    for _, row in idx.iterrows():
        sid = str(row[c_sec_id]).split(".")[0].strip()
        names = [str(row[x]).strip() for x in name_cols if pd.notna(row[x])]
        candidates.append((sid, names))

    def match_one(aliases: list[str]) -> tuple[str, str]:
        alias_norm = {norm(a) for a in aliases}
        exact = []
        for sid, names in candidates:
            for name in names:
                if norm(name) in alias_norm:
                    exact.append((sid, name))
        unique = {(sid, name) for sid, name in exact if sid and sid.lower() != "nan"}
        if not unique:
            tokens = [t for t in re.split(r"[^A-Z0-9]+", aliases[0].upper()) if t not in {"NIFTY","INDEX","AND"}]
            nearby = []
            for sid, names in candidates:
                for name in names:
                    score = sum(tok in name.upper() for tok in tokens)
                    if score:
                        nearby.append((score, sid, name))
            nearby = sorted(nearby, reverse=True)[:20]
            raise RuntimeError(
                f"Could not exactly map index aliases {aliases}; nearby candidates={nearby}"
            )
        if len({sid for sid, _ in unique}) != 1:
            raise RuntimeError(f"Ambiguous exact index mapping for {aliases}: {sorted(unique)}")
        sid, name = sorted(unique, key=lambda x: (len(norm(x[1])), norm(x[1]), x[0]))[0]
        return sid, name


    indices = {}
    nifty_sid, nifty_name = match_one(NIFTY_ALIASES)
    indices["NIFTY 50"] = {"security_id": nifty_sid, "matched_name": nifty_name}
    for canonical, aliases in SECTOR_INDEX_ALIASES.items():
        sid, matched = match_one(aliases)
        indices[canonical] = {"security_id": sid, "matched_name": matched}

    if len(indices) != 22:
        raise RuntimeError(f"Expected NIFTY + 21 sector indices, got {len(indices)}")
    return stocks, indices


def chunks(start_date: str, end_date: str, days: int = 60):
    start = pd.Timestamp(start_date + " 09:15:00")
    end_exclusive = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    cur = start
    while cur < end_exclusive:
        nxt = min(cur + pd.Timedelta(days=days), end_exclusive)
        yield cur, nxt
        cur = nxt


def request_chunk(
    security_id: str,
    exchange_segment: str,
    instrument: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    token: str,
    client_id: str,
) -> pd.DataFrame:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "interval": "1",
        "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    last = None
    for attempt in range(6):
        try:
            r = requests.post(INTRADAY_URL, headers=headers, json=payload, timeout=90)
            if r.status_code == 200:
                data = r.json()
                ts = data.get("timestamp") or []
                n = len(ts)
                if n == 0:
                    return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
                out = pd.DataFrame({
                    "timestamp": ts,
                    "open": data.get("open", [None]*n),
                    "high": data.get("high", [None]*n),
                    "low": data.get("low", [None]*n),
                    "close": data.get("close", [None]*n),
                    "volume": data.get("volume", [None]*n),
                })
                return out
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code in (400, 401, 403):
                raise last
        except Exception as exc:
            last = exc
        time.sleep(min(20, 1.5 ** attempt))
    raise RuntimeError(f"Dhan request failed after retries: {last}")


def normalize_candles(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df.empty:
        return df
    ts = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    out = df.copy()
    out["timestamp"] = ts.dt.tz_localize(None)
    out = out.drop_duplicates("timestamp").sort_values("timestamp")
    d = out["timestamp"].dt.date
    lo, hi = pd.Timestamp(start_date).date(), pd.Timestamp(end_date).date()
    out = out[(d >= lo) & (d <= hi)]
    tod = out["timestamp"].dt.time
    out = out[
        (tod >= dt.time(9, 15))
        & (tod <= dt.time(15, 30))
        & (out["timestamp"].dt.dayofweek < 5)
    ]
    out["trade_date"] = out["timestamp"].dt.date.astype(str)
    return out.reset_index(drop=True)


def fetch_instrument(
    name: str,
    security_id: str,
    exchange_segment: str,
    instrument: str,
    start_date: str,
    end_date: str,
    out_path: Path,
    token: str,
    client_id: str,
) -> dict:
    frames = []
    for a, b in chunks(start_date, end_date):
        frames.append(request_chunk(
            security_id, exchange_segment, instrument, a, b, token, client_id
        ))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    df = normalize_candles(df, start_date, end_date)
    if df.empty:
        raise RuntimeError(f"No historical candles for {name} / {security_id}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False, compression="zstd")
    return {
        "name": name,
        "security_id": security_id,
        "rows": int(len(df)),
        "trading_days": int(df["trade_date"].nunique()),
        "first_timestamp": str(df["timestamp"].min()),
        "last_timestamp": str(df["timestamp"].max()),
        "sha256": sha256_file(out_path),
        "path": str(out_path),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-03-02")
    ap.add_argument("--end", default="2026-09-25")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    token = os.getenv("DHAN_ACCESS_TOKEN")
    client_id = os.getenv("DHAN_CLIENT_ID")
    if not token or not client_id:
        raise SystemExit("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are required")

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    master = get_master()
    stocks, indices = discover_universe(master)

    universe = {
        "sample_start": args.start,
        "sample_end": args.end,
        "stock_count": len(stocks),
        "stocks": stocks,
        "excluded_post_sample_fno": sorted(EXCLUDE_POST_SAMPLE_FNO),
        "indices": indices,
    }
    (root / "universe.json").write_text(json.dumps(universe, indent=2), encoding="utf-8")

    # Probe validates secret/token/API with NIFTY and one stock without exposing secrets.
    if args.probe:
        probe_stock = sorted(stocks)[0]
        targets = [
            ("NIFTY 50", indices["NIFTY 50"]["security_id"], "IDX_I", "INDEX", root/"indices"/"NIFTY_50.parquet"),
            (probe_stock, stocks[probe_stock], "NSE_EQ", "EQUITY", root/"stocks"/f"{probe_stock}.parquet"),
        ]
    else:
        targets = [
            (sym, sid, "NSE_EQ", "EQUITY", root/"stocks"/f"{sym}.parquet")
            for sym, sid in sorted(stocks.items())
        ]
        targets += [
            (name, info["security_id"], "IDX_I", "INDEX", root/"indices"/(norm(name)+".parquet"))
            for name, info in indices.items()
        ]

    records = []
    failures = []
    def work(t):
        name,sid,seg,inst,path = t
        return fetch_instrument(name,sid,seg,inst,args.start,args.end,path,token,client_id)

    with cf.ThreadPoolExecutor(max_workers=max(1,args.workers)) as ex:
        futs = {ex.submit(work,t): t for t in targets}
        for fut in cf.as_completed(futs):
            t = futs[fut]
            try:
                rec = fut.result()
                records.append(rec)
                print(f"OK {rec['name']} rows={rec['rows']} days={rec['trading_days']}", flush=True)
            except Exception as exc:
                failures.append({"name": t[0], "security_id": t[1], "error": str(exc)})
                print(f"FAIL {t[0]}: {exc}", flush=True)

    manifest = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "probe": bool(args.probe),
        "records": sorted(records, key=lambda x: x["name"]),
        "failures": failures,
    }
    (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if failures:
        raise SystemExit(f"{len(failures)} Dhan downloads failed; see download_manifest.json")
    print(json.dumps({
        "status":"PASS",
        "probe":args.probe,
        "files":len(records),
        "stock_universe":len(stocks),
        "index_universe":len(indices),
    }, indent=2))


if __name__ == "__main__":
    main()
