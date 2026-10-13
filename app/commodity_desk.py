"""NG Winter Commodity Desk (ADMIN ONLY, spotting / paper - no orders are placed).

Weekly playbook researched on NYMEX NG front-month (Oct-Feb winters 2010-2026). Rules were chosen
on 2010-19 data only; 2020-26 was held out. All inputs are lagged to the moment they are public:

  MON close  CFTC open-interest 4-week change (Friday's report)  down -> CALL, up -> PUT   exit after 1 session
  WED close  3-day fall (r3s<0) OR last 7 days colder than normal -> PUT (into Thu EIA)    exit after 2 sessions
  FRI close  open-interest 4-week change  down -> PUT, up -> CALL (over the weekend)       exit after 1 session

Data is fetched server-side and cached under <storage>/commodity_desk:
  Yahoo NG=F daily OHLC, USDINR (INR=X), CFTC disaggregated COT (Socrata 72hh-3qpy, code 023651),
  Open-Meteo gas-weighted HDD (observed archive + 16-day forecast).
Every fetch is fail-soft: the page renders from the last good cache with the error shown.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

ROOT = Path(config.V12_STORAGE_ROOT) / "commodity_desk"
UA = {"User-Agent": "Mozilla/5.0 (DBIndicator commodity desk)"}
WINTER = (10, 11, 12, 1, 2)
CITIES = [  # lat, lon, weight - population/gas-heating demand proxy
    (41.88, -87.63, .16), (40.71, -74.01, .16), (39.95, -75.17, .07), (42.36, -71.06, .07), (42.33, -83.05, .09),
    (39.96, -83.00, .07), (44.98, -93.27, .06), (40.44, -79.99, .05), (33.75, -84.39, .05), (32.78, -96.80, .08),
    (29.76, -95.37, .04), (39.74, -104.99, .04), (34.05, -118.24, .06)]

DEFAULTS = dict(
    oi_weeks=4,          # CFTC open-interest lookback (weeks)
    cold_z=0.0,          # Wed leg: last-7-day HDD anomaly z above this counts as "cold"
    mon_hold=1, wed_hold=2, fri_hold=1,
    leg_mon=True, leg_wed=True, leg_fri=True,
    t1=0.5, t2=1.0, stop=1.0,   # in units of sigma_h = close * sigma_d * sqrt(hold)
    refresh_hours=6.0,
    # MCX layer
    mcx_symbol="NATURALGAS",   # or NATGASMINI
    mcx_roll_days=2,           # switch to the next future this many days before expiry
    opt_min_days=4,            # option expiry must be at least this many calendar days away
    max_spread_pct=3.0,        # suggested strike must have a bid/ask spread below this
    entry_minutes_before_close=60,   # MCX paper ledger records today's entry from this many minutes before the MCX close
    live_price_minutes=10,     # refresh the NYMEX intraday price on page load if older than this
)
MCX_SYMBOLS = ("NATURALGAS", "NATGASMINI")

_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_last_error: dict = {}


# ----------------------------------------------------------------------------- storage
def _p(name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT / name


def _atomic_text(name: str, text: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader never sees a half-written file."""
    f = _p(name); tmp = f.with_name(f.name + f".tmp{threading.get_ident()}")
    tmp.write_text(text); os.replace(tmp, f)


def _atomic_csv(name: str, df: pd.DataFrame) -> None:
    f = _p(name); tmp = f.with_name(f.name + f".tmp{threading.get_ident()}")
    df.to_csv(tmp, index=False); os.replace(tmp, f)


def load_settings() -> dict:
    try:
        s = json.loads(_p("settings.json").read_text())
        return {**DEFAULTS, **{k: s[k] for k in s if k in DEFAULTS}}
    except Exception:
        return dict(DEFAULTS)


def save_settings(new: dict) -> dict:
    cur = load_settings()
    for k, v in (new or {}).items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, str):
                v = str(v).strip().upper()
                if k == "mcx_symbol" and v not in MCX_SYMBOLS:
                    continue
                cur[k] = v
            else:
                cur[k] = bool(v) if isinstance(d, bool) else int(v) if isinstance(d, int) else float(v)
        except (TypeError, ValueError):
            continue
    cur["oi_weeks"] = min(max(cur["oi_weeks"], 1), 12)
    for k in ("mon_hold", "wed_hold", "fri_hold"):
        cur[k] = min(max(cur[k], 1), 3)
    _atomic_text("settings.json", json.dumps(cur, indent=1))
    return cur


# ----------------------------------------------------------------------------- fetchers (network)
def _yahoo(symbol: str, rng: str = "20y") -> pd.DataFrame:
    last = None
    for host in ("query1", "query2"):           # Yahoo occasionally rate-limits one host
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                             params={"range": rng, "interval": "1d"}, headers=UA, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            break
        except Exception as exc:  # pragma: no cover - network dependent
            last = exc
    else:
        raise last
    off = int(res.get("meta", {}).get("gmtoffset") or 0)
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"date": pd.to_datetime(np.array(res["timestamp"]) + off, unit="s").normalize(),
                       "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
                       "volume": q.get("volume")})
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    return df[df.date.dt.dayofweek < 5]


