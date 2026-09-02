"""Official NSE historical cash-market daily prices for V9.6 integrity.

V9.6.1 uses the legacy CM bhavcopy for the fixed 2021-2023 Trial-17
window so point-in-time F&O members that later left the current universe do
not require a current Kite instrument token. Missing exchange holidays are
not treated as corrupt data; network/parse failures are.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests

NSE_ARCHIVES = "https://nsearchives.nseindia.com"
DEFAULT_TIMEOUT = 25
_COLUMNS = ["date", "symbol", "series", "open", "high", "low", "close", "source_format"]


def _empty_day() -> pd.DataFrame:
    out = pd.DataFrame(columns=_COLUMNS)
    out["date"] = pd.to_datetime(out["date"])
    return out


def parse_legacy_cm_bhavcopy(content: bytes | str, trade_date) -> pd.DataFrame:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = str(content)
    if not text.strip():
        return _empty_day()
    raw = pd.read_csv(io.StringIO(text))
    raw.columns = [str(c).strip().upper() for c in raw.columns]
    required = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE"}
    if not required.issubset(raw.columns):
        raise ValueError("legacy NSE CM bhavcopy missing required OHLC columns")
    raw = raw[raw["SERIES"].astype(str).str.strip().str.upper().eq("EQ")].copy()
    if raw.empty:
        return _empty_day()
    out = pd.DataFrame({
        "date": pd.Timestamp(trade_date).normalize(),
        "symbol": raw["SYMBOL"].astype(str).str.strip().str.upper(),
        "series": "EQ",
        "open": pd.to_numeric(raw["OPEN"], errors="coerce"),
        "high": pd.to_numeric(raw["HIGH"], errors="coerce"),
        "low": pd.to_numeric(raw["LOW"], errors="coerce"),
        "close": pd.to_numeric(raw["CLOSE"], errors="coerce"),
        "source_format": "LEGACY_CM_BHAVCOPY",
    })
    out = out[out[["open", "high", "low", "close"]].notna().all(axis=1)].copy()
    return out.reset_index(drop=True)


class NSECashArchiveClient:
    def __init__(self, *, session=None, cache_dir=None, timeout=DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.timeout = int(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/zip,text/csv,*/*"})
        except Exception:
            pass

    @staticmethod
    def _day(day) -> pd.Timestamp:
        return pd.Timestamp(day).normalize()

    def _cache_path(self, day) -> Path | None:
        if self.cache_dir is None:
            return None
        d = self._day(day)
        return self.cache_dir / f"cm_{d:%Y%m%d}.zip"

    def _url(self, day) -> str:
        d = self._day(day)
        mon = d.strftime("%b").upper()
        return f"{NSE_ARCHIVES}/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"

    def fetch_day(self, day) -> pd.DataFrame:
        d = self._day(day)
        path = self._cache_path(d)
        payload = None
        if path is not None and path.exists() and path.stat().st_size > 0:
            payload = path.read_bytes()
        else:
            resp = self.session.get(self._url(d), timeout=self.timeout)
            status = int(getattr(resp, "status_code", 200) or 200)
            if status == 404:
                raise FileNotFoundError(f"NSE CM bhavcopy not found for {d.date()}")
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            payload = bytes(getattr(resp, "content", b"") or b"")
            if not payload:
                raise FileNotFoundError(f"Empty NSE CM bhavcopy for {d.date()}")
            if path is not None:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(payload)
                tmp.replace(path)
        if payload[:2] != b"PK":
            raise ValueError(f"NSE CM bhavcopy for {d.date()} is not a valid zip")
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise ValueError("zip contains no CSV")
                name = max(names, key=lambda n: zf.getinfo(n).file_size)
                csv = zf.read(name)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"NSE CM bhavcopy for {d.date()} is not a valid zip") from exc
        return parse_legacy_cm_bhavcopy(csv, d)


def build_symbol_price_histories(days: Iterable, symbols: Iterable[str], client, progress_cb=None) -> dict:
    dates = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(days)).normalize())))
    symbols = [str(s).strip().upper() for s in symbols]
    fields = ["open", "high", "low", "close"]
    out = {s: pd.DataFrame(index=dates, columns=fields, dtype=float) for s in symbols}
    wanted = set(symbols)
    loaded = not_found = hard_errors = 0
    errors = {}
    for i, d in enumerate(dates, start=1):
        if progress_cb:
            progress_cb(i - 1, len(dates), str(d.date()))
        try:
            frame = client.fetch_day(d.date())
            if frame is None or frame.empty:
                hard_errors += 1
                errors[str(d.date())] = "EMPTY_ARCHIVE"
                continue
            loaded += 1
            frame = frame[frame["symbol"].isin(wanted)].copy()
            for _, row in frame.iterrows():
                symbol = str(row["symbol"]).upper()
                for field in fields:
                    out[symbol].loc[d, field] = pd.to_numeric(pd.Series([row.get(field)]), errors="coerce").iloc[0]
        except FileNotFoundError as exc:
            not_found += 1
            errors[str(d.date())] = f"NOT_FOUND:{exc}"
        except Exception as exc:  # noqa: BLE001
            hard_errors += 1
            errors[str(d.date())] = str(exc)
        finally:
            if progress_cb:
                progress_cb(i, len(dates), str(d.date()))
    denom = loaded + hard_errors
    out["_meta"] = {
        "dates_requested": int(len(dates)),
        "dates_loaded": int(loaded),
        "dates_not_found": int(not_found),
        "hard_error_days": int(hard_errors),
        "calendar_hit_rate": float(loaded / len(dates)) if len(dates) else 0.0,
        "date_coverage": float(loaded / denom) if denom else 0.0,
        "errors": errors,
        "source": "NSE_OFFICIAL_CM_LEGACY_BHAVCOPY",
    }
    return out
