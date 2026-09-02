"""Official NSE India-VIX and NIFTY-50 history for V9.6.2 promotion controls."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import requests

NSE_BASE = "https://www.nseindia.com"
VIX_URL = f"{NSE_BASE}/api/historical/vixhistory"
INDEX_URL = f"{NSE_BASE}/api/historical/indicesHistory"
DEFAULT_TIMEOUT = 25


def _walk_rows(obj):
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
            yield from _walk_rows(item)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_rows(value)


def _pick(row, keys):
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _parse_series(payload, *, date_keys, close_keys) -> pd.Series:
    vals = {}
    for row in _walk_rows(payload):
        dv = _pick(row, date_keys); cv = _pick(row, close_keys)
        if dv is None or cv is None:
            continue
        dt = pd.to_datetime(str(dv), errors="coerce", dayfirst=True)
        close = pd.to_numeric(pd.Series([str(cv).replace(",", "")]), errors="coerce").iloc[0]
        if pd.isna(dt) or not np.isfinite(close):
            continue
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.tz_localize(None)
        vals[pd.Timestamp(dt).normalize()] = float(close)
    return pd.Series(vals, dtype=float).sort_index()


def parse_vix_payload(payload) -> pd.Series:
    return _parse_series(payload, date_keys=["EOD_TIMESTAMP", "date", "timestamp"], close_keys=["EOD_CLOSE_INDEX_VAL", "close", "close_price"])


def parse_index_payload(payload) -> pd.Series:
    return _parse_series(payload, date_keys=["EOD_TIMESTAMP", "HistoricalDate", "CH_TIMESTAMP", "date"], close_keys=["EOD_CLOSE_INDEX_VAL", "CLOSE", "CH_CLOSING_VALUE", "close_price"])


class NSEMarketRegimeClient:
    def __init__(self, *, session=None, cache_dir=None, timeout=DEFAULT_TIMEOUT):
        self.session = session or requests.Session(); self.timeout = int(timeout); self._warmed = False
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None: self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/json,text/plain,*/*", "Referer": f"{NSE_BASE}/all-reports",
            })
        except Exception:
            pass

    def _warm(self):
        if self._warmed: return
        try: self.session.get(NSE_BASE + "/", timeout=self.timeout)
        except Exception: pass
        self._warmed = True

    def _cache(self, kind, start, end):
        if self.cache_dir is None: return None
        return self.cache_dir / f"{kind}_{pd.Timestamp(start):%Y%m%d}_{pd.Timestamp(end):%Y%m%d}.json"

    def _request(self, kind, start, end):
        path = self._cache(kind, start, end)
        if path is not None and path.exists() and path.stat().st_size > 0:
            try: return json.loads(path.read_text(encoding="utf-8"))
            except Exception: pass
        self._warm()
        url = VIX_URL if kind == "vix" else INDEX_URL
        params = {"from": pd.Timestamp(start).strftime("%d-%m-%Y"), "to": pd.Timestamp(end).strftime("%d-%m-%Y")}
        if kind == "nifty": params["indexType"] = "NIFTY 50"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if hasattr(resp, "raise_for_status"): resp.raise_for_status()
        payload = resp.json() if hasattr(resp, "json") else json.loads(bytes(resp.content).decode("utf-8"))
        if path is not None:
            tmp = path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(payload), encoding="utf-8"); tmp.replace(path)
        return payload

    def fetch(self, start, end) -> pd.DataFrame:
        start = pd.Timestamp(start).normalize(); end = pd.Timestamp(end).normalize()
        chunks=[]; cur=start
        while cur <= end:
            stop=min(end, cur + pd.Timedelta(days=364)); chunks.append((cur,stop)); cur=stop + pd.Timedelta(days=1)
        vix_parts=[]; nifty_parts=[]
        for a,b in chunks:
            vix_parts.append(parse_vix_payload(self._request("vix", a, b)))
            nifty_parts.append(parse_index_payload(self._request("nifty", a, b)))
        vix=pd.concat(vix_parts).groupby(level=0).last().sort_index() if vix_parts else pd.Series(dtype=float)
        nifty=pd.concat(nifty_parts).groupby(level=0).last().sort_index() if nifty_parts else pd.Series(dtype=float)
        idx=pd.DatetimeIndex(sorted(set(vix.index) | set(nifty.index)))
        out=pd.DataFrame(index=idx)
        out["india_vix"] = vix.reindex(idx)
        out["nifty_close"] = nifty.reindex(idx)
        ret=np.log(out["nifty_close"] / out["nifty_close"].shift(1))
        out["nifty_rv20_prev"] = ret.rolling(20, min_periods=15).std(ddof=1).shift(1) * math.sqrt(252.0)
        target=pd.bdate_range(start,end)
        both=pd.DataFrame({"v":vix.reindex(target),"n":nifty.reindex(target)}).notna().all(axis=1)
        out.attrs["coverage"]={
            "business_days_requested": int(len(target)),
            "both_series_days": int(both.sum()),
            "event_date_coverage": float(both.mean()) if len(target) else 0.0,
            "source": "NSE_HISTORICAL_VIX_AND_NIFTY50",
        }
        return out