def _cot() -> pd.DataFrame:
    r = requests.get("https://publicreporting.cftc.gov/resource/72hh-3qpy.json", headers=UA, timeout=60,
                     params={"cftc_contract_market_code": "023651", "$select": "report_date_as_yyyy_mm_dd,open_interest_all",
                             "$order": "report_date_as_yyyy_mm_dd", "$limit": 50000})
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["date"] = pd.to_datetime(df.report_date_as_yyyy_mm_dd).dt.normalize()
    df["oi"] = df.open_interest_all.astype(float)
    return df[["date", "oi"]].drop_duplicates("date", keep="last")


def _om_json(url: str, params: dict, timeout: int) -> list:
    """Open-Meteo returns a list for multi-location requests, or {"error": true, "reason": ...}; surface the reason."""
    for attempt in range(3):                               # a gateway blip (502/503/504) or a dropped connection is retried; real errors are not
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
        except requests.RequestException:
            if attempt == 2: raise
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code in (502, 503, 504) and attempt < 2:
            time.sleep(2 * (attempt + 1)); continue
        break
    try:
        js = r.json()
    except ValueError:
        raise RuntimeError(f"Open-Meteo HTTP {r.status_code}: {r.text[:120]}")
    if isinstance(js, dict):
        raise RuntimeError(f"Open-Meteo: {js.get('reason') or js}"[:200])
    return js


def _city_hdd(js: list, key: str, field: str) -> pd.Series:
    t = pd.to_datetime(js[0][key]["time"])
    num = np.zeros(len(t)); den = np.zeros(len(t))
    for c, (_, _, w) in zip(js, CITIES):
        v = np.array([np.nan if x is None else x for x in c[key][field]], dtype=float)
        ok = ~np.isnan(v); num[ok] += w * np.maximum(65 - v[ok], 0); den[ok] += w
    return pd.Series(np.where(den > 0, num / np.maximum(den, 1e-9), np.nan), index=t)


def _hdd() -> tuple[pd.Series, pd.Series]:
    """Observed gas-weighted HDD (incremental: only the last 60 days once history is cached) + 16-day forecast."""
    lat = ",".join(str(c[0]) for c in CITIES); lon = ",".join(str(c[1]) for c in CITIES)
    base = dict(latitude=lat, longitude=lon, temperature_unit="fahrenheit", timezone="America/New_York", daily="temperature_2m_mean")
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    old = _read("hdd_obs.csv")
    old_s = old.set_index("date").hdd if old is not None and len(old) > 3000 else None
    start = (today - pd.Timedelta(days=60)) if old_s is not None else pd.Timestamp("2007-01-01")
    end = today - pd.Timedelta(days=6)
    parts = []
    for a in pd.date_range(start, end, freq="5YS").union([start]):     # history in <= 5-year chunks (keeps each request light)
        b = min(a + pd.DateOffset(years=5) - pd.Timedelta(days=1), end)
        if a > b:
            continue
        js = _om_json("https://archive-api.open-meteo.com/v1/archive",
                      {**base, "start_date": a.strftime("%Y-%m-%d"), "end_date": b.strftime("%Y-%m-%d")}, 120)
        parts.append(_city_hdd(js, "daily", "temperature_2m_mean"))
    fc = _om_json("https://api.open-meteo.com/v1/forecast", {**base, "past_days": 14, "forecast_days": 16}, 60)
    fcs = _city_hdd(fc, "daily", "temperature_2m_mean")
    obs = pd.concat(parts) if parts else pd.Series(dtype=float)
    if old_s is not None:
        obs = obs.combine_first(old_s)
    observed = obs.combine_first(fcs[fcs.index < today]).dropna().sort_index()
    observed = observed[~observed.index.duplicated(keep="first")]
    forecast = fcs[fcs.index >= today].dropna()
    return observed, forecast


_RS = {"running": False, "started": None, "finished": None, "result": None}
_SCHED = {"started": False}


