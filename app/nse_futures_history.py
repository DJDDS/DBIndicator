"""Official NSE daily stock-futures history for V9.5 evidence research.

This module deliberately stays independent of Kite.  It reads the official NSE
Equity Derivatives daily bhavcopy in both generations used by the 3+ year
research window:

* legacy ``foDDMMMYYYYbhav.csv.zip`` before 2024-07-08;
* UDiFF ``BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`` from 2024-07-08.

Only stock futures are retained.  The normalized frame is contract-wise and
keeps the exchange-reported OI unchanged; aggregation across near/next/far
expiries happens separately so the roll cycle is visible instead of stitched
away.  Missing archives never become zero OI or false membership.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

UDIFF_START = pd.Timestamp("2024-07-08")
ARCHIVE_BASE = "https://nsearchives.nseindia.com"
NSE_BASE = "https://www.nseindia.com"
NSE_REPORTS_URL = f"{NSE_BASE}/api/reports"
MARKET_ACTIVITY_REPORT = "F&O - Market Activity Report"
DEFAULT_TIMEOUT = 25

_NORMAL_COLUMNS = [
    "date", "symbol", "expiry", "open", "high", "low", "close", "settle",
    "open_interest", "oi_contracts", "oi_share_equivalent", "change_oi", "volume", "turnover_notional", "lot_size", "source_format",
]


def _as_text(content: bytes | str) -> str:
    if isinstance(content, bytes):
        return content.decode("utf-8-sig", errors="replace")
    return str(content)


def _num(series) -> pd.Series:
    return pd.to_numeric(pd.Series(series), errors="coerce")


def _empty_contract_frame() -> pd.DataFrame:
    out = pd.DataFrame(columns=_NORMAL_COLUMNS)
    out["date"] = pd.to_datetime(out["date"])
    out["expiry"] = pd.to_datetime(out["expiry"])
    return out


def parse_legacy_fo_bhavcopy(content: bytes | str, trade_date) -> pd.DataFrame:
    """Parse the pre-UDiFF NSE FO bhavcopy and retain FUTSTK rows only."""
    text = _as_text(content)
    if not text.strip():
        return _empty_contract_frame()
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("legacy NSE F&O bhavcopy is not parseable CSV") from exc
    raw.columns = [str(c).strip().upper() for c in raw.columns]
    required = {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "OPEN_INT"}
    if not required.issubset(set(raw.columns)):
        raise ValueError("legacy NSE F&O bhavcopy missing required columns")
    raw = raw[raw["INSTRUMENT"].astype(str).str.strip().str.upper().eq("FUTSTK")].copy()
    if raw.empty:
        return _empty_contract_frame()

    def col(name, default=np.nan):
        return raw[name] if name in raw.columns else pd.Series(default, index=raw.index)

    date_values = col("TIMESTAMP", pd.Timestamp(trade_date))
    date_parsed = pd.to_datetime(date_values, errors="coerce", dayfirst=True, format="mixed")
    date_parsed = date_parsed.fillna(pd.Timestamp(trade_date))
    close = _num(col("CLOSE"))
    settle = _num(col("SETTLE_PR"))
    contracts = _num(col("CONTRACTS"))
    turnover_lakh = _num(col("VAL_INLAKH"))
    reference_price = settle.where(settle > 0, close)
    # Legacy NSE FO bhavcopy reports OPEN_INT in underlying quantity.  The
    # historical file does not carry lot size, but futures turnover does:
    #   value(lakh) = contracts * lot * price / 100000.
    # Infer lot only when there was traded volume; otherwise leave it missing.
    inferred_lot = (turnover_lakh * 100000.0) / (contracts * reference_price)
    inferred_lot = inferred_lot.where((contracts > 0) & (reference_price > 0) & np.isfinite(inferred_lot))
    inferred_lot = inferred_lot.round().where(inferred_lot.round() >= 1)
    reported_oi = _num(col("OPEN_INT"))
    oi_contracts = reported_oi / inferred_lot
    out = pd.DataFrame({
        "date": date_parsed.dt.normalize(),
        "symbol": raw["SYMBOL"].astype(str).str.strip().str.upper(),
        "expiry": pd.to_datetime(raw["EXPIRY_DT"], errors="coerce", dayfirst=True, format="mixed").dt.normalize(),
        "open": _num(col("OPEN")),
        "high": _num(col("HIGH")),
        "low": _num(col("LOW")),
        "close": close,
        "settle": settle,
        "open_interest": reported_oi,
        "oi_contracts": oi_contracts,
        "oi_share_equivalent": reported_oi,
        "change_oi": _num(col("CHG_IN_OI")),
        "volume": contracts,
        "turnover_notional": turnover_lakh * 100000.0,
        "lot_size": inferred_lot,
        "source_format": "LEGACY_FO_BHAVCOPY",
    })
    out = out[out["symbol"].ne("") & out["expiry"].notna() & out["open_interest"].notna()]
    return out.reset_index(drop=True)


def parse_udiff_fo_bhavcopy(content: bytes | str, trade_date) -> pd.DataFrame:
    """Parse the NSE UDiFF FO bhavcopy and retain stock-futures rows.

    NSE's UDiFF code for stock futures is ``STF``.  ``FUTSTK`` is accepted as
    a defensive alias because some historical exports/tools expose the long
    instrument label rather than the ISO-style code.
    """
    text = _as_text(content)
    if not text.strip():
        return _empty_contract_frame()
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("UDiFF NSE F&O bhavcopy is not parseable CSV") from exc
    cols = {str(c).strip().lower(): c for c in raw.columns}

    def cname(*names):
        for name in names:
            c = cols.get(str(name).lower())
            if c is not None:
                return c
        return None

    inst = cname("FinInstrmTp", "INSTRUMENT")
    symbol = cname("TckrSymb", "SYMBOL")
    expiry = cname("FininstrmActlXpryDt", "XpryDt", "EXPIRY_DT")
    oi = cname("OpnIntrst", "OPEN_INT")
    if not all((inst, symbol, expiry, oi)):
        raise ValueError("UDiFF NSE F&O bhavcopy missing required columns")
    inst_values = raw[inst].astype(str).str.strip().str.upper()
    raw = raw[inst_values.isin({"STF", "FUTSTK"})].copy()
    if raw.empty:
        return _empty_contract_frame()

    def values(*names, default=np.nan):
        c = cname(*names)
        return raw[c] if c is not None else pd.Series(default, index=raw.index)

    date_col = cname("TradDt", "BizDt")
    if date_col is not None:
        date_parsed = pd.to_datetime(raw[date_col], errors="coerce")
        date_parsed = date_parsed.fillna(pd.Timestamp(trade_date))
    else:
        date_parsed = pd.Series(pd.Timestamp(trade_date), index=raw.index)
    reported_oi = _num(raw[oi])
    lot_size = _num(values("NewBrdLotQty"))
    # NSE's UDiFF schema labels TtlTradgVol as the lots field, while
    # OpnIntrst is the contract open-interest quantity.  Keep OpnIntrst in
    # underlying/share-equivalent units (consistent with the legacy bhavcopy
    # and Market Activity FOD report) and derive a contracts diagnostic from
    # the published board lot.  Multiplying OpnIntrst by lot would double
    # apply the lot-size conversion and manufacture huge OI shocks.
    share_equiv = reported_oi
    oi_contracts = reported_oi / lot_size
    traded_lots = _num(values("TtlTradgVol", "CONTRACTS"))
    reference_price = _num(values("SttlmPric", "SETTLE_PR")).where(lambda x: x > 0, _num(values("ClsPric", "CLOSE")))
    computed_turnover = traded_lots * lot_size * reference_price
    reported_turnover = _num(values("TtlTrfVal", "Traded Value"))
    turnover_notional = computed_turnover.where(computed_turnover.notna() & (computed_turnover >= 0), reported_turnover)
    out = pd.DataFrame({
        "date": pd.to_datetime(date_parsed).dt.normalize(),
        "symbol": raw[symbol].astype(str).str.strip().str.upper(),
        "expiry": pd.to_datetime(raw[expiry], errors="coerce").dt.normalize(),
        "open": _num(values("OpnPric", "OPEN")),
        "high": _num(values("HghPric", "HIGH")),
        "low": _num(values("LwPric", "LOW")),
        "close": _num(values("ClsPric", "CLOSE")),
        "settle": _num(values("SttlmPric", "SETTLE_PR")),
        "open_interest": reported_oi,
        "oi_contracts": oi_contracts,
        "oi_share_equivalent": share_equiv,
        "change_oi": _num(values("ChngInOpnIntrst", "CHG_IN_OI")),
        "volume": traded_lots,
        "turnover_notional": turnover_notional,
        "lot_size": lot_size,
        "source_format": "UDIFF_FO_BHAVCOPY",
    })
    out = out[out["symbol"].ne("") & out["expiry"].notna() & out["open_interest"].notna()]
    return out.reset_index(drop=True)



def _norm_col(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def parse_market_activity_futures_csv(content: bytes | str, trade_date) -> pd.DataFrame:
    """Parse NSE Market Activity ``FODDMMYYYY.CSV`` contract-wise futures.

    NSE documents the market-activity file as contract-wise futures with
    instrument, symbol, expiry, OHLC, open interest, traded quantity and
    contract count.  Its open-interest field is a quantity measure.  We keep
    that quantity as the share-equivalent research series and infer the
    historical market lot from traded quantity / contracts when a contract
    traded that day.
    """
    text = _as_text(content)
    if not text.strip():
        return _empty_contract_frame()
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("NSE Market Activity futures CSV is not parseable") from exc
    cols = {_norm_col(c): c for c in raw.columns}

    def cname(*names):
        for name in names:
            c = cols.get(_norm_col(name))
            if c is not None:
                return c
        return None

    inst = cname("Instrument")
    symbol = cname("Symbol")
    expiry = cname("Expiry Date", "Expiry")
    oi = cname("Open Interest")
    if not all((inst, symbol, expiry, oi)):
        raise ValueError("NSE Market Activity futures CSV missing required columns")
    raw = raw[raw[inst].astype(str).str.strip().str.upper().eq("FUTSTK")].copy()
    if raw.empty:
        return _empty_contract_frame()

    def values(*names, default=np.nan):
        c = cname(*names)
        return raw[c] if c is not None else pd.Series(default, index=raw.index)

    traded_qty = _num(values("Traded Quantity"))
    contracts = _num(values("No of Contracts", "No. of Contracts"))
    lot_size = (traded_qty / contracts).where((traded_qty > 0) & (contracts > 0))
    lot_size = lot_size.round().where(lot_size.round() >= 1)
    reported_oi = _num(raw[oi])
    oi_contracts = reported_oi / lot_size
    reference_price = _num(values("Close Price", "Close"))
    computed_turnover = traded_qty * reference_price
    reported_turnover = _num(values("Traded Value"))
    turnover_notional = computed_turnover.where(computed_turnover.notna() & (computed_turnover >= 0), reported_turnover)
    out = pd.DataFrame({
        "date": pd.Series(pd.Timestamp(trade_date).normalize(), index=raw.index),
        "symbol": raw[symbol].astype(str).str.strip().str.upper(),
        "expiry": pd.to_datetime(raw[expiry], errors="coerce", dayfirst=True).dt.normalize(),
        "open": _num(values("Open Price", "Open")),
        "high": _num(values("High Price", "High")),
        "low": _num(values("Low Price", "Low")),
        "close": _num(values("Close Price", "Close")),
        "settle": _num(values("Close Price", "Close")),
        "open_interest": reported_oi,
        "oi_contracts": oi_contracts,
        "oi_share_equivalent": reported_oi,
        "change_oi": pd.Series(np.nan, index=raw.index, dtype=float),
        "volume": contracts,
        "turnover_notional": turnover_notional,
        "lot_size": lot_size,
        "source_format": "NSE_MARKET_ACTIVITY_FOD",
    })
    out = out[out["symbol"].ne("") & out["expiry"].notna() & out["open_interest"].notna()]
    return out.reset_index(drop=True)

def _extract_single_csv(zip_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError("zip has no CSV")
            # Official daily archive contains one final bhavcopy CSV.  If extra
            # metadata appears later, prefer the largest CSV as the data file.
            name = max(names, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(name).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ValueError("response is not a valid NSE F&O bhavcopy zip") from exc


def _extract_market_activity_fod(zip_bytes: bytes, day) -> str:
    """Extract the contract-wise futures CSV from an NSE Market Activity ZIP."""
    d = pd.Timestamp(day).normalize()
    expected = f"fo{d:%d%m%Y}.csv".lower()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError("zip has no CSV")
            exact = [n for n in names if Path(n).name.lower() == expected]
            if not exact:
                # Defensive compatibility with older/nested exports whose
                # contract-wise futures file still starts with FO and embeds
                # the trade date.  Never select OP/OPT files.
                token = d.strftime("%d%m%Y")
                exact = [n for n in names if Path(n).name.lower().startswith("fo") and token in Path(n).name and not Path(n).name.lower().startswith(("fut", "fo_"))]
            if not exact:
                raise ValueError(f"Market Activity ZIP missing {expected}")
            name = max(exact, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(name).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ValueError("response is not a valid NSE F&O Market Activity zip") from exc


class NSEFuturesArchiveClient:
    """Cached official-NSE daily futures downloader.

    Prefer the compact Market Activity contract-wise futures report and fall
    back to the full FO bhavcopy/UDiFF archive when the report is unavailable.
    """

    def __init__(self, *, session=None, cache_dir=None, timeout=DEFAULT_TIMEOUT, prefer_market_activity=True):
        self.session = session or requests.Session()
        self.timeout = int(timeout)
        self.prefer_market_activity = bool(prefer_market_activity)
        self._warmed = False
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/134 Safari/537.36",
                "Accept": "application/zip,application/json,application/octet-stream,*/*",
                "Referer": "https://www.nseindia.com/all-reports-derivatives",
            })
        except Exception:
            pass

    @staticmethod
    def _day(value) -> pd.Timestamp:
        return pd.Timestamp(value).normalize()

    def _warmup(self):
        if self._warmed:
            return
        resp = self.session.get(NSE_BASE + "/", timeout=self.timeout)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        self._warmed = True

    def _report_params(self, day) -> dict:
        d = self._day(day)
        archives = [{
            "name": MARKET_ACTIVITY_REPORT,
            "type": "archives",
            "category": "derivatives",
            "section": "equity",
        }]
        return {
            "archives": json.dumps(archives, separators=(",", ":")),
            "date": d.strftime("%d-%b-%Y"),
            "type": "equity",
            "mode": "single",
        }

    def _market_cache_path(self, day) -> Path | None:
        if self.cache_dir is None:
            return None
        d = self._day(day)
        return self.cache_dir / f"fo_market_activity_{d:%Y%m%d}.zip"

    def _fetch_market_activity(self, day) -> bytes:
        d = self._day(day)
        path = self._market_cache_path(d)
        if path is not None and path.exists() and path.stat().st_size > 0:
            payload = path.read_bytes()
            _extract_market_activity_fod(payload, d)
            return payload
        self._warmup()
        resp = self.session.get(NSE_REPORTS_URL, params=self._report_params(d), timeout=self.timeout)
        status = int(getattr(resp, "status_code", 200) or 200)
        if status == 404:
            raise FileNotFoundError(f"NSE Market Activity report unavailable for {d.date()}")
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        payload = bytes(getattr(resp, "content", b"") or b"")
        ctype = str(getattr(resp, "headers", {}).get("content-type", "")).lower()
        if "json" in ctype or payload.lstrip().startswith((b"{", b"[")):
            try:
                data = resp.json()
            except Exception:
                data = None
            url = data.get("url") or data.get("downloadUrl") or data.get("download_url") if isinstance(data, dict) else None
            if not url:
                raise FileNotFoundError(f"NSE Market Activity report unavailable for {d.date()}")
            r2 = self.session.get(url, timeout=self.timeout)
            if hasattr(r2, "raise_for_status"):
                r2.raise_for_status()
            payload = bytes(getattr(r2, "content", b"") or b"")
        _extract_market_activity_fod(payload, d)
        if path is not None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(path)
        return payload

    def _url(self, day) -> tuple[str, str]:
        d = self._day(day)
        if d >= UDIFF_START:
            filename = f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
            return f"{ARCHIVE_BASE}/content/fo/{filename}", "udiff"
        filename = f"fo{d:%d%b%Y}bhav.csv.zip".upper()
        # The historical path uses upper-case month but lower-case 'fo'/'bhav'.
        filename = "fo" + d.strftime("%d%b%Y").upper() + "bhav.csv.zip"
        return f"{ARCHIVE_BASE}/content/historical/DERIVATIVES/{d:%Y}/{d:%b}/{filename}".replace(d.strftime("%b"), d.strftime("%b").upper()), "legacy"

    def _cache_path(self, day) -> Path | None:
        if self.cache_dir is None:
            return None
        d = self._day(day)
        return self.cache_dir / f"fo_bhavcopy_{d:%Y%m%d}.zip"

    def fetch_day(self, day) -> pd.DataFrame:
        d = self._day(day)
        if self.prefer_market_activity:
            try:
                payload = self._fetch_market_activity(d)
                csv_text = _extract_market_activity_fod(payload, d)
                frame = parse_market_activity_futures_csv(csv_text, d)
                if not frame.empty:
                    return frame
                raise ValueError(f"NSE Market Activity report had no FUTSTK rows for {d.date()}")
            except Exception as exc:  # noqa: BLE001
                # The compact report is an optimization, not a single point of
                # failure.  Fall back to the official daily bhavcopy generation.
                log.warning("NSE Market Activity fetch failed for %s; falling back to bhavcopy: %s", d.date(), exc)

        path = self._cache_path(d)
        url, fmt = self._url(d)
        if path is not None and path.exists() and path.stat().st_size > 0:
            payload = path.read_bytes()
        else:
            resp = self.session.get(url, timeout=self.timeout)
            status = int(getattr(resp, "status_code", 200) or 200)
            if status == 404:
                raise FileNotFoundError(f"NSE F&O bhavcopy unavailable for {d.date()}")
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            payload = bytes(getattr(resp, "content", b"") or b"")
            _extract_single_csv(payload)
            if path is not None:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(payload)
                tmp.replace(path)
        csv_text = _extract_single_csv(payload)
        if fmt == "legacy":
            return parse_legacy_fo_bhavcopy(csv_text, d)
        return parse_udiff_fo_bhavcopy(csv_text, d)


def _empty_history(index: pd.DatetimeIndex) -> dict:
    return {
        "total_oi": pd.Series(np.nan, index=index, dtype=float),
        "near_oi": pd.Series(np.nan, index=index, dtype=float),
        "next_oi": pd.Series(np.nan, index=index, dtype=float),
        "far_oi": pd.Series(np.nan, index=index, dtype=float),
        "membership": pd.Series(False, index=index, dtype=bool),
        "near_expiry": pd.Series(pd.NaT, index=index, dtype="datetime64[ns]"),
        "next_expiry": pd.Series(pd.NaT, index=index, dtype="datetime64[ns]"),
        "near_dte": pd.Series(np.nan, index=index, dtype=float),
        "next_dte": pd.Series(np.nan, index=index, dtype=float),
        "near_settle": pd.Series(np.nan, index=index, dtype=float),
        "next_settle": pd.Series(np.nan, index=index, dtype=float),
        "lot_size": pd.Series(np.nan, index=index, dtype=float),
        "total_volume": pd.Series(np.nan, index=index, dtype=float),
        "near_volume": pd.Series(np.nan, index=index, dtype=float),
        "total_turnover_notional": pd.Series(np.nan, index=index, dtype=float),
        "near_turnover_notional": pd.Series(np.nan, index=index, dtype=float),
        "source_format": pd.Series(None, index=index, dtype=object),
    }


def build_symbol_histories(days: Iterable, symbols: Iterable[str], client, progress_cb=None, discover_historical: bool = False) -> dict:
    """Stream daily contract files into point-in-time per-symbol OI histories.

    ``membership`` is true only on dates where the official daily bhavcopy
    actually contains a stock-futures contract for that symbol.  Missing dates
    are never forward-filled as membership or zero OI.
    """
    dates = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(days)).normalize())))
    symbols = [str(s).strip().upper() for s in symbols]
    out = {s: _empty_history(dates) for s in symbols}
    wanted = set(symbols)
    loaded = 0
    not_found = 0
    hard_errors = 0
    errors = {}
    formats = set()

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
            if discover_historical:
                frame = frame.copy()
                for discovered in frame["symbol"].dropna().astype(str).str.strip().str.upper().unique():
                    if discovered and discovered not in out:
                        out[discovered] = _empty_history(dates)
            else:
                frame = frame[frame["symbol"].isin(wanted)].copy()
            if frame.empty:
                continue
            formats.update(str(x) for x in frame["source_format"].dropna().unique())
            for symbol, grp in frame.groupby("symbol", sort=False):
                grp = grp.sort_values("expiry")
                payload = out[symbol]
                payload["membership"].loc[d] = True
                oi_source = grp["oi_share_equivalent"] if "oi_share_equivalent" in grp.columns and pd.to_numeric(grp["oi_share_equivalent"], errors="coerce").notna().any() else grp["open_interest"]
                oi = pd.to_numeric(oi_source, errors="coerce")
                payload["total_oi"].loc[d] = float(oi.sum(min_count=1)) if oi.notna().any() else np.nan
                expiries = list(grp["expiry"])
                oivals = list(oi)
                price_source = pd.to_numeric(grp.get("settle"), errors="coerce") if "settle" in grp.columns else pd.Series(np.nan, index=grp.index)
                if price_source.isna().all() and "close" in grp.columns:
                    price_source = pd.to_numeric(grp.get("close"), errors="coerce")
                prices = list(price_source)
                if len(oivals) >= 1:
                    payload["near_oi"].loc[d] = oivals[0]
                    payload["near_expiry"].loc[d] = pd.Timestamp(expiries[0]).normalize()
                    payload["near_dte"].loc[d] = int((pd.Timestamp(expiries[0]).normalize() - d).days)
                    if len(prices) >= 1 and pd.notna(prices[0]):
                        payload["near_settle"].loc[d] = float(prices[0])
                if len(oivals) >= 2:
                    payload["next_oi"].loc[d] = oivals[1]
                    payload["next_expiry"].loc[d] = pd.Timestamp(expiries[1]).normalize()
                    payload["next_dte"].loc[d] = int((pd.Timestamp(expiries[1]).normalize() - d).days)
                    if len(prices) >= 2 and pd.notna(prices[1]):
                        payload["next_settle"].loc[d] = float(prices[1])
                if len(oivals) >= 3:
                    payload["far_oi"].loc[d] = float(pd.Series(oivals[2:]).sum(min_count=1))
                lots = pd.to_numeric(grp.get("lot_size"), errors="coerce") if "lot_size" in grp else pd.Series(dtype=float)
                if len(lots) and lots.notna().any():
                    payload["lot_size"].loc[d] = float(lots.dropna().iloc[0])
                if "volume" in grp.columns:
                    vols = pd.to_numeric(grp["volume"], errors="coerce")
                    lot_for_vol = pd.to_numeric(grp.get("lot_size"), errors="coerce") if "lot_size" in grp.columns else pd.Series(np.nan, index=grp.index)
                    share_vol = (vols * lot_for_vol).where(vols.notna() & lot_for_vol.notna())
                    if share_vol.notna().any():
                        payload["total_volume"].loc[d] = float(share_vol.sum(min_count=1))
                        payload["near_volume"].loc[d] = float(share_vol.iloc[0]) if pd.notna(share_vol.iloc[0]) else np.nan
                if "turnover_notional" in grp.columns:
                    turns = pd.to_numeric(grp["turnover_notional"], errors="coerce")
                    if turns.notna().any():
                        payload["total_turnover_notional"].loc[d] = float(turns.sum(min_count=1))
                        payload["near_turnover_notional"].loc[d] = float(turns.iloc[0]) if pd.notna(turns.iloc[0]) else np.nan
                payload["source_format"].loc[d] = "+".join(sorted(set(str(x) for x in grp["source_format"].dropna())))
        except FileNotFoundError as exc:
            # Business-day calendars include exchange holidays.  A genuine
            # 404/missing archive is not evidence corruption and therefore is
            # excluded from the archive-integrity denominator.
            not_found += 1
            errors[str(d.date())] = f"NOT_FOUND:{exc}"
        except Exception as exc:  # noqa: BLE001
            hard_errors += 1
            errors[str(d.date())] = str(exc)
        finally:
            if progress_cb:
                progress_cb(i, len(dates), str(d.date()))

    integrity_denominator = loaded + hard_errors
    out["_meta"] = {
        "dates_requested": int(len(dates)),
        "dates_loaded": int(loaded),
        "dates_not_found": int(not_found),
        "hard_error_days": int(hard_errors),
        "calendar_hit_rate": float(loaded / len(dates)) if len(dates) else 0.0,
        "date_coverage": float(loaded / integrity_denominator) if integrity_denominator else 0.0,
        "errors": errors,
        "source_formats": sorted(formats),
        "source": "NSE_OFFICIAL_FO_MARKET_ACTIVITY_WITH_BHAVCOPY_FALLBACK",
        "historical_symbols_discovered": int(len([k for k in out if k != "_meta"])),
    }
    return out
