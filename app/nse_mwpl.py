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
import xml.etree.ElementTree as ET
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



def _xml_records_to_rows(text: str) -> dict[str, dict]:
    """Parse legacy NSE NCL OI XML using normalized tag names.

    Historical NSE specifications published ``nseoi_DDMMYYYY.xml`` with
    Date/ISIN/Scrip Name/NSE Symbol/MWPL/NSE Open Interest fields.  The
    exact container/record element names have varied, so parsing is based on
    the stable field tags rather than a brittle XPath.
    """
    try:
        root = ET.fromstring(text)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for elem in root.iter():
        children = list(elem)
        if not children:
            continue
        row = {_norm_col(child.tag.split("}")[-1]): (child.text or "").strip() for child in children}
        symbol = row.get("nsesymbol") or row.get("symbol") or row.get("tradingsymbol")
        mwpl = _numeric(row.get("mwpl") or row.get("marketwidepositionlimit"))
        oi = _numeric(row.get("openinterest") or row.get("nseopeninterest") or row.get("marketwideopeninterest") or row.get("combinedopeninterest"))
        symbol = str(symbol or "").strip().upper()
        if not symbol or not np.isfinite(mwpl) or mwpl <= 0 or not np.isfinite(oi) or oi < 0:
            continue
        out[symbol] = {
            "mwpl": float(mwpl),
            "open_interest": float(oi),
            "mwpl_pct": float(oi / mwpl * 100.0),
        }
    return out