def refresh(force: bool = False) -> dict:
    """Download all sources and cache them (state is tracked so the page can show running / result)."""
    _RS.update(running=True, started=datetime.now(timezone.utc).isoformat(timespec="seconds"), result=None)
    try:
        m = _refresh_impl(force); errs = m.get("errors") or {}
        _RS.update(result="ok" if not errs else "partial: " + ", ".join(errs))
        return m
    except Exception as exc:  # noqa: BLE001
        _RS.update(result="failed: " + str(exc)[:120]); raise
    finally:
        _RS.update(running=False, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def refresh_state() -> dict:
    return dict(_RS, running=bool(_RS["running"] or (_refresh_thread and _refresh_thread.is_alive())))


def _refresh_impl(force: bool = False) -> dict:
    """Download all sources and cache them. Each source is independent and fail-soft."""
    with _lock:
        errors, done = {}, []
        jobs = {
            "ng.csv": lambda: _yahoo("NG=F"),
            "usdinr.csv": lambda: _yahoo("INR=X", "2y"),
            "cot.csv": _cot,
        }
        for name, fn in jobs.items():
            try:
                df = fn()
                if len(df) < 10:
                    raise ValueError("too few rows")
                _atomic_csv(name, df); done.append(name)
            except Exception as exc:  # pragma: no cover - network dependent
                errors[name] = str(exc)[:200]
        try:
            obs, fc = _hdd()
            _atomic_csv("hdd_obs.csv", obs.rename("hdd").rename_axis("date").reset_index())
            _atomic_csv("hdd_fcst.csv", fc.rename("hdd").rename_axis("date").reset_index())
            done += ["hdd_obs.csv", "hdd_fcst.csv"]
        except Exception as exc:  # pragma: no cover - network dependent
            errors["hdd"] = str(exc)[:200]
        meta = {"refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": done, "errors": errors}
        _atomic_text("meta.json", json.dumps(meta, indent=1))
        _last_error.clear(); _last_error.update(errors)
        try:
            update_ledger()
        except Exception as exc:  # pragma: no cover
            log.warning("commodity_desk ledger update failed: %s", exc)
        try:
            ng_, cot_, hd_ = _read("ng.csv"), _read("cot.csv"), _read("hdd_obs.csv")
            if ng_ is not None and cot_ is not None and hd_ is not None:
                st_ = load_settings(); log_year_round(_settled(features(ng_, cot_, hd_.set_index("date").hdd, st_)), st_, (ng_, cot_, hd_.set_index("date").hdd))
        except Exception as exc:  # pragma: no cover
            log.warning("commodity_desk year-round study failed: %s", exc)
        return meta


def refresh_price(max_age_minutes: float) -> str | None:
    """Merge the last ~3 months of NG=F daily bars (incl. today's in-progress bar) into the cache if it is stale.
    Small, fast request so the page can show a provisional signal for today. Returns an error string or None."""
    f = _p("ng.csv")
    try:
        if f.exists() and (time.time() - f.stat().st_mtime) < max_age_minutes * 60:
            return None
        if not _lock.acquire(timeout=0.5):         # a full refresh is writing the cache right now - use what is there
            return None
        try:
            new = _yahoo("NG=F", "3mo")
            old = pd.read_csv(f, parse_dates=["date"]) if f.exists() else new.iloc[:0]
            merged = pd.concat([old[old.date < new.date.min()], new]).drop_duplicates("date", keep="last").sort_values("date")
            _atomic_csv("ng.csv", merged)
        finally:
            _lock.release()
        return None
    except Exception as exc:  # pragma: no cover - network dependent
        return str(exc)[:200]


def _meta() -> dict:
    try:
        return json.loads(_p("meta.json").read_text())
    except Exception:
        return {}


def refresh_async() -> bool:
    """Start a background refresh unless one is already running. Returns True if started."""
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive():
        return False
    _refresh_thread = threading.Thread(target=refresh, name="commodity-desk-refresh", daemon=True)
    _refresh_thread.start()
    return True


RETRY_AFTER_ERROR_MIN = 10


def maybe_refresh_async() -> None:
    """Kick a background refresh when the cache is older than settings.refresh_hours (non-blocking)."""
    m = _meta(); s = load_settings()
    try:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(m["refreshed_at"])).total_seconds() / 3600
    except Exception:
        age_h = 1e9
    if age_h >= s["refresh_hours"] or (m.get("errors") and age_h >= RETRY_AFTER_ERROR_MIN / 60):      # a source failed last time: try again soon, not after 6 h
        refresh_async()


# ----------------------------------------------------------------------------- model (pure, testable)
def _read(name: str) -> pd.DataFrame | None:
    f = _p(name)
    if not f.exists():
        return None
    df = pd.read_csv(f, parse_dates=["date"])
    df = df.dropna(subset=["date"]).sort_values("date")
    if "close" in df.columns:
        df = df.dropna(subset=["close"])          # never let a truncated last line become "today"
    return df


def roll_days(idx: pd.DatetimeIndex) -> pd.Series:
    """NYMEX NG expires 3 business days before the 1st of the delivery month; the next session is a roll."""
    r = pd.Series(False, index=idx)
    if len(idx) == 0:
        return r
    for m in pd.date_range(idx[0].replace(day=1), idx[-1] + pd.offsets.MonthBegin(2), freq="MS"):
        e = m - pd.offsets.BDay(3); nxt = idx[idx > e]
        if len(nxt):
            r[nxt[0]] = True
    return r


def hdd_anomaly_z(hdd: pd.Series) -> pd.Series:
    """7-day HDD anomaly vs climatology of PRIOR years only, lagged one day, scaled by its 3-year std."""
    h = hdd.asfreq("D")
    yr = h.index.year; norm = pd.Series(np.nan, index=h.index)
    for y in range(yr.min() + 2, yr.max() + 2):
        past = h[yr < y]
        if past.empty:
            continue
        cl = past.groupby(past.index.dayofyear).mean()
        cl = pd.concat([cl.iloc[-7:], cl, cl.iloc[:7]]).rolling(15, center=True).mean().iloc[7:-7]
        m = yr == y; norm[m] = cl.reindex(h.index.dayofyear[m]).values
    a7 = (h - norm).shift(1).rolling(7).sum()
    return (a7 / a7.rolling(1095, min_periods=300).std()), norm


def features(ng: pd.DataFrame, cot: pd.DataFrame, hdd: pd.Series, s: dict) -> pd.DataFrame:
    ng = ng.set_index("date").sort_index()
    C = ng.close
    roll = roll_days(ng.index)
    lr = np.log(C).diff(); lr[roll] = np.nan
    sig = lr.rolling(20, min_periods=10).std()
    f = pd.DataFrame(index=ng.index)
    f["open"], f["high"], f["low"], f["close"] = ng.open, ng.high, ng.low, C
    f["roll"] = roll; f["sig"] = sig
    f["r3s"] = lr.fillna(0).rolling(3).sum() / (sig * np.sqrt(3))
    oi = cot.set_index("date").oi.sort_index()
    chg = np.log(oi).diff(int(s["oi_weeks"]))
    chg.index = chg.index + pd.Timedelta(days=6)        # Tuesday positions, released Friday -> usable Monday
    f["oi_chg"] = chg.reindex(ng.index, method="ffill", limit=10)
    z, _ = hdd_anomaly_z(hdd)
    f["cold_z"] = z.dropna().reindex(ng.index, method="ffill", limit=5)
    f["dow"] = ng.index.dayofweek
    f["season"] = np.where(ng.index.month >= 10, ng.index.year, ng.index.year - 1)
    return f


