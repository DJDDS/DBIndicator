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
)

_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_last_error: dict = {}


# ----------------------------------------------------------------------------- storage
def _p(name: str) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT / name


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
            cur[k] = bool(v) if isinstance(d, bool) else int(v) if isinstance(d, int) else float(v)
        except (TypeError, ValueError):
            continue
    cur["oi_weeks"] = min(max(cur["oi_weeks"], 1), 12)
    for k in ("mon_hold", "wed_hold", "fri_hold"):
        cur[k] = min(max(cur[k], 1), 3)
    _p("settings.json").write_text(json.dumps(cur, indent=1))
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


def _city_hdd(js: list, key: str, field: str) -> pd.Series:
    t = pd.to_datetime(js[0][key]["time"])
    num = np.zeros(len(t)); den = np.zeros(len(t))
    for c, (_, _, w) in zip(js, CITIES):
        v = np.array([np.nan if x is None else x for x in c[key][field]], dtype=float)
        ok = ~np.isnan(v); num[ok] += w * np.maximum(65 - v[ok], 0); den[ok] += w
    return pd.Series(np.where(den > 0, num / np.maximum(den, 1e-9), np.nan), index=t)


def _hdd() -> tuple[pd.Series, pd.Series]:
    lat = ",".join(str(c[0]) for c in CITIES); lon = ",".join(str(c[1]) for c in CITIES)
    base = dict(latitude=lat, longitude=lon, temperature_unit="fahrenheit", timezone="America/New_York")
    end = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    arc = requests.get("https://archive-api.open-meteo.com/v1/archive", timeout=120,
                       params={**base, "daily": "temperature_2m_mean", "start_date": "2007-01-01", "end_date": end}).json()
    fc = requests.get("https://api.open-meteo.com/v1/forecast", timeout=60,
                      params={**base, "daily": "temperature_2m_mean", "past_days": 14, "forecast_days": 16}).json()
    obs = _city_hdd(arc, "daily", "temperature_2m_mean")
    fcs = _city_hdd(fc, "daily", "temperature_2m_mean")
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    observed = obs.combine_first(fcs[fcs.index < today]).dropna()
    forecast = fcs[fcs.index >= today].dropna()
    return observed, forecast


def refresh(force: bool = False) -> dict:
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
                df.to_csv(_p(name), index=False); done.append(name)
            except Exception as exc:  # pragma: no cover - network dependent
                errors[name] = str(exc)[:200]
        try:
            obs, fc = _hdd()
            obs.rename("hdd").rename_axis("date").reset_index().to_csv(_p("hdd_obs.csv"), index=False)
            fc.rename("hdd").rename_axis("date").reset_index().to_csv(_p("hdd_fcst.csv"), index=False)
            done += ["hdd_obs.csv", "hdd_fcst.csv"]
        except Exception as exc:  # pragma: no cover - network dependent
            errors["hdd"] = str(exc)[:200]
        meta = {"refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": done, "errors": errors}
        _p("meta.json").write_text(json.dumps(meta, indent=1))
        _last_error.clear(); _last_error.update(errors)
        try:
            update_ledger()
        except Exception as exc:  # pragma: no cover
            log.warning("commodity_desk ledger update failed: %s", exc)
        return meta


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


def maybe_refresh_async() -> None:
    """Kick a background refresh when the cache is older than settings.refresh_hours (non-blocking)."""
    m = _meta(); s = load_settings()
    try:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(m["refreshed_at"])).total_seconds() / 3600
    except Exception:
        age_h = 1e9
    if age_h >= s["refresh_hours"]:
        refresh_async()


# ----------------------------------------------------------------------------- model (pure, testable)
def _read(name: str) -> pd.DataFrame | None:
    f = _p(name)
    if not f.exists():
        return None
    df = pd.read_csv(f, parse_dates=["date"])
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


def leg_signal(row, s: dict) -> tuple[int, str | None, int | None]:
    """Return (side, leg, hold) for one bar; side +1 CALL, -1 PUT, 0 none."""
    if pd.Timestamp(row.name).month not in WINTER:
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
    _p("ledger.jsonl").write_text("\n".join(json.dumps(v) for v in sorted(led.values(), key=lambda x: x["date"])) + "\n")


def _settled(f: pd.DataFrame) -> pd.DataFrame:
    """Drop today's in-progress bar until NYMEX has settled (~14:30 ET; we wait to 15:00 ET)."""
    if f.empty:
        return f
    et = datetime.now(timezone.utc) - timedelta(hours=4 if 3 <= datetime.now(timezone.utc).month <= 10 else 5)
    if f.index[-1].date() >= et.date() and et.hour < 15:
        return f.iloc[:-1]
    return f


# ----------------------------------------------------------------------------- status for the page
def status() -> dict:
    s = load_settings(); meta = _meta()
    ng, cot, hd, fc, fx = _read("ng.csv"), _read("cot.csv"), _read("hdd_obs.csv"), _read("hdd_fcst.csv"), _read("usdinr.csv")
    out = dict(engine="NG Winter weekly playbook (paper, spotting only - no orders)", storage=str(ROOT), settings=s,
               meta=meta, robustness=ROBUSTNESS, refreshing=bool(_refresh_thread and _refresh_thread.is_alive()))
    if ng is None or cot is None or hd is None:
        out["error"] = "No cached data yet - press Refresh data."
        return out
    f_all = features(ng, cot, hd.set_index("date").hdd, s)
    f = _settled(f_all)
    last = f.iloc[-1]; d = f.index[-1]
    side, leg, hold = leg_signal(last, s)
    usdinr = float(fx.close.iloc[-1]) if fx is not None and len(fx) else None
    sig_d = float(last.sig)
    out["inputs"] = dict(date=str(d.date()), weekday=d.day_name(), close=round(float(last.close), 4), sigma_d_pct=round(sig_d * 100, 2),
                         r3s=None if pd.isna(last.r3s) else round(float(last.r3s), 2),
                         oi_chg=None if pd.isna(last.oi_chg) else round(float(last.oi_chg), 4),
                         cold_z=None if pd.isna(last.cold_z) else round(float(last.cold_z), 2),
                         usdinr=None if usdinr is None else round(usdinr, 2),
                         mcx_equiv=None if usdinr is None else round(float(last.close) * usdinr, 1),
                         in_season=d.month in WINTER, partial_bar_hidden=len(f_all) > len(f))
    if side:
        sh = float(last.close) * sig_d * np.sqrt(hold)
        lv = lambda k: round(float(last.close + side * k * sh), 4)
        out["signal"] = dict(side="CALL" if side > 0 else "PUT", leg=leg, hold=hold, t1=lv(s["t1"]), t2=lv(s["t2"]), stop=lv(-s["stop"]),
                             sigma_h=round(float(sh), 4), sigma_h_pct=round(float(sig_d * np.sqrt(hold) * 100), 2),
                             mcx=None if usdinr is None else {k: round(v * usdinr, 1) for k, v in
                                                              dict(entry=float(last.close), t1=lv(s["t1"]), t2=lv(s["t2"]), stop=lv(-s["stop"])).items()})
    nxt = {0: "Monday close (if no signal) -> next: Wednesday close", 1: "Wednesday close", 2: "Friday close", 3: "Friday close", 4: "Monday close"}
    out["next_check"] = nxt[int(last.dow)] if not side else None
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
    return out