def parse_combined_oi_payload(content: bytes | str) -> dict[str, dict]:
    """Parse current/legacy combined-OI payloads across CSV, ZIP and XML."""
    payload = content
    if isinstance(payload, bytes):
        raw = payload
        if raw[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                    names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt", ".xml"))]
                    if not names:
                        return {}
                    name = max(names, key=lambda n: zf.getinfo(n).file_size)
                    raw = zf.read(name)
            except (zipfile.BadZipFile, KeyError):
                return {}
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = str(payload)
    stripped = text.lstrip()
    if stripped.startswith("<"):
        rows = _xml_records_to_rows(text)
        if rows:
            return rows
    return parse_combined_oi_csv(text)



def parse_monthly_mwpl_csv(content: bytes | str) -> dict[str, float]:
    """Parse legacy NSE monthly ``mpl_monyyyy.csv`` MWPL master.

    NSE's historical F&O post-trade specification defines two stable fields:
    ``UNDERLYING_NAME`` and ``MWPL (MonthYYYY)``.  Newer exports have also
    used Symbol/MWPL-like headings, so matching is normalization based.
    Values are share-equivalent market-wide position limits.
    """
    if isinstance(content, bytes):
        payload = content
        if payload[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
                    names=[n for n in zf.namelist() if n.lower().endswith((".csv",".txt"))]
                    if not names: return {}
                    payload=zf.read(max(names,key=lambda n: zf.getinfo(n).file_size))
            except Exception:
                return {}
        text=payload.decode("utf-8-sig",errors="replace")
    else:
        text=str(content)
    if not text.strip(): return {}
    try:
        df=pd.read_csv(io.StringIO(text))
    except Exception:
        return {}
    if df.empty or len(df.columns)<2: return {}
    cols={_norm_col(c):c for c in df.columns}
    sym_col=(cols.get("underlyingname") or cols.get("nsesymbol") or cols.get("symbol") or cols.get("underlying"))
    mwpl_col=None
    for norm,raw in cols.items():
        if norm=="mwpl" or norm.startswith("mwpl") or "marketwidepositionlimit" in norm:
            mwpl_col=raw; break
    if sym_col is None or mwpl_col is None: return {}
    out={}
    for _,row in df.iterrows():
        symbol=str(row.get(sym_col,"")).strip().upper()
        mwpl=_numeric(row.get(mwpl_col))
        if symbol and symbol not in {"NAN","UNDERLYING_NAME"} and np.isfinite(mwpl) and mwpl>0:
            out[symbol]=float(mwpl)
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
        self._legacy_route_hints: dict[str, str] = {}
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
                "Accept": "text/csv,application/xml,text/xml,application/json,text/plain,*/*",
                "Accept-Language": "en-GB,en;q=0.8",
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

    def _fetch_direct_legacy(self, day, report_key: str) -> bytes:
        d = self._day(day)
        token = d.strftime("%d%m%Y")
        # Legacy MWPL/NCL OI was published as daily ZIP files under
        # /archives/nsccl/mwpl/ (for example nseoi_DDMMYYYY.zip and
        # combineoi_DDMMYYYY.zip).  Probe that real archive family first;
        # the older CSV/XML locations remain as compatibility fallbacks.
        filenames = [f"{report_key}_{token}.zip", f"{report_key}_{token}.csv", f"{report_key}_{token}.xml"]
        bases = [
            "https://nsearchives.nseindia.com/archives/nsccl/mwpl",
            "https://www.nseindia.com/archives/nsccl/mwpl",
            "https://archives.nseindia.com/archives/nsccl/mwpl",
            "https://nsearchives.nseindia.com/content/nsccl",
            "https://nsearchives.nseindia.com/archives/nsccl",
            "https://nsearchives.nseindia.com/archives/fo",
            "https://archives.nseindia.com/content/nsccl",
            "https://archives.nseindia.com/archives/nsccl",
            "https://archives.nseindia.com/archives/fo",
        ]
        candidates = [f"{base}/{name}" for base in bases for name in filenames]
        hint = self._legacy_route_hints.get(report_key)
        if hint:
            hinted = hint.format(token=token)
            candidates = [hinted] + [u for u in candidates if u != hinted]
        errors = []
        for url in candidates:
            try:
                resp = self.session.get(url, timeout=self.timeout)
                status = int(getattr(resp, "status_code", 200) or 200)
                if status == 404:
                    errors.append(f"{url}:404")
                    continue
                if hasattr(resp, "raise_for_status"):
                    resp.raise_for_status()
                content = bytes(getattr(resp, "content", b"") or b"")
                if content.strip():
                    # Remember the route shape after the first success so a
                    # multi-year build makes one request per date, not a probe
                    # storm across obsolete archive locations.
                    self._legacy_route_hints[report_key] = url.replace(token, "{token}")
                    path = self._cache_path(report_key, d)
                    if path is not None:
                        tmp = path.with_suffix(path.suffix + ".tmp")
                        tmp.write_bytes(content); tmp.replace(path)
                    return content
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}:{exc}")
        raise FileNotFoundError(" | ".join(errors))

    def _monthly_cache_path(self, month) -> Path | None:
        if self.cache_dir is None:
            return None
        p=pd.Timestamp(month).to_period("M")
        return self.cache_dir / f"mpl_{p.year:04d}{p.month:02d}.csv"

    def fetch_monthly_mwpl(self, month) -> dict[str, float]:
        """Fetch one official NSE monthly MWPL master, not daily OI.

        Historical specifications name the file ``mpl_monyyyy.csv``.  The
        direct archive family is preferred for 2018-2021; report-API probing
        is retained as a compatibility fallback.  A successful route shape is
        remembered for the remaining months.
        """
        period=pd.Timestamp(month).to_period("M")
        path=self._monthly_cache_path(period.start_time)
        if path is not None and path.exists() and path.stat().st_size>0:
            rows=parse_monthly_mwpl_csv(path.read_bytes())
            if rows: return rows
        token=pd.Timestamp(period.start_time).strftime("%b%Y").lower()
        filenames=[f"mpl_{token}.csv",f"mpl_{token}.zip"]
        bases=[
            "https://nsearchives.nseindia.com/content/nsccl",
            "https://nsearchives.nseindia.com/archives/nsccl",
            "https://nsearchives.nseindia.com/archives/nsccl/mwpl",
            "https://nsearchives.nseindia.com/archives/fo",
            "https://archives.nseindia.com/content/nsccl",
            "https://archives.nseindia.com/archives/nsccl",
        ]
        candidates=[f"{b}/{f}" for b in bases for f in filenames]
        hint=self._legacy_route_hints.get("mpl_monthly")
        if hint:
            h=hint.format(token=token); candidates=[h]+[u for u in candidates if u!=h]
        errors=[]
        for url in candidates:
            try:
                resp=self.session.get(url,timeout=min(self.timeout,5))
                status=int(getattr(resp,"status_code",200) or 200)
                if status==404: continue
                if hasattr(resp,"raise_for_status"): resp.raise_for_status()
                content=bytes(getattr(resp,"content",b"") or b"")
                rows=parse_monthly_mwpl_csv(content)
                if rows:
                    self._legacy_route_hints["mpl_monthly"]=url.replace(token,"{token}")
                    if path is not None:
                        tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_bytes(content); tmp.replace(path)
                    return rows
                errors.append(f"{url}:unparseable")
            except Exception as exc:
                errors.append(f"{url}:{exc}")
        # Modern historical-reports API fallback; one request per month only.
        for report_name in ("F&O - Market wide Position Limits","Market wide Position Limits"):
            try:
                content=self._fetch_report(report_name,period.start_time,report_key=f"mpl_{period.year:04d}{period.month:02d}")
                rows=parse_monthly_mwpl_csv(content)
                if rows: return rows
            except Exception as exc:
                errors.append(f"api:{report_name}:{exc}")
        raise FileNotFoundError(" | ".join(errors))

    def fetch_combined_oi(self, day) -> dict[str, dict]:
        errors = []
        d = self._day(day)
        # Before 2024 the NCL ``nseoi`` archive is the canonical legacy
        # family, so try it before combined-OI.  This avoids a large storm of
        # dead probes on every historical date.
        if d < pd.Timestamp("2024-01-01"):
            report_pairs = [(NCL_OI_REPORT, "nseoi"), (COMBINED_OI_REPORT, "combineoi"), (NCL_OI_REPORT, "ncloi")]
        else:
            report_pairs = [(COMBINED_OI_REPORT, "combineoi"), (NCL_OI_REPORT, "nseoi"), (NCL_OI_REPORT, "ncloi")]
        # Once an old archive route is discovered, prefer that report family
        # on subsequent dates. This turns a multi-year history run from many
        # failed probes per day into a single cached-route request.
        report_pairs.sort(key=lambda pair: 0 if pair[1] in self._legacy_route_hints else 1)
        legacy_first = d < pd.Timestamp("2024-01-01")
        for report_name, report_key in report_pairs:
            loaders = ("direct", "api") if legacy_first else ("api", "direct")
            for loader in loaders:
                try:
                    if loader == "direct":
                        content = self._fetch_direct_legacy(day, report_key)
                    else:
                        content = self._fetch_report(report_name, day, report_key=report_key)
                    rows = parse_combined_oi_payload(content)
                    if rows:
                        return rows
                    errors.append(f"{report_key}:{loader}:unparseable")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{report_key}:{loader}:{exc}")
        raise ValueError(f"NSE MWPL/OI report unavailable for {d.date()}: {' | '.join(errors)}")

    def fetch_secban(self, day) -> set[str]:
        try:
            content = self._fetch_report(SECBAN_REPORT, day, report_key="secban")
            return parse_secban_csv(content)
        except Exception:
            d = self._day(day)
            token = d.strftime("%d%m%Y")
            candidates = [
                f"https://nsearchives.nseindia.com/content/nsccl/fo_secban_{token}.csv",
                f"https://nsearchives.nseindia.com/archives/nsccl/fo_secban_{token}.csv",
                f"https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{token}.csv",
                f"https://nsearchives.nseindia.com/archives/fo/fo_secban_{token}.csv",
                f"https://archives.nseindia.com/content/nsccl/fo_secban_{token}.csv",
                f"https://archives.nseindia.com/archives/nsccl/fo_secban_{token}.csv",
                f"https://archives.nseindia.com/archives/fo/sec_ban/fo_secban_{token}.csv",
            ]
            for url in candidates:
                try:
                    resp = self.session.get(url, timeout=self.timeout)
                    status = int(getattr(resp, "status_code", 200) or 200)
                    if status == 404:
                        continue
                    if hasattr(resp, "raise_for_status"):
                        resp.raise_for_status()
                    content = bytes(getattr(resp, "content", b"") or b"")
                    if content.strip():
                        return parse_secban_csv(content)
                except Exception:
                    continue
            # The 95/80 MWPL state machine remains the primary historical ban
            # derivation. Missing first-day secban is disclosed, never invented.
            return set()


def build_monthly_mwpl_controls(*, validation_dates: Iterable, symbols: Iterable[str], total_oi_by_symbol: dict, client,
                                min_date_coverage: float = 0.95, progress_cb=None) -> dict:
    """Reconstruct daily MWPL utilisation from monthly limits + daily FUTSTK OI.

    This is the historical 2018-2021 path.  It intentionally avoids fetching
    one MWPL/OI file per trading date.  The monthly master supplies the limit;
    Trial-19's already-normalized total FUTSTK OI supplies the numerator.
    Daily secban files are queried only for dates that can plausibly be in a
    ban state (>=80% utilisation or a derived 95/80 state), and override the
    state-machine flag when they explicitly list a symbol.
    """
    dates=pd.DatetimeIndex(sorted(set(pd.to_datetime(list(validation_dates)).normalize())))
    symbols=[str(s).upper() for s in symbols]
    if not len(dates):
        return {"available":False,"reason":"NO_VALIDATION_DATES","date_coverage":0.0,"month_coverage":0.0,"mwpl_by_symbol":{},"ban_by_symbol":{},"source":"NSE_MONTHLY_MWPL_PLUS_RECONSTRUCTED_TOTAL_FUTSTK_OI"}
    periods=sorted(set(dates.to_period("M")))
    monthly={}; errors={}
    if progress_cb: progress_cb(0,len(periods),str(periods[0]))
    for i,p in enumerate(periods,start=1):
        try:
            rows=client.fetch_monthly_mwpl(p.start_time)
            if rows: monthly[p]=rows
        except Exception as exc:
            errors[f"mpl:{p}"]=str(exc)
        finally:
            if progress_cb: progress_cb(i,len(periods),str(p))
    month_coverage=float(len(monthly)/len(periods)) if periods else 0.0
    mwpl_by_symbol={}; ban_by_symbol={}
    valid_obs=0; total_obs=0
    for symbol in symbols:
        raw=total_oi_by_symbol.get(symbol)
        if isinstance(raw,pd.Series):
            oi=pd.to_numeric(raw.copy(),errors="coerce"); oi.index=pd.to_datetime(oi.index).normalize(); oi=oi.reindex(dates)
        else:
            oi=pd.Series(np.nan,index=dates,dtype=float)
        vals=[]
        for d,v in oi.items():
            if np.isfinite(v): total_obs+=1
            limit=(monthly.get(pd.Timestamp(d).to_period("M")) or {}).get(symbol)
            if np.isfinite(v) and limit is not None and np.isfinite(limit) and float(limit)>0:
                vals.append(float(v)/float(limit)*100.0); valid_obs+=1
            else:
                vals.append(np.nan)
        mw=pd.Series(vals,index=dates,dtype=float,name="mwpl_pct")
        mwpl_by_symbol[symbol]=mw
        ban_by_symbol[symbol]=derive_ban_flags(mw,initially_banned=False)
    date_valid=[]
    for d in dates:
        date_valid.append(any(np.isfinite(mwpl_by_symbol[s].get(d,np.nan)) for s in symbols))
    date_coverage=float(sum(date_valid)/len(dates))
    observation_coverage=float(valid_obs/total_obs) if total_obs else 0.0

    # Authoritative ban-list cross-check only where a ban can plausibly exist.
    risk_dates=[]
    for d in dates:
        risky=False
        for s in symbols:
            pct=mwpl_by_symbol[s].get(d,np.nan)
            if (np.isfinite(pct) and pct>=80.0) or bool(ban_by_symbol[s].get(d,False)):
                risky=True; break
        if risky: risk_dates.append(d)
    secban_loaded=0
    for d in risk_dates:
        try:
            listed=set(client.fetch_secban(d.date())) if hasattr(client,"fetch_secban") else set()
            secban_loaded+=1
            if listed:
                for s in listed:
                    if s in ban_by_symbol: ban_by_symbol[s].loc[d]=True
        except Exception as exc:
            errors[f"secban:{d.date()}"]=str(exc)
    available=bool(month_coverage>=0.95 and date_coverage>=float(min_date_coverage) and observation_coverage>=float(min_date_coverage))
    reason="APPLIED" if available else f"INSUFFICIENT_MONTHLY_MWPL_COVERAGE:months={month_coverage:.1%},dates={date_coverage:.1%},observations={observation_coverage:.1%}"
    return {
        "available":available,"reason":reason,"date_coverage":date_coverage,"month_coverage":month_coverage,"observation_coverage":observation_coverage,
        "months_requested":len(periods),"months_loaded":len(monthly),"dates_requested":len(dates),
        "mwpl_by_symbol":mwpl_by_symbol,"ban_by_symbol":ban_by_symbol,"errors":errors,
        "source":"NSE_MONTHLY_MWPL_PLUS_RECONSTRUCTED_TOTAL_FUTSTK_OI",
        "ban_source":"95_80_RECONSTRUCTED_PLUS_TARGETED_NSE_SECBAN",
        "secban_dates_requested":len(risk_dates),"secban_dates_loaded":secban_loaded,
        "secban_risk_date_coverage":float(secban_loaded/len(risk_dates)) if risk_dates else 1.0,
    }


def build_validation_mwpl_controls(*, validation_dates: Iterable, symbols: Iterable[str], client,
                                   min_date_coverage: float = 0.95, progress_cb=None) -> dict:
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
    total_dates = int(len(dates))
    if progress_cb:
        progress_cb(0, total_dates, str(dates[0].date()))
    for i, d in enumerate(dates, start=1):
        try:
            rows = client.fetch_combined_oi(d.date())
            if rows:
                snapshots[d] = rows
        except Exception as exc:  # noqa: BLE001
            errors[str(d.date())] = str(exc)
        finally:
            if progress_cb:
                progress_cb(i, total_dates, str(d.date()))

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