def leg_signal(row, s: dict, all_year: bool = False) -> tuple[int, str | None, int | None]:
    """Return (side, leg, hold) for one bar; side +1 CALL, -1 PUT, 0 none. all_year=True ignores the winter-only gate (research)."""
    if not all_year and pd.Timestamp(row.name).month not in WINTER:
        return 0, None, None
    oi, dow = row.oi_chg, int(row.dow)
    if dow == 0 and s["leg_mon"] and pd.notna(oi) and oi != 0:
        return (1 if oi < 0 else -1), "MON open-interest", int(s["mon_hold"])
    if dow == 2 and s["leg_wed"] and ((pd.notna(row.r3s) and row.r3s < 0) or (pd.notna(row.cold_z) and row.cold_z > s["cold_z"])):
        return -1, "WED into-EIA put", int(s["wed_hold"])
    if dow == 4 and s["leg_fri"] and pd.notna(oi) and oi != 0:
        return (-1 if oi < 0 else 1), "FRI weekend open-interest", int(s["fri_hold"])
    return 0, None, None


def outcome(f: pd.DataFrame, i: int, side: int, hold: int) -> dict | None:
    """Movement capture after entry at close of bar i: first touch of +/-1 sigma_h, close direction, reach."""
    if i + hold >= len(f) or f.roll.iloc[i + 1:i + hold + 1].any():
        return None
    c0, sd = f.close.iloc[i], f.sig.iloc[i]
    if not np.isfinite(sd) or sd <= 0:
        return None
    sh = sd * np.sqrt(hold)
    up_lvl, dn_lvl = c0 * np.exp(sh), c0 * np.exp(-sh)
    ft = 0
    for k in range(1, hold + 1):
        hi = f.high.iloc[i + k] if pd.notna(f.high.iloc[i + k]) else f.close.iloc[i + k]
        lo = f.low.iloc[i + k] if pd.notna(f.low.iloc[i + k]) else f.close.iloc[i + k]
        hit_fav = hi >= up_lvl if side > 0 else lo <= dn_lvl
        hit_adv = lo <= dn_lvl if side > 0 else hi >= up_lvl
        if hit_adv:            # same-bar both -> counted as a loss (conservative)
            ft = -1; break
        if hit_fav:
            ft = 1; break
    w = f.iloc[i + 1:i + hold + 1]
    fav = (np.log(w.high.max() / c0) if side > 0 else np.log(c0 / w.low.min())) if w.high.notna().all() else np.nan
    c1 = f.close.iloc[i + hold]
    return dict(first_touch=ft, dir_ok=bool(np.sign(np.log(c1 / c0)) == side), reach_half=bool(fav >= 0.5 * sh) if pd.notna(fav) else None,
                move_pct=round(float(np.log(c1 / c0) * 100 * side), 2), exit_close=round(float(c1), 4))


def backtest(f: pd.DataFrame, s: dict) -> dict:
    rows = []
    idx = {d: i for i, d in enumerate(f.index)}
    for d, row in f[f.index.month.isin(WINTER) & (f.season >= 2010)].iterrows():
        side, leg, hold = leg_signal(row, s)
        if not side:
            continue
        o = outcome(f, idx[d], side, hold)
        if o:
            rows.append(dict(date=d, season=int(row.season), leg=leg, side=side, **o))
    T = pd.DataFrame(rows)
    if T.empty:
        return {"periods": [], "legs": [], "winters": [], "n": 0}

    def agg(t: pd.DataFrame) -> dict:
        dec = (t.first_touch != 0).sum()
        weeks = max(1, t.date.dt.to_period("W").nunique())
        return dict(n=int(len(t)), first_touch=round(float((t.first_touch == 1).sum() / dec * 100), 1) if dec else None,
                    dir_hit=round(float(t.dir_ok.mean() * 100), 1), reach_half=round(float(t.reach_half.dropna().astype(float).mean() * 100), 1),
                    per_active_week=round(float(len(t) / weeks), 2))
    periods = [dict(period=nm, **agg(T[m])) for nm, m in
               [("2010-16 (selection)", T.season <= 2016), ("2017-19 (selection)", (T.season >= 2017) & (T.season <= 2019)),
                ("2020+ (held out)", T.season >= 2020), ("All winters", T.season >= 0)] if m.any()]
    legs = [dict(leg=k, **agg(g)) for k, g in T.groupby("leg")]
    winters = [dict(winter=f"{k}-{(k + 1) % 100:02d}", **agg(g)) for k, g in T.groupby("season")]
    return {"periods": periods, "legs": legs, "winters": winters, "n": int(len(T))}


