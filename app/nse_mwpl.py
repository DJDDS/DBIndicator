"""Official NSE historical MWPL / ban controls for V9.5 research.

The loader uses NSE's Historical Reports endpoint for the Combined Open
Interest report.  The report publishes symbol-level MWPL and aggregate open
interest across exchanges.  Ban state is initialized from NSE's Security in
ban period report and then advanced using the published 95% entry / 80% exit
rules.  Missing or partial history is never imputed as a clean observation.
"""
from __future__ import annotations

import io
import json
import zipfile
import logging
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
NSE_REPORTS_URL = f"{NSE_BASE}/api/reports"
COMBINED_OI_REPORT = "F&O - Combine Open Interest across exchanges"
SECBAN_REPORT = "F&O - Security in ban period"
NCL_OI_REPORT = "F&O - NCL Open Interest"
DEFAULT_TIMEOUT = 20


def _norm_col(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _numeric(value):
    if value is None:
        return np.nan
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
    return pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]


def parse_combined_oi_csv(content: bytes | str) -> dict[str, dict]:
    """Parse NSE combineoi CSV into {symbol: MWPL snapshot}.

    Format-tolerant across the pre/post Oct-2025 additions; only the stable
    NSE Symbol, MWPL and Open Interest fields are required.
    """
    if isinstance(content, bytes):
        payload = content
        if payload[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
                    names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
                    if not names:
                        return {}
                    name = max(names, key=lambda n: zf.getinfo(n).file_size)
                    payload = zf.read(name)
            except (zipfile.BadZipFile, KeyError):
                return {}
        text = payload.decode("utf-8-sig", errors="replace")
    else:
        text = str(content)
    if not text.strip():
        return {}
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        return {}
    if df.empty:
        return {}
    cols = {_norm_col(c): c for c in df.columns}
    sym_col = cols.get("nsesymbol") or cols.get("symbol") or cols.get("tradingsymbol")
    mwpl_col = cols.get("mwpl") or cols.get("marketwidepositionlimit")
    oi_col = cols.get("openinterest") or cols.get("nseopeninterest") or cols.get("marketwideopeninterest") or cols.get("combinedopeninterest")
    if not sym_col or not mwpl_col or not oi_col:
        return {}

    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        symbol = str(row.get(sym_col, "")).strip().upper()
        mwpl = _numeric(row.get(mwpl_col))
        oi = _numeric(row.get(oi_col))
        if not symbol or not np.isfinite(mwpl) or mwpl <= 0 or not np.isfinite(oi) or oi < 0:
            continue
        out[symbol] = {
            "mwpl": float(mwpl),
            "open_interest": float(oi),
            "mwpl_pct": float(oi / mwpl * 100.0),
        }
    return out


def parse_secban_csv(content: bytes | str) -> set[str]:
    """Return symbols listed by NSE as being in the F&O ban period."""
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = str(content)
    if not text.strip():
        return set()
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        return set()
    if df.empty:
        return set()
    cols = {_norm_col(c): c for c in df.columns}
    sym_col = cols.get("symbol") or cols.get("nsesymbol") or cols.get("securityinban")
    if sym_col is None:
        # Historical files have also appeared as a single unnamed/list column.
        sym_col = df.columns[-1]
    values = set()
    for value in df[sym_col].dropna().astype(str):
        s = value.strip().upper()
        if s and s not in {"SYMBOL", "SECURITYINBAN", "NAN"}:
            values.add(s)
    return values


def derive_ban_flags(mwpl_pct: pd.Series, *, initially_banned: bool = False) -> pd.Series:
    """Derive trade-date ban status from EOD MWPL utilisation.

    NSE applies a new ban from the next trade date after EOD utilisation
    exceeds 95%.  Once in ban, normal trading resumes only after EOD
    utilisation is <=80%, again from the next trade date.
    """
    pct = pd.to_numeric(pd.Series(mwpl_pct).copy(), errors="coerce").sort_index()
    banned = bool(initially_banned)
    flags = []
    for _, value in pct.items():
        flags.append(banned)
        if not np.isfinite(value):
            continue
        if banned:
            if value <= 80.0:
                banned = False
        elif value > 95.0:
            banned = True
    return pd.Series(flags, index=pct.index, dtype=bool, name="ban_flag")


class NSEHistoricalReportClient:
    """Small cached client for official NSE historical derivative reports."""

    def __init__(self, *, session=None, cache_dir=None, timeout=DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.timeout = int(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._warmed = False
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "text/csv,application/json,text/plain,*/*",
                "Referer": f"{NSE_BASE}/all-reports-derivatives",
            })
        except Exception:
            pass

    @staticmethod
    def _day(value) -> pd.Timestamp:
        return pd.Timestamp(value).normalize()

    def _cache_path(self, report_key: str, day) -> Path | None:
        if self.cache_dir is None:
            return None
        d = self._day(day)
        return self.cache_dir / f"{report_key}_{d:%Y%m%d}.csv"

    def _warmup(self):
        if self._warmed:
            return
        resp = self.session.get(NSE_BASE + "/", timeout=self.timeout)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        self._warmed = True

    def _report_params(self, report_name: str, day) -> dict:
        d = self._day(day)
        archives = [{
            "name": report_name,
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

    def _fetch_report(self, report_name: str, day, *, report_key: str) -> bytes:
        path = self._cache_path(report_key, day)
        if path is not None and path.exists() and path.stat().st_size > 0:
            return path.read_bytes()
        self._warmup()
        resp = self.session.get(
            NSE_REPORTS_URL,
            params=self._report_params(report_name, day),
            timeout=self.timeout,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        content = bytes(getattr(resp, "content", b"") or b"")
        # NSE sometimes answers a missing historical file with JSON.  Do not
        # turn that into an empty/clean market state.
        ctype = str(getattr(resp, "headers", {}).get("content-type", "")).lower()
        if "json" in ctype or content.lstrip().startswith((b"{", b"[")):
            try:
                payload = resp.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                url = payload.get("url") or payload.get("downloadUrl") or payload.get("download_url")
                if url:
                    r2 = self.session.get(url, timeout=self.timeout)
                    if hasattr(r2, "raise_for_status"):
                        r2.raise_for_status()
                    content = bytes(getattr(r2, "content", b"") or b"")
                else:
                    raise FileNotFoundError(f"NSE report unavailable for {self._day(day).date()}: {payload}")
            else:
                raise FileNotFoundError(f"NSE report unavailable for {self._day(day).date()}")
        if not content.strip():
            raise FileNotFoundError(f"Empty NSE report for {self._day(day).date()}")
        if path is not None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        return content

    def fetch_combined_oi(self, day) -> dict[str, dict]:
        errors = []
        for report_name, report_key in ((COMBINED_OI_REPORT, "combineoi"), (NCL_OI_REPORT, "nseoi")):
            try:
                content = self._fetch_report(report_name, day, report_key=report_key)
                rows = parse_combined_oi_csv(content)
                if rows:
                    return rows
                errors.append(f"{report_key}:unparseable")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{report_key}:{exc}")
        raise ValueError(f"NSE MWPL/OI report unavailable for {self._day(day).date()}: {' | '.join(errors)}")

    def fetch_secban(self, day) -> set[str]:
        try:
            content = self._fetch_report(SECBAN_REPORT, day, report_key="secban")
        except FileNotFoundError:
            # A no-ban trading date may legitimately have no populated file.
            return set()
        return parse_secban_csv(content)


def build_validation_mwpl_controls(*, validation_dates: Iterable, symbols: Iterable[str], client,
                                   min_date_coverage: float = 0.95) -> dict:
    """Load MWPL only for the pre-declared validation window.

    This intentionally never fetches or inspects the locked final 20%.
    ``available`` becomes true only when official report coverage is high
    enough; sparse history remains a disclosed missing control.
    """
    dates = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(validation_dates)).normalize())))
    symbols = [str(s).upper() for s in symbols]
    if len(dates) == 0:
        return {
            "available": False, "reason": "NO_VALIDATION_DATES", "date_coverage": 0.0,
            "mwpl_by_symbol": {}, "ban_by_symbol": {}, "source": "NSE_COMBINED_OI",
        }

    snapshots: dict[pd.Timestamp, dict] = {}
    errors: dict[str, str] = {}
    for d in dates:
        try:
            rows = client.fetch_combined_oi(d.date())
            if rows:
                snapshots[d] = rows
        except Exception as exc:  # noqa: BLE001
            errors[str(d.date())] = str(exc)

    date_coverage = float(len(snapshots) / len(dates))
    mwpl_by_symbol: dict[str, pd.Series] = {}
    ban_by_symbol: dict[str, pd.Series] = {}

    initial_banned = set()
    try:
        initial_banned = set(client.fetch_secban(dates[0].date())) if hasattr(client, "fetch_secban") else set()
    except Exception as exc:  # noqa: BLE001
        errors[f"secban:{dates[0].date()}"] = str(exc)

    for symbol in symbols:
        vals = []
        for d in dates:
            row = snapshots.get(d, {}).get(symbol)
            vals.append(float(row["mwpl_pct"]) if row and np.isfinite(row.get("mwpl_pct", np.nan)) else np.nan)
        mw = pd.Series(vals, index=dates, dtype=float, name="mwpl_pct")
        mwpl_by_symbol[symbol] = mw
        ban_by_symbol[symbol] = derive_ban_flags(mw, initially_banned=(symbol in initial_banned))

    available = bool(date_coverage >= float(min_date_coverage))
    reason = "APPLIED" if available else f"INSUFFICIENT_MWPL_DATE_COVERAGE:{date_coverage:.1%}"
    return {
        "available": available,
        "reason": reason,
        "date_coverage": date_coverage,
        "dates_requested": int(len(dates)),
        "dates_loaded": int(len(snapshots)),
        "mwpl_by_symbol": mwpl_by_symbol,
        "ban_by_symbol": ban_by_symbol,
        "errors": errors,
        "source": "NSE_F&O_COMBINED_OPEN_INTEREST",
        "ban_source": "NSE_SECBAN_INITIAL_STATE_PLUS_95_80_RULE",
    }
