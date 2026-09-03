"""Official NSE financial-result filing dates for V9.6.2 promotion controls.

Only the filing/broadcast calendar is used. Financial numbers are not parsed.
Missing fetches fail closed; no inferred quarterly calendar is fabricated.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

NSE_BASE = "https://www.nseindia.com"
FIN_RESULTS_URL = f"{NSE_BASE}/api/corporates-financial-results"
BOARD_MEETINGS_URL = f"{NSE_BASE}/api/corporate-board-meetings"
DEFAULT_TIMEOUT = 25


def _to_date(value):
    if value is None or str(value).strip() in {"", "-", "None", "nan"}:
        return None
    try:
        ts = pd.to_datetime(str(value).strip(), errors="coerce", dayfirst=True)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_localize(None)
    return pd.Timestamp(ts).normalize()


def parse_financial_result_rows(payload, *, symbol=None) -> pd.DatetimeIndex:
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("records") or payload.get("results") or []
    else:
        rows = payload or []
    want = str(symbol).strip().upper() if symbol else None
    dates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rsym = str(row.get("symbol") or row.get("sm_name") or row.get("companyName") or "").strip().upper()
        if want and rsym and rsym != want:
            continue
        value = (
            row.get("broadcastDate") or row.get("broadCastDate") or row.get("broadcast_date")
            or row.get("filingDate") or row.get("filing_date") or row.get("date")
        )
        d = _to_date(value)
        if d is not None:
            dates.add(d)
    return pd.DatetimeIndex(sorted(dates))


def parse_board_meeting_rows(payload, *, symbol=None) -> pd.DatetimeIndex:
    """Return meeting dates explicitly tied to financial-result purposes."""
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("records") or payload.get("results") or []
    else:
        rows = payload or []
    want = str(symbol).strip().upper() if symbol else None
    dates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rsym = str(row.get("symbol") or row.get("bm_symbol") or row.get("sm_name") or "").strip().upper()
        if want and rsym and rsym != want:
            continue
        purpose = str(row.get("purpose") or row.get("bm_purpose") or row.get("details") or "").strip().lower()
        if not any(token in purpose for token in ("financial result", "quarterly result", "audited result", "unaudited result")):
            continue
        value = row.get("meetingDate") or row.get("meeting_date") or row.get("bm_date") or row.get("date")
        d = _to_date(value)
        if d is not None:
            dates.add(d)
    return pd.DatetimeIndex(sorted(dates))


class NSEEarningsHistoryClient:
    def __init__(self, *, session=None, cache_dir=None, timeout=DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout = int(timeout)
        self._warmed = False
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": f"{NSE_BASE}/companies-listing/corporate-filings-financial-results",
            })
        except Exception:
            pass

    def _warm(self):
        if self._warmed:
            return
        try:
            self.session.get(NSE_BASE + "/", timeout=self.timeout)
        except Exception:
            pass
        self._warmed = True

    def _path(self, symbol, start=None, end=None) -> Path | None:
        if self.cache_dir is None:
            return None
        if start is None or end is None:
            return self.cache_dir / f"earnings_{str(symbol).upper()}.json"
        a = pd.Timestamp(start).strftime("%Y%m%d"); b = pd.Timestamp(end).strftime("%Y%m%d")
        return self.cache_dir / f"boardmeet_{str(symbol).upper()}_{a}_{b}.json"

    def _fetch_board_rows(self, symbol: str, start, end):
        path = self._path(symbol, start, end)
        if path is not None and path.exists() and path.stat().st_size > 0:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._warm()
        resp = self.session.get(
            BOARD_MEETINGS_URL,
            params={
                "index": "equities",
                "symbol": str(symbol).upper(),
                "from_date": pd.Timestamp(start).strftime("%d-%m-%Y"),
                "to_date": pd.Timestamp(end).strftime("%d-%m-%Y"),
            },
            timeout=self.timeout,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        payload = resp.json() if hasattr(resp, "json") else json.loads(bytes(resp.content).decode("utf-8"))
        if path is not None:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        return payload

    def _fetch_financial_rows(self, symbol: str):
        path = self._path(symbol)
        if path is not None and path.exists() and path.stat().st_size > 0:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._warm()
        resp = self.session.get(FIN_RESULTS_URL, params={"index": "equities", "symbol": str(symbol).upper()}, timeout=self.timeout)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        payload = resp.json() if hasattr(resp, "json") else json.loads(bytes(resp.content).decode("utf-8"))
        if path is not None:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        return payload

    def fetch_symbol(self, symbol, start, end) -> pd.DatetimeIndex:
        start = pd.Timestamp(start).normalize(); end = pd.Timestamp(end).normalize(); symbol = str(symbol).upper()
        errors = []
        try:
            dates = parse_board_meeting_rows(self._fetch_board_rows(symbol, start, end), symbol=symbol)
            if len(dates):
                return dates[(dates >= start) & (dates <= end)]
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        # Compatibility fallback for symbols/periods where the board-meeting
        # endpoint does not return historical rows.  Still official NSE data.
        try:
            dates = parse_financial_result_rows(self._fetch_financial_rows(symbol), symbol=symbol)
            return dates[(dates >= start) & (dates <= end)]
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        raise RuntimeError("NSE earnings history unavailable: " + " | ".join(errors))


def build_earnings_map(symbols, start, end, client, progress_cb=None) -> dict:
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    out = {}; loaded = 0; loaded_symbols = []; symbols_with_dates = 0; result_dates_loaded = 0; errors = {}
    for i, symbol in enumerate(symbols, start=1):
        if progress_cb:
            progress_cb(i - 1, len(symbols), symbol)
        try:
            dates = client.fetch_symbol(symbol, start, end)
            dates = pd.DatetimeIndex(pd.to_datetime(dates, errors="coerce")).dropna().normalize().unique().sort_values()
            out[symbol] = dates
            loaded += 1
            loaded_symbols.append(symbol)
            if len(dates):
                symbols_with_dates += 1
                result_dates_loaded += int(len(dates))
        except Exception as exc:  # noqa: BLE001
            out[symbol] = pd.DatetimeIndex([])
            errors[symbol] = str(exc)
        finally:
            if progress_cb:
                progress_cb(i, len(symbols), symbol)
    out["_meta"] = {
        "symbols_requested": int(len(symbols)),
        "symbols_loaded": int(loaded),
        "symbol_coverage": float(loaded / len(symbols)) if symbols else 0.0,
        "symbols_with_dates": int(symbols_with_dates),
        "result_dates_loaded": int(result_dates_loaded),
        "symbol_date_coverage": float(symbols_with_dates / len(symbols)) if symbols else 0.0,
        "loaded_symbols": sorted(loaded_symbols),
        "errors": errors,
        "source": "NSE_CORPORATES_FINANCIAL_RESULTS",
    }
    return out