def year_round(f: pd.DataFrame, s: dict) -> dict:
    """Research: the same playbook legs over the WHOLE year (winter vs the other months), plus an exploratory grid of
    every weekday x signal x direction. Direction hit = close of the hold window beyond entry in the signalled direction;
    first-touch = +1 sigma reached before -1 sigma. Selection 2010-19, held out 2020+."""
    idx = {d: i for i, d in enumerate(f.index)}
    rows = []
    for d, row in f[f.season >= 2010].iterrows():
        i = idx[d]; win = d.month in WINTER
        cands = []
        side, leg, hold = leg_signal(row, s, all_year=True)
        if side:
            cands.append((leg, side, hold))
        oi, dow = row.oi_chg, int(row.dow)
        for nm, val in (("oi", oi if pd.notna(oi) and oi != 0 else None), ("r3s", row.r3s if pd.notna(row.r3s) else None),
                        ("cold", (row.cold_z - s["cold_z"]) if pd.notna(row.cold_z) else None)):
            if val is None or val == 0:
                continue
            for dirn in (1, -1):
                cands.append((f"grid {['MON','TUE','WED','THU','FRI'][dow]} {nm}{'+' if val > 0 else '-'}->{'CALL' if dirn * (1 if val > 0 else -1) > 0 else 'PUT'}",
                              dirn * (1 if val > 0 else -1), 1))
        for leg, sd_, hold in cands:
            o = outcome(f, i, sd_, hold)
            if o:
                rows.append(dict(date=d, season=int(row.season), winter=win, leg=leg, first_touch=o["first_touch"], dir_ok=o["dir_ok"], side=sd_, rsign=sd_ if o["dir_ok"] else -sd_))
    T = pd.DataFrame(rows)
    if T.empty:
        return {"n": 0, "legs": []}

    def agg(t):
        dec = (t.first_touch != 0).sum()
        return dict(n=int(len(t)), dir_hit=round(float(t.dir_ok.mean() * 100), 1) if len(t) else None,
                    first_touch=round(float((t.first_touch == 1).sum() / dec * 100), 1) if dec else None)
    rng = np.random.default_rng(11)

    def shuffle_p(g, n=500):
        """Share of 500 random re-assignments of the CALL/PUT sides (same days, same realised moves) that do at least as well."""
        if len(g) < 20:
            return None
        rs, sd = g.rsign.values, g.side.values; obs = float((rs == sd).mean())
        return round(float(np.mean([(rs == rng.permutation(sd)).mean() >= obs for _ in range(n)])), 3)
    out = []
    for leg, g in T.groupby("leg"):
        out.append(dict(leg=leg, p_all=shuffle_p(g), p_held_out=shuffle_p(g[g.season >= 2020]), all=agg(g), winter=agg(g[g.winter]), other=agg(g[~g.winter]),
                        sel=agg(g[g.season <= 2019]), held_out=agg(g[g.season >= 2020]),
                        held_out_other=agg(g[(g.season >= 2020) & ~g.winter]),
                        years_above_50=int((g.groupby("season").dir_ok.mean() > .5).sum()), years=int(g.season.nunique())))
    return {"n": int(len(T)), "legs": out, "asof": str(f.index[-1].date())}


def sensitivity(ng: pd.DataFrame, cot: pd.DataFrame, hdd: pd.Series, s: dict) -> list[dict]:
    """Nudge one setting at a time; a real edge keeps roughly the same held-out (2020+) direction hit, all months, for the MON/WED/FRI legs."""
    variants = [("base", {})] + [(f"oi_weeks={w}", {"oi_weeks": w}) for w in (3, 5, 6)] + \
               [(f"cold_z={c}", {"cold_z": c}) for c in (-0.5, 0.5, 1.0)] + [(f"holds={h}", {"mon_hold": h, "wed_hold": h, "fri_hold": h}) for h in (1, 2, 3)]
    res = []
    for nm, ch in variants:
        sv = {**s, **ch}
        f = _settled(features(ng, cot, hdd, sv)); idx = {d: i for i, d in enumerate(f.index)}; hits = {}
        for d, row in f[f.season >= 2020].iterrows():
            side, leg, hold = leg_signal(row, sv, all_year=True)
            if side:
                o = outcome(f, idx[d], side, hold)
                if o:
                    hits.setdefault(leg, []).append(o["dir_ok"])
        res.append(dict(variant=nm, **{k: (len(v), round(100 * float(np.mean(v)), 1)) for k, v in hits.items()}))
    return res


def log_year_round(f: pd.DataFrame, s: dict, raw=None) -> None:
    try:
        if raw is not None:
            sens = sensitivity(*raw, s); _atomic_text("sensitivity.json", json.dumps(sens, indent=1))
            for r_ in sens:
                log.warning("YEAR_ROUND_SENS %s", r_)
        r = year_round(f, s)
        _atomic_text("year_round.json", json.dumps(r, indent=1))
        for L in r["legs"]:
            if L["leg"].startswith("grid") and not (L["sel"]["dir_hit"] and L["sel"]["dir_hit"] >= 53):
                continue
            log.warning("YEAR_ROUND %s | p(chance) all %s held-out %s | all %s | winter %s | other %s | sel2010-19 %s | held-out2020+ %s | held-out other months %s | yrs>50 %s/%s",
                     L["leg"], L["p_all"], L["p_held_out"], L["all"], L["winter"], L["other"], L["sel"], L["held_out"], L["held_out_other"], L["years_above_50"], L["years"])
    except Exception as exc:  # pragma: no cover
        log.warning("year_round failed: %s", exc)


ROBUSTNESS = {  # offline research, Oct 2026 (NYMEX NG 2010-26); shown on the page for context
    "sensitivity": "2020+ direction 54-56% across OI lookback 3-6 weeks, cold threshold -0.5..1.0 and holds 1-3 sessions (no cliffs).",
    "bootstrap": "All winters: 53.7% direction, 95% winter-bootstrap range 51.0-56.2%; 14 of 17 winters above 50%.",
    "placebo": "Shuffling the OI signal 500x: Mon+Fri legs alone p = 0.12 on 2020+ (not significant on their own). Wednesday leg is the strongest.",
    "caveat": "Modest edge (~55%), not a high-win system. Targets/stop are in sigma; NYMEX settles ~00:00-01:00 IST, MCX closes 23:30/23:55 IST.",
}


# ----------------------------------------------------------------------------- paper ledger (forward test)
def _ledger() -> list[dict]:
    f = _p("ledger.jsonl")
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def update_ledger() -> None:
    ng, cot, hd = _read("ng.csv"), _read("cot.csv"), _read("hdd_obs.csv")
    if ng is None or cot is None or hd is None:
        return
    s = load_settings()
    f = _settled(features(ng, cot, hd.set_index("date").hdd, s))
    led = {r["date"]: r for r in _ledger()}
    idx = {d: i for i, d in enumerate(f.index)}
    start = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=21)
    for d, row in f[f.index >= start].iterrows():
        key = str(d.date())
        side, leg, hold = leg_signal(row, s)
        if side and key not in led:
            sh = row.close * row.sig * np.sqrt(hold)
            led[key] = dict(date=key, leg=leg, side="CALL" if side > 0 else "PUT", hold=hold, entry=round(float(row.close), 4),
                            t1=round(float(row.close + side * s["t1"] * sh), 4), t2=round(float(row.close + side * s["t2"] * sh), 4),
                            stop=round(float(row.close - side * s["stop"] * sh), 4), logged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        r = led.get(key)
        if r and "result" not in r:
            o = outcome(f, idx[d], 1 if r["side"] == "CALL" else -1, r["hold"])
            if o:
                r["result"] = o
    _atomic_text("ledger.jsonl", "\n".join(json.dumps(v) for v in sorted(led.values(), key=lambda x: x["date"])) + "\n")


def _settled(f: pd.DataFrame, now_utc: datetime | None = None) -> pd.DataFrame:
    """Drop the in-progress NYMEX bar. A session's bar (dated by its trade date) opens at 18:00 New York time the evening
    before and settles ~14:30 NY; we treat it as settled from 15:00 NY. Uses the real New York clock (DST-aware)."""
    if f.empty:
        return f
    from zoneinfo import ZoneInfo
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    last = f.index[-1].date()
    if last > ny.date() or (last == ny.date() and ny.hour < 15):
        return f.iloc[:-1]
    return f


# ----------------------------------------------------------------------------- status for the page
def _now_ist() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def _signal_block(row, s: dict, usdinr: float | None, provisional: bool) -> dict | None:
    side, leg, hold = leg_signal(row, s)
    if not side:
        return None
    c, sig_d = float(row.close), float(row.sig)
    sh = c * sig_d * np.sqrt(hold)
    lv = lambda k: round(float(c + side * k * sh), 4)
    return dict(side="CALL" if side > 0 else "PUT", side_n=side, leg=leg, hold=hold, date=str(row.name.date()), weekday=row.name.day_name(),
                provisional=provisional, nymex=dict(entry=round(c, 4), t1=lv(s["t1"]), t2=lv(s["t2"]), stop=lv(-s["stop"])),
                sigma_h_pct=round(float(sig_d * np.sqrt(hold) * 100), 2),
                approx_inr=None if usdinr is None else {k: round(v * usdinr, 1) for k, v in
                                                        dict(entry=c, t1=lv(s["t1"]), t2=lv(s["t2"]), stop=lv(-s["stop"])).items()})


def status(kite=None, now_ist: datetime | None = None) -> dict:
    s = load_settings(); now_ist = now_ist or _now_ist()
    live_err = refresh_price(float(s["live_price_minutes"])) if float(s["live_price_minutes"]) > 0 else None
    meta = _meta()
    ng, cot, hd, fc, fx = _read("ng.csv"), _read("cot.csv"), _read("hdd_obs.csv"), _read("hdd_fcst.csv"), _read("usdinr.csv")
    out = dict(engine="NG Winter weekly playbook (paper, spotting only - no orders)", storage=str(ROOT), settings=s,
               meta=meta, robustness=ROBUSTNESS, refreshing=bool(_refresh_thread and _refresh_thread.is_alive()),
               live_price_error=live_err, now_ist=now_ist.strftime("%a %d %b %H:%M"))
    if ng is None or cot is None or hd is None:
        out["error"] = "No cached data yet - press Refresh data."
        return out
    f_all = features(ng, cot, hd.set_index("date").hdd, s)
    f = _settled(f_all)
    last = f.iloc[-1]; d = f.index[-1]
    usdinr = float(fx.close.iloc[-1]) if fx is not None and len(fx) else None
    # Today's provisional signal: the NYMEX bar still trading (MCX closes before the NYMEX settle, so this is what gets traded)
    today = _signal_block(f_all.iloc[-1], s, usdinr, True) if len(f_all) > len(f) else None
    settled = _signal_block(last, s, usdinr, False)
    primary = today if len(f_all) > len(f) else settled
    cur = f_all.iloc[-1]; sig_d = float(cur.sig)
    out["inputs"] = dict(date=str(f_all.index[-1].date()), weekday=f_all.index[-1].day_name(), close=round(float(cur.close), 4),
                         sigma_d_pct=round(sig_d * 100, 2), r3s=None if pd.isna(cur.r3s) else round(float(cur.r3s), 2),
                         oi_chg=None if pd.isna(cur.oi_chg) else round(float(cur.oi_chg), 4),
                         cold_z=None if pd.isna(cur.cold_z) else round(float(cur.cold_z), 2),
                         usdinr=None if usdinr is None else round(usdinr, 2),
                         mcx_equiv=None if usdinr is None else round(float(cur.close) * usdinr, 1),
                         in_season=f_all.index[-1].month in WINTER, live_bar=len(f_all) > len(f), settled_date=str(d.date()))
    out["today"] = today; out["settled"] = settled; out["signal"] = primary
    out["trading_day_view"] = len(f_all) > len(f)
    from .commodity_mcx import mcx_session
    sess = mcx_session(now_ist)
    win_from = (sess["close"] - timedelta(minutes=int(s["entry_minutes_before_close"]))).strftime("%H:%M")
    win_to = (sess["close"] - timedelta(minutes=5)).strftime("%H:%M")
    out["session"] = dict(is_open=sess["is_open"], open_str=sess["open_str"], close_str=sess["close_str"], us_dst=sess["us_dst"],
                          entry_window=f"{win_from}-{win_to}", weekday=sess["weekday"])
    dow = int(cur.dow)
    nxt = {0: "Wednesday close", 1: "Wednesday close", 2: "Friday close", 3: "Friday close", 4: "Monday close", 5: "Monday close", 6: "Monday close"}
    out["next_check"] = None if primary else nxt.get(dow, "next session")
    # ---- MCX live layer (preferred view)
    if kite is None:
        out["mcx"] = {"error": "Kite login required - log in on the dashboard to see MCX prices and the option chain."}
    else:
        try:
            from . import commodity_mcx
            out["mcx"] = commodity_mcx.snapshot(kite, s, primary["side_n"] if primary else None, primary["hold"] if primary else None, sig_d, now_ist)
            if primary and primary.get("provisional") and "error" not in out["mcx"]:
                record_mcx_entry(primary, out["mcx"], s, now_ist)
            score_mcx_ledger(kite, now_ist)
        except Exception as exc:  # fail-soft: the NYMEX view must still render
            log.warning("commodity_desk mcx snapshot failed: %s", exc)
            out["mcx"] = {"error": f"MCX data unavailable: {str(exc)[:160]}"}
    if fc is not None and len(fc):
        _, norm = hdd_anomaly_z(hd.set_index("date").hdd)
        fcs = fc.set_index("date").hdd
        clim = norm.groupby(norm.index.dayofyear).last()
        nrm = clim.reindex(fcs.index.dayofyear).values
        out["forecast"] = dict(days=len(fcs), hdd_sum=round(float(fcs.sum()), 1), normal_sum=round(float(np.nansum(nrm)), 1),
                               anomaly=round(float(fcs.sum() - np.nansum(nrm)), 1),
                               rows=[dict(date=str(i.date()), hdd=round(float(v), 1), normal=None if np.isnan(n) else round(float(n), 1))
                                     for i, v, n in zip(fcs.index, fcs.values, nrm)])
    out["backtest"] = backtest(f, s)
    led = _ledger()[-40:][::-1]
    done = [r for r in led if r.get("result")]
    out["ledger"] = led
    out["ledger_summary"] = dict(n=len(done), dir_hit=round(float(np.mean([r["result"]["dir_ok"] for r in done]) * 100), 1) if done else None,
                                 first_touch=round(sum(r["result"]["first_touch"] == 1 for r in done) /
                                                   sum(r["result"]["first_touch"] != 0 for r in done) * 100, 1)
                                 if any(r["result"]["first_touch"] != 0 for r in done) else None)
    ml = _mcx_ledger()[-40:][::-1]
    out["mcx_ledger"] = ml
    sc = [r for r in ml if r.get("exit")]
    opt = [r["exit"]["option_pct"] for r in sc if r["exit"].get("option_pct") is not None]
    out["mcx_ledger_summary"] = dict(n=len(sc), fut_dir_hit=round(float(np.mean([r["exit"]["fut_dir_ok"] for r in sc]) * 100), 1) if sc else None,
                                     opt_win=round(float(np.mean([p > 0 for p in opt]) * 100), 1) if opt else None,
                                     opt_avg_pct=round(float(np.mean(opt)), 1) if opt else None)
    return out


# ----------------------------------------------------------------------------- MCX paper ledger (real MCX prices)
def _mcx_ledger() -> list[dict]:
    f = _p("mcx_ledger.jsonl")
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _write_mcx_ledger(rows: list[dict]) -> None:
    _atomic_text("mcx_ledger.jsonl", "\n".join(json.dumps(r) for r in sorted(rows, key=lambda x: x["date"])) + "\n")


def record_mcx_entry(sig: dict, mcx: dict, s: dict, now_ist: datetime) -> None:
    """Snapshot today's MCX future + suggested option in the last `entry_minutes_before_close` minutes before the MCX close
    (23:30 IST in US daylight time, 23:55 IST otherwise); last view wins."""
    from .commodity_mcx import mcx_session
    sess = mcx_session(now_ist)
    start = sess["close"] - timedelta(minutes=int(s["entry_minutes_before_close"]))
    if not (sess["weekday"] and start <= now_ist < sess["close"]) or now_ist.date() != pd.Timestamp(sig["date"]).date():
        return
    sug = (mcx.get("suggested") or {}).get("tradingsymbol")
    opt = next((r for r in mcx.get("chain", []) if r["tradingsymbol"] == sug), None) if sug else None
    rows = {r["date"]: r for r in _mcx_ledger()}
    rows[sig["date"]] = dict(date=sig["date"], leg=sig["leg"], side=sig["side"], hold=sig["hold"], snapshot_ist=now_ist.strftime("%H:%M"),
                             fut=dict(tradingsymbol=mcx["future"]["tradingsymbol"], token=mcx["future"].get("instrument_token"), entry=mcx["future"]["ltp"]),
                             levels=mcx.get("levels"),
                             option=None if not opt else dict(tradingsymbol=opt["tradingsymbol"], token=opt.get("instrument_token"),
                                                              label=opt["label"], strike=opt["strike"], entry_mid=opt["mid"],
                                                              lot_size=opt.get("lot_size"), iv_pct=opt.get("iv_pct")))
    _write_mcx_ledger(list(rows.values()))


def score_mcx_ledger(kite, now_ist: datetime, max_calls: int = 6) -> None:
    """After the exit session has closed on MCX, fetch day closes of the future and option and store % moves."""
    rows = _mcx_ledger(); changed = False; calls = 0
    try:
        from . import commodity_mcx
        inst = {r["tradingsymbol"]: r.get("instrument_token") for r in commodity_mcx.instruments(kite, now_ist.date())}
    except Exception:
        return
    for r in rows:
        if r.get("exit") or calls >= max_calls:
            continue
        exit_d = (pd.Timestamp(r["date"]) + pd.offsets.BDay(int(r["hold"]))).date()
        if now_ist.date() <= exit_d:          # score from the day after the exit session (MCX closes 23:30 / 23:55 IST)
            continue
        side = 1 if r["side"] == "CALL" else -1
        ftok = r["fut"].get("token") or inst.get(r["fut"]["tradingsymbol"])   # stored token survives the monthly roll
        fc = commodity_mcx.day_close(kite, ftok, exit_d) if ftok else None; calls += 1
        oc = None
        if r.get("option") and r["option"].get("token"):
            oc = commodity_mcx.day_close(kite, r["option"]["token"], exit_d); calls += 1
        if fc is None:
            continue
        fut_pct = (fc / r["fut"]["entry"] - 1) * 100 * side
        r["exit"] = dict(date=str(exit_d), fut_close=fc, fut_pct=round(fut_pct, 2), fut_dir_ok=bool(fut_pct > 0),
                         option_close=oc, option_pct=round((oc / r["option"]["entry_mid"] - 1) * 100, 1)
                         if oc is not None and r.get("option") and r["option"].get("entry_mid") else None)
        changed = True
    if changed:
        _write_mcx_ledger(rows)


# --- cached, single-flight status for the web routes ------------------------------------------------------------
# status() fetches a Yahoo price and the MCX chain from Kite; running that inside a page request made /admin/commodity take 1-8 s
# and let slow upstream calls tie up web workers. One background thread now refreshes it; the routes only read the last result.
_SC: dict = {'st': None, 'at': 0.0, 'busy': False, 'took': None, 'err': None}
_SLOCK = threading.Lock()


def _status_job(kite_fn) -> None:
    t0 = time.time()
    try:
        st = status(kite=kite_fn() if kite_fn else None)
        _SC.update(st=st, at=time.time(), took=round(time.time() - t0, 1), err=None)
    except Exception as exc:  # noqa: BLE001
        log.warning('commodity_desk status failed: %s', exc)
        _SC.update(err=str(exc)[:200], at=time.time(), took=round(time.time() - t0, 1))
    finally:
        _SC['busy'] = False


def cached_status(kite_fn=None, ttl: float = 20.0) -> dict | None:
    """Last computed status (None until the first background run finishes). Starts a refresh when older than ttl seconds."""
    with _SLOCK:
        if time.time() - _SC['at'] > ttl and not _SC['busy']:
            _SC['busy'] = True
            threading.Thread(target=_status_job, args=(kite_fn,), name='commodity-status', daemon=True).start()
    st = _SC['st']
    if st is None: return None
    out = dict(st); out['status_age_s'] = round(time.time() - _SC['at'], 0); out['status_took_s'] = _SC['took']
    return out


def invalidate_status() -> None:
    _SC['at'] = 0.0


def start_once() -> None:
    """Background scheduler: the cloud has no page views at night, so the refresh must not depend on someone opening the page."""
    if _SCHED["started"]: return
    _SCHED["started"] = True

    def loop():
        time.sleep(30)
        try:        # the year-round study runs once per boot even when the data cache is still fresh
            ng_, cot_, hd_ = _read("ng.csv"), _read("cot.csv"), _read("hdd_obs.csv")
            if ng_ is not None and cot_ is not None and hd_ is not None:
                st_ = load_settings(); log_year_round(_settled(features(ng_, cot_, hd_.set_index("date").hdd, st_)), st_, (ng_, cot_, hd_.set_index("date").hdd))
        except Exception:  # noqa: BLE001
            log.exception("commodity_desk startup study failed")
        while True:
            try:
                maybe_refresh_async()
            except Exception:  # noqa: BLE001
                log.exception("commodity_desk scheduler tick failed")
            time.sleep(60)
    threading.Thread(target=loop, name="commodity-desk-scheduler", daemon=True).start()
