"""V9.5 Daily OI Evidence Lab.

Research-only daily-bar evidence engine. It deliberately does not create
TRADE/WATCH signals or mutate the production playbook registry.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

BUILD_ID = "2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE"
TRIAL15_NUMBER = 15
TRIAL16_NUMBER = 16
FAMILYWISE_ALPHA = 0.05
UNEXPECTED_OI_Z_MIN = 1.5
MIN_VALIDATION_EVENTS = 250
MIN_VALIDATION_DAYS = 60
EXPIRY_REGIME_BREAK = pd.Timestamp("2025-09-01")


def trial15_spec() -> dict:
    return {
        "trial_number": TRIAL15_NUMBER,
        "name": "Unexpected Daily OI -> Next-session Magnitude",
        "unexpected_oi_z_min": UNEXPECTED_OI_Z_MIN,
        "primary_horizon": "1D",
        "secondary_horizon": "2D",
        "secondary_2D_cannot_rescue_1D": True,
        "development_pct": 60,
        "validation_pct": 20,
        "final_pct": 20,
        "final_20_locked": True,
        "familywise_alpha": FAMILYWISE_ALPHA,
        "bonferroni_alpha": FAMILYWISE_ALPHA / TRIAL15_NUMBER,
        "t_stat_hurdle": 3.0,
        "research_only": True,
        "directional_prediction": False,
        "message": (
            "Pre-registered V9.5 test: determine whether a positive unexpected daily futures-OI "
            "shock predicts abnormal next-session movement after realized-volatility, expiry-cycle, "
            "MWPL/ban, membership and OI-integrity controls. The final 20% is unread."
        ),
    }




def trial15_terminal_status(metrics: Mapping, inconclusive_reasons: list[str] | tuple[str, ...]) -> tuple[str, bool]:
    """Return the frozen Trial-15 terminal verdict with efficacy before controls.

    Missing integrity controls can block an otherwise passing feature, but they
    cannot hide a primary efficacy failure.  ``closed`` is true only for a
    terminal efficacy rejection; inconclusive data-quality states remain open
    to a data-layer repair without retuning the hypothesis.
    """
    if not bool(metrics.get("sample_ok")):
        return "INCONCLUSIVE_SAMPLE", False
    if not bool(metrics.get("lift_ok")):
        return "FAIL_NO_INDEPENDENT_LIFT", True
    if not bool(metrics.get("vol_ok")):
        return "FAIL_VOL_REGIME_CONTROL", True
    if not bool(metrics.get("tail_ok")):
        return "FAIL_TAIL_DEPENDENCE", True
    if not bool(metrics.get("stability_ok")):
        return "FAIL_TIME_STABILITY", True
    reasons = list(inconclusive_reasons or [])
    if reasons:
        return "INCONCLUSIVE_" + str(reasons[0]), False
    return "PASS_VALIDATION", False

def trial16_spec() -> dict:
    return {
        "trial_number": TRIAL16_NUMBER,
        "name": "Direction conditional on validated Daily OI shock",
        "locked": True,
        "auto_run": False,
        "eligibility": "Trial 15 is closed in V9.5.3; Trial 16 remains locked",
        "research_only": True,
    }


def _to_naive_daily_index(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    return idx.normalize()


def _last_weekday(year: int, month: int, weekday: int) -> pd.Timestamp:
    # Python Monday=0 ... Sunday=6.
    last_day = calendar.monthrange(year, month)[1]
    d = pd.Timestamp(year=year, month=month, day=last_day)
    while d.weekday() != weekday:
        d -= pd.Timedelta(days=1)
    return d


def derived_days_to_expiry(index) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Derived monthly stock-F&O expiry calendar with the Sep-2025 regime break.

    This is an explicit approximation: exchange holidays are not inferred. Callers
    with a true historical expiry calendar should supply exact dates upstream.
    """
    idx = _to_naive_daily_index(index)
    dtes, regimes = [], []
    for d in idx:
        post = d >= EXPIRY_REGIME_BREAK
        weekday = 1 if post else 3  # Tuesday or Thursday
        regime = "TUESDAY" if post else "THURSDAY"
        expiry = _last_weekday(d.year, d.month, weekday)
        if expiry < d:
            ny, nm = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            expiry = _last_weekday(ny, nm, weekday)
        dtes.append(max(0, int((expiry - d).days)))
        regimes.append(regime)
    return (
        pd.Series(dtes, index=idx, dtype=float, name="days_to_expiry"),
        pd.Series(True, index=idx, dtype=bool, name="derived_expiry_calendar"),
        pd.Series(regimes, index=idx, dtype=object, name="expiry_regime"),
    )


def _rolling_z(series: pd.Series, window=60, min_obs=20) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mu = s.rolling(window, min_periods=min_obs).mean().shift(1)
    sd = s.rolling(window, min_periods=min_obs).std(ddof=1).shift(1)
    return (s - mu) / sd.where(sd > 1e-12)


def _future_movement(price: pd.DataFrame, atr: pd.Series) -> tuple[pd.Series, pd.Series]:
    close = pd.to_numeric(price["close"], errors="coerce")
    hi1 = pd.to_numeric(price["high"], errors="coerce").shift(-1)
    lo1 = pd.to_numeric(price["low"], errors="coerce").shift(-1)
    m1 = pd.concat([(hi1 - close).abs(), (lo1 - close).abs()], axis=1).max(axis=1) / atr

    hi2 = pd.concat([
        pd.to_numeric(price["high"], errors="coerce").shift(-1),
        pd.to_numeric(price["high"], errors="coerce").shift(-2),
    ], axis=1).max(axis=1)
    lo2 = pd.concat([
        pd.to_numeric(price["low"], errors="coerce").shift(-1),
        pd.to_numeric(price["low"], errors="coerce").shift(-2),
    ], axis=1).min(axis=1)
    m2 = pd.concat([(hi2 - close).abs(), (lo2 - close).abs()], axis=1).max(axis=1) / (atr * math.sqrt(2.0))
    return m1, m2


def build_symbol_daily_frame(price_df: pd.DataFrame, oi_series, *, expiry_dates=None,
                             ban_series=None, mwpl_series=None, futures_volume_series=None) -> pd.DataFrame:
    """Build point-in-time daily features and future movement outcomes for one symbol."""
    if price_df is None or len(price_df) == 0:
        return pd.DataFrame()
    price = price_df.copy()
    price.index = _to_naive_daily_index(price.index)
    price = price[~price.index.duplicated(keep="last")].sort_index()
    for col in ("open", "high", "low", "close"):
        price[col] = pd.to_numeric(price[col], errors="coerce")

    oi = pd.Series(oi_series).copy()
    oi.index = _to_naive_daily_index(oi.index)
    oi = pd.to_numeric(oi, errors="coerce").replace([np.inf, -np.inf], np.nan)
    oi = oi[oi > 0]
    oi = oi[~oi.index.duplicated(keep="last")].sort_index().reindex(price.index)

    out = pd.DataFrame(index=price.index)
    out["oi_level"] = oi
    out["oi_chg_pct"] = oi.pct_change(fill_method=None) * 100.0
    out["raw_oi_z"] = _rolling_z(out["oi_chg_pct"])
    out["oi_level_z_prev"] = _rolling_z(np.log(oi.where(oi > 0))).shift(1)
    out["oi_chg_lag1"] = out["oi_chg_pct"].shift(1)
    out["oi_chg_lag2"] = out["oi_chg_pct"].shift(2)

    prev_close = price["close"].shift(1)
    tr = pd.concat([
        price["high"] - price["low"],
        (price["high"] - prev_close).abs(),
        (price["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=10).mean()
    out["atr14_prev"] = atr.shift(1)
    out["atr_pct_prev"] = (atr.shift(1) / price["close"].shift(1)).replace([np.inf, -np.inf], np.nan)
    logret = np.log(price["close"] / price["close"].shift(1))
    out["realized_vol20_prev"] = (logret.rolling(20, min_periods=15).std(ddof=1) * math.sqrt(252.0)).shift(1)
    # V9.7.2 confound control: five-session realised volatility known before
    # the event date.  Shift one full session so the signal day's close never
    # enters the matching covariate.
    out["realized_vol5_prev"] = (logret.rolling(5, min_periods=4).std(ddof=1) * math.sqrt(252.0)).shift(1)

    # V9.8 incremental-volatility benchmark.  The event is known after the
    # current session closes, so today's close-to-close variance and its HAR
    # weekly/monthly aggregates are valid predictors of next-session variance.
    daily_var = logret.pow(2)
    out["har_daily_var"] = daily_var
    out["har_weekly_var"] = daily_var.rolling(5, min_periods=5).mean()
    out["har_monthly_var"] = daily_var.rolling(22, min_periods=15).mean()

    # Daily OHLC variance proxies for the *realised next session*.  The
    # Yang-Zhang-style daily component includes the overnight jump plus the
    # Rogers-Satchell intraday component; Garman-Klass is retained as a
    # high/low robustness target.  Both are shifted backward one row so the
    # outcome at t is information from t+1 only.
    safe_open = price["open"].where(price["open"] > 0)
    safe_high = price["high"].where(price["high"] > 0)
    safe_low = price["low"].where(price["low"] > 0)
    safe_close = price["close"].where(price["close"] > 0)
    overnight = np.log(safe_open / safe_close.shift(1))
    open_close = np.log(safe_close / safe_open)
    high_low = np.log(safe_high / safe_low)
    gk_daily = (0.5 * high_low.pow(2) - (2.0 * math.log(2.0) - 1.0) * open_close.pow(2)).clip(lower=0.0)
    rs_daily = (np.log(safe_high / safe_open) * np.log(safe_high / safe_close)
                + np.log(safe_low / safe_open) * np.log(safe_low / safe_close)).clip(lower=0.0)
    yz_k = 0.34 / (1.34 + (21.0 / 19.0))
    yz_daily = (overnight.pow(2) + yz_k * open_close.pow(2) + (1.0 - yz_k) * rs_daily).clip(lower=0.0)
    out["next_yz_var"] = yz_daily.shift(-1)
    out["next_gk_var"] = gk_daily.shift(-1)

    if futures_volume_series is None:
        out["futures_volume"] = np.nan
        out["futures_volume_z"] = np.nan
    else:
        fv = pd.Series(futures_volume_series).copy()
        fv.index = _to_naive_daily_index(fv.index)
        fv = pd.to_numeric(fv, errors="coerce").replace([np.inf, -np.inf], np.nan)
        fv = fv.where(fv >= 0).reindex(out.index)
        out["futures_volume"] = fv
        out["futures_volume_z"] = _rolling_z(np.log1p(fv), window=60, min_obs=20)

    if expiry_dates is None:
        dte, derived, regime = derived_days_to_expiry(out.index)
        out["days_to_expiry"] = dte
        out["derived_expiry_calendar"] = derived
        out["expiry_regime"] = regime
    else:
        exp = pd.Series(expiry_dates).copy()
        exp.index = _to_naive_daily_index(exp.index)
        exp = pd.to_datetime(exp, errors="coerce").reindex(out.index)
        out["days_to_expiry"] = [(e.normalize() - d).days if pd.notna(e) else np.nan for d, e in zip(out.index, exp)]
        out["derived_expiry_calendar"] = False
        out["expiry_regime"] = np.where(out.index >= EXPIRY_REGIME_BREAK, "TUESDAY", "THURSDAY")

    out["post_2025_expiry_break"] = (out.index >= EXPIRY_REGIME_BREAK).astype(float)
    out["dow"] = out.index.dayofweek.astype(float)
    out["ban_flag"] = False if ban_series is None else pd.Series(ban_series).reindex(out.index).astype("boolean").fillna(False).astype(bool)
    out["mwpl_pct"] = np.nan if mwpl_series is None else pd.to_numeric(pd.Series(mwpl_series).reindex(out.index), errors="coerce")

    m1, m2 = _future_movement(price, atr)
    out["movement_1d_atr"] = m1
    out["movement_2d_atr"] = m2
    # For an event formed after session t closes, the two *complete* sessions
    # before it are the next-session movements originating at t-2 and t-3.
    # These are diagnostics only; they do not enter Trial-19 event formation.
    out["movement_prev1_atr"] = m1.shift(2)
    out["movement_prev2_atr"] = m1.shift(3)
    out["eligible"] = (
        out[["oi_chg_pct", "atr14_prev", "realized_vol20_prev", "days_to_expiry"]].notna().all(axis=1)
        & out["movement_1d_atr"].notna()
    )
    return out


_MODEL_COLUMNS = [
    "oi_chg_lag1", "oi_chg_lag2", "days_to_expiry", "days_to_expiry_sq",
    "post_2025_expiry_break", "oi_level_z_prev", "dow_1", "dow_2", "dow_3", "dow_4",
]


def _model_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    f["days_to_expiry_sq"] = pd.to_numeric(f.get("days_to_expiry"), errors="coerce") ** 2
    dow = pd.to_numeric(f.get("dow"), errors="coerce")
    for i in range(1, 5):
        f[f"dow_{i}"] = (dow == i).astype(float)
    return f.reindex(columns=_MODEL_COLUMNS).apply(pd.to_numeric, errors="coerce")


def fit_expected_oi_model(dev_frame: pd.DataFrame) -> dict:
    X = _model_matrix(dev_frame)
    y = pd.to_numeric(dev_frame.get("oi_chg_pct"), errors="coerce")
    valid = X.notna().all(axis=1) & y.notna()
    Xv, yv = X.loc[valid], y.loc[valid]
    if len(Xv) < max(20, len(_MODEL_COLUMNS) + 3):
        return {
            "columns": list(_MODEL_COLUMNS), "coef": [0.0] * (len(_MODEL_COLUMNS) + 1),
            "resid_mean": 0.0, "resid_std": float("nan"), "n": int(len(Xv)),
            "fit_end": str(pd.DatetimeIndex(dev_frame.index).max().date()) if len(dev_frame) else None,
            "valid": False,
        }
    A = np.column_stack([np.ones(len(Xv)), Xv.to_numpy(dtype=float)])
    coef = np.linalg.pinv(A) @ yv.to_numpy(dtype=float)
    resid = yv.to_numpy(dtype=float) - A @ coef
    std = float(np.std(resid, ddof=1)) if len(resid) > 1 else float("nan")
    return {
        "columns": list(_MODEL_COLUMNS), "coef": [float(x) for x in coef],
        "resid_mean": float(np.mean(resid)), "resid_std": std, "n": int(len(Xv)),
        "fit_end": str(pd.DatetimeIndex(dev_frame.index).max().date()), "valid": bool(np.isfinite(std) and std > 1e-12),
    }


def apply_expected_oi_model(frame: pd.DataFrame, model: Mapping) -> pd.DataFrame:
    out = frame.copy()
    X = _model_matrix(out)
    coef = np.asarray(model.get("coef") or [], dtype=float)
    expected = pd.Series(np.nan, index=out.index, dtype=float)
    valid = X.notna().all(axis=1)
    if len(coef) == len(_MODEL_COLUMNS) + 1 and valid.any():
        A = np.column_stack([np.ones(int(valid.sum())), X.loc[valid].to_numpy(dtype=float)])
        expected.loc[valid] = A @ coef
    out["expected_oi_chg_pct"] = expected
    out["unexpected_oi_resid"] = pd.to_numeric(out.get("oi_chg_pct"), errors="coerce") - expected
    sd = float(model.get("resid_std") or np.nan)
    mu = float(model.get("resid_mean") or 0.0)
    out["unexpected_oi_z"] = (out["unexpected_oi_resid"] - mu) / sd if np.isfinite(sd) and sd > 1e-12 else np.nan
    return out


def _safe_lift(event_vals, base_vals):
    e = pd.to_numeric(pd.Series(event_vals), errors="coerce").dropna()
    b = pd.to_numeric(pd.Series(base_vals), errors="coerce").dropna()
    if e.empty or b.empty:
        return None
    bm = float(b.mean())
    if not np.isfinite(bm) or bm <= 1e-12:
        return None
    return float(e.mean() / bm)


def day_cluster_bootstrap_lift(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, reps=1000, seed=950) -> dict:
    """Bootstrap lift by resampling whole validation trading days.

    The cluster universe is every eligible baseline day, not only days on which
    an anomaly happened. This preserves the unconditional validation baseline
    and prevents event-day regime selection from contaminating the confidence
    interval. Rows inside a selected day retain their original multiplicity.
    """
    ev = events.copy()
    ba = baseline.copy()
    if "date" not in ev or "date" not in ba:
        raise ValueError("events and baseline require a date column")
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize()
    ba["date"] = pd.to_datetime(ba["date"]).dt.normalize()
    lift = _safe_lift(ev[field], ba[field])
    days = sorted(set(ba["date"].dropna()))
    if not days or lift is None:
        return {"lift": lift, "ci95_low": None, "ci95_high": None, "clusters": len(days), "reps": int(reps)}

    bval = pd.to_numeric(ba[field], errors="coerce")
    eval_ = pd.to_numeric(ev[field], errors="coerce")
    btmp = pd.DataFrame({"date": ba["date"], "value": bval}).dropna(subset=["date", "value"])
    etmp = pd.DataFrame({"date": ev["date"], "value": eval_}).dropna(subset=["date", "value"])
    bstats = btmp.groupby("date")["value"].agg(["sum", "count"]).reindex(days).fillna(0.0)
    estats = etmp.groupby("date")["value"].agg(["sum", "count"]).reindex(days).fillna(0.0)
    bsum = bstats["sum"].to_numpy(dtype=float)
    bcount = bstats["count"].to_numpy(dtype=float)
    esum = estats["sum"].to_numpy(dtype=float)
    ecount = estats["count"].to_numpy(dtype=float)

    rng = np.random.default_rng(seed)
    vals = []
    n_days = len(days)
    for _ in range(int(reps)):
        draw = rng.integers(0, n_days, size=n_days)
        bc = float(bcount[draw].sum())
        ec = float(ecount[draw].sum())
        if bc <= 0 or ec <= 0:
            continue
        bm = float(bsum[draw].sum() / bc)
        em = float(esum[draw].sum() / ec)
        if np.isfinite(bm) and bm > 1e-12 and np.isfinite(em):
            vals.append(em / bm)
    if not vals:
        lo = hi = None
    else:
        lo, hi = (float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975)))
    return {"lift": lift, "ci95_low": lo, "ci95_high": hi, "clusters": n_days, "reps": int(reps)}


def cluster_robust_ols(y, x: pd.DataFrame, clusters) -> dict:
    y = pd.Series(y).reset_index(drop=True)
    X = pd.DataFrame(x).reset_index(drop=True)
    c = pd.Series(clusters).reset_index(drop=True)
    valid = y.notna() & c.notna() & X.notna().all(axis=1)
    yv = y.loc[valid].astype(float).to_numpy()
    Xv = X.loc[valid].astype(float)
    cv = c.loc[valid]
    names = list(Xv.columns)
    if len(yv) <= len(names) + 2:
        return {"n": len(yv), "clusters": int(cv.nunique()), "coef": {}, "se": {}, "t": {}}
    A = np.column_stack([np.ones(len(Xv)), Xv.to_numpy()])
    bread = np.linalg.pinv(A.T @ A)
    beta = bread @ A.T @ yv
    resid = yv - A @ beta
    meat = np.zeros((A.shape[1], A.shape[1]), dtype=float)
    cv_np = cv.to_numpy()
    for g in pd.unique(cv):
        mask = cv_np == g
        score = A[mask].T @ resid[mask]
        meat += np.outer(score, score)
    G = int(cv.nunique())
    N, k = A.shape
    correction = (G / (G - 1)) * ((N - 1) / (N - k)) if G > 1 and N > k else 1.0
    cov = bread @ meat @ bread * correction
    se = np.sqrt(np.maximum(0.0, np.diag(cov)))
    all_names = ["intercept"] + names
    tvals = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    return {
        "n": int(N), "clusters": G,
        "coef": {n: float(v) for n, v in zip(all_names, beta)},
        "se": {n: float(v) for n, v in zip(all_names, se)},
        "t": {n: float(v) for n, v in zip(all_names, tvals)},
    }


def top_days_removed_lift(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, top_n=3) -> dict:
    if events.empty:
        return {"removed_days": [], "lift": None}
    ev = events.copy(); ba = baseline.copy()
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize(); ba["date"] = pd.to_datetime(ba["date"]).dt.normalize()
    by = ev.groupby("date")[field].mean().sort_values(ascending=False)
    removed = list(by.head(int(top_n)).index)
    e2 = ev[~ev["date"].isin(removed)]
    b2 = ba[~ba["date"].isin(removed)]
    return {"removed_days": [str(pd.Timestamp(d).date()) for d in removed], "lift": _safe_lift(e2[field], b2[field])}


def chronological_block_lifts(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, blocks=4) -> list[dict]:
    ev = events.copy(); ba = baseline.copy()
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize(); ba["date"] = pd.to_datetime(ba["date"]).dt.normalize()
    dates = np.asarray(sorted(set(ba["date"])), dtype="datetime64[ns]")
    out = []
    for i, chunk in enumerate(np.array_split(dates, int(blocks)), start=1):
        if len(chunk) == 0:
            continue
        cset = set(pd.Timestamp(d) for d in chunk)
        ee = ev[ev["date"].isin(cset)]
        bb = ba[ba["date"].isin(cset)]
        out.append({"block": i, "start": str(pd.Timestamp(chunk[0]).date()), "end": str(pd.Timestamp(chunk[-1]).date()), "lift": _safe_lift(ee[field], bb[field])})
    return out


def _partition_dates(frames: Mapping[str, pd.DataFrame]):
    dates = sorted({pd.Timestamp(d).normalize() for f in frames.values() for d in f.index})
    n = len(dates)
    a = int(math.floor(n * 0.60)); b = int(math.floor(n * 0.80))
    return set(dates[:a]), set(dates[a:b]), set(dates[b:])


def _stack(frames: Mapping[str, pd.DataFrame], dates: set[pd.Timestamp]) -> pd.DataFrame:
    rows = []
    for symbol, f in frames.items():
        if f is None or f.empty:
            continue
        x = f.copy()
        x["date"] = _to_naive_daily_index(x.index)
        x = x[x["date"].isin(dates)]
        x["symbol"] = symbol
        rows.append(x.reset_index(drop=True))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _horizon_report(events, baseline, field, *, reps):
    boot = day_cluster_bootstrap_lift(events, baseline, field, reps=reps, seed=950)
    return {
        "event_count": int(len(events)),
        "baseline_count": int(len(baseline)),
        "distinct_days": int(pd.to_datetime(events.get("date", pd.Series(dtype="datetime64[ns]")).dropna()).dt.normalize().nunique()),
        "avg_move_atr": float(pd.to_numeric(events.get(field), errors="coerce").mean()) if len(events) else None,
        "baseline_avg_move_atr": float(pd.to_numeric(baseline.get(field), errors="coerce").mean()) if len(baseline) else None,
        **boot,
    }


def evaluate_trial15(symbol_frames: Mapping[str, pd.DataFrame], *, controls=None, bootstrap_reps=1000) -> dict:
    controls = dict(controls or {})
    dev_dates, val_dates, final_dates = _partition_dates(symbol_frames)
    all_partition_dates = sorted(dev_dates | val_dates | final_dates)
    final_start = min(final_dates) if final_dates else None
    data_end = max(all_partition_dates) if all_partition_dates else None
    v94_discovery_guard = bool(
        final_start is not None and data_end is not None
        and final_start <= (data_end - pd.Timedelta(days=180))
    )
    modeled = {}
    model_diag = {}
    for sym, f in symbol_frames.items():
        if f is None or f.empty:
            continue
        # If caller already supplied unexpected_oi_z (unit/integration fixtures), retain it.
        if "unexpected_oi_z" in f.columns and f["unexpected_oi_z"].notna().any() and "oi_chg_pct" not in f.columns:
            modeled[sym] = f.copy()
            model_diag[sym] = {"fixture_or_precomputed": True}
            continue
        dev = f[pd.DatetimeIndex(f.index).normalize().isin(dev_dates)]
        model = fit_expected_oi_model(dev)
        modeled[sym] = apply_expected_oi_model(f, model)
        model_diag[sym] = model

    dev = _stack(modeled, dev_dates)
    val = _stack(modeled, val_dates)
    # Never stack final outcomes. Only count locked rows.
    final_count = int(sum(pd.DatetimeIndex(f.index).normalize().isin(final_dates).sum() for f in modeled.values()))
    if not val.empty:
        eligible = val.get("eligible", True)
        if not isinstance(eligible, pd.Series):
            eligible = pd.Series(bool(eligible), index=val.index)
        baseline_all = val[eligible.fillna(False).astype(bool)].copy()
    else:
        baseline_all = val.copy()
    events_all = baseline_all[pd.to_numeric(baseline_all.get("unexpected_oi_z"), errors="coerce") >= UNEXPECTED_OI_Z_MIN].copy() if not baseline_all.empty else baseline_all.copy()

    ban_mwpl_analysis = {}
    baseline = baseline_all
    if controls.get("mwpl_available") and not baseline_all.empty and {"ban_flag", "mwpl_pct"}.issubset(baseline_all.columns):
        ban = baseline_all["ban_flag"].fillna(False).astype(bool)
        mwpl = pd.to_numeric(baseline_all["mwpl_pct"], errors="coerce")
        populations = {
            "normal": (~ban) & (mwpl < 80.0),
            "high_mwpl_preban": (~ban) & (mwpl >= 80.0) & (mwpl < 95.0),
            "ban_or_95": ban | (mwpl >= 95.0),
        }
        for name, mask in populations.items():
            bb = baseline_all[mask].copy()
            ee = bb[pd.to_numeric(bb.get("unexpected_oi_z"), errors="coerce") >= UNEXPECTED_OI_Z_MIN].copy() if not bb.empty else bb.copy()
            ban_mwpl_analysis[name] = {
                "events": int(len(ee)), "baseline": int(len(bb)),
                "lift_1D": _safe_lift(ee.get("movement_1d_atr"), bb.get("movement_1d_atr")) if not ee.empty else None,
                "lift_2D": _safe_lift(ee.get("movement_2d_atr"), bb.get("movement_2d_atr")) if not ee.empty else None,
            }
        # Primary Trial-15 inference excludes the mechanically censored ban tail.
        clean = (~ban) & (mwpl < 95.0)
        baseline = baseline_all[clean].copy()

    events = baseline[pd.to_numeric(baseline.get("unexpected_oi_z"), errors="coerce") >= UNEXPECTED_OI_Z_MIN].copy() if not baseline.empty else baseline.copy()

    diagnostics = {}
    if not baseline.empty:
        raw_z = pd.to_numeric(baseline["raw_oi_z"], errors="coerce") if "raw_oi_z" in baseline else pd.Series(np.nan, index=baseline.index)
        raw_events = baseline[raw_z >= UNEXPECTED_OI_Z_MIN].copy()
        neg_z = pd.to_numeric(baseline["unexpected_oi_z"], errors="coerce") if "unexpected_oi_z" in baseline else pd.Series(np.nan, index=baseline.index)
        neg_events = baseline[neg_z <= -UNEXPECTED_OI_Z_MIN].copy()
        level_z = pd.to_numeric(baseline["oi_level_z_prev"], errors="coerce") if "oi_level_z_prev" in baseline else pd.Series(np.nan, index=baseline.index)
        level_events = baseline[level_z >= UNEXPECTED_OI_Z_MIN].copy()
        diagnostics = {
            "raw_positive_oi_z": {
                "1D": _horizon_report(raw_events, baseline, "movement_1d_atr", reps=bootstrap_reps),
                "2D": _horizon_report(raw_events, baseline, "movement_2d_atr", reps=bootstrap_reps),
                "role": "audit comparator only; not a second trial",
            },
            "unexpected_negative_oi_z": {
                "1D": _horizon_report(neg_events, baseline, "movement_1d_atr", reps=bootstrap_reps),
                "2D": _horizon_report(neg_events, baseline, "movement_2d_atr", reps=bootstrap_reps),
                "role": "pre-specified sign diagnostic; cannot rescue Trial 15",
            },
            "high_oi_level_z": {
                "1D": _horizon_report(level_events, baseline, "movement_1d_atr", reps=bootstrap_reps),
                "2D": _horizon_report(level_events, baseline, "movement_2d_atr", reps=bootstrap_reps),
                "role": "market-depth/level diagnostic separate from unexpected change",
            },
        }

    r1 = _horizon_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps)
    r2 = _horizon_report(events, baseline, "movement_2d_atr", reps=bootstrap_reps)

    reg_candidates = ["unexpected_oi_z", "oi_level_z_prev", "realized_vol20_prev", "atr_pct_prev", "days_to_expiry"]
    if controls.get("atm_iv_available") and "atm_iv_pct_pti" in baseline.columns:
        reg_candidates.append("atm_iv_pct_pti")
    reg_cols = [c for c in reg_candidates if c in baseline.columns]
    reg = cluster_robust_ols(
        pd.to_numeric(baseline.get("movement_1d_atr"), errors="coerce"),
        baseline[reg_cols] if reg_cols else pd.DataFrame(index=baseline.index),
        baseline.get("date", pd.Series(index=baseline.index, dtype=object)),
    ) if len(reg_cols) >= 3 and not baseline.empty else {"n":0,"clusters":0,"coef":{},"se":{},"t":{}}

    tail = top_days_removed_lift(events, baseline, "movement_1d_atr", top_n=3) if not baseline.empty else {"removed_days":[],"lift":None}
    blocks = chronological_block_lifts(events, baseline, "movement_1d_atr", blocks=4) if not baseline.empty else []

    vol_quartiles = []
    if not dev.empty and not baseline.empty and "realized_vol20_prev" in dev and "realized_vol20_prev" in baseline:
        dv = pd.to_numeric(dev["realized_vol20_prev"], errors="coerce").dropna()
        if len(dv) >= 20:
            qs = list(np.quantile(dv, [0, .25, .5, .75, 1]))
            qs[0], qs[-1] = -np.inf, np.inf
            for i in range(4):
                bq = baseline[(baseline["realized_vol20_prev"] > qs[i]) & (baseline["realized_vol20_prev"] <= qs[i+1])]
                eq = events[(events["realized_vol20_prev"] > qs[i]) & (events["realized_vol20_prev"] <= qs[i+1])]
                vol_quartiles.append({"quartile": i+1, "events": len(eq), "baseline": len(bq), "lift": _safe_lift(eq["movement_1d_atr"], bq["movement_1d_atr"])})

    inconclusive = []
    if not controls.get("mwpl_available"):
        inconclusive.append("MISSING_MWPL_CONTROL")
    if not controls.get("historical_membership_available"):
        inconclusive.append("SURVIVORSHIP_BIAS")
    if not controls.get("lot_size_normalization_available"):
        inconclusive.append("OI_NORMALIZATION_UNAVAILABLE")
    if controls.get("independent_history_guard_required") and not v94_discovery_guard:
        inconclusive.append("V94_DISCOVERY_WINDOW_OVERLAP")

    t_oi = (reg.get("t") or {}).get("unexpected_oi_z")
    primary_pass = bool(
        r1.get("event_count", 0) >= MIN_VALIDATION_EVENTS
        and r1.get("distinct_days", 0) >= MIN_VALIDATION_DAYS
        and (r1.get("lift") or 0) > 1.0
        and (r1.get("ci95_low") or 0) > 1.0
        and t_oi is not None and np.isfinite(t_oi) and t_oi >= 3.0
        and (tail.get("lift") or 0) > 1.0
        and blocks and sum(1 for b in blocks if (b.get("lift") or 0) > 1.0) > len(blocks) / 2
    )

    status_metrics = {
        "sample_ok": bool(r1.get("event_count", 0) >= MIN_VALIDATION_EVENTS and r1.get("distinct_days", 0) >= MIN_VALIDATION_DAYS),
        "lift_ok": bool((r1.get("lift") or 0) > 1.0 and (r1.get("ci95_low") or 0) > 1.0),
        "vol_ok": bool(t_oi is not None and np.isfinite(t_oi) and t_oi >= 3.0),
        "tail_ok": bool((tail.get("lift") or 0) > 1.0),
        "stability_ok": bool(blocks and sum(1 for b in blocks if (b.get("lift") or 0) > 1.0) > len(blocks) / 2),
    }
    status, trial15_closed = trial15_terminal_status(status_metrics, inconclusive)

    return {
        "build": BUILD_ID,
        "trial15": trial15_spec(),
        "trial16": trial16_spec(),
        "status": status,
        "trial15_closed": bool(trial15_closed),
        "closure_reason": status if trial15_closed else None,
        "primary_pass": bool(primary_pass and not inconclusive),
        "validation": {"1D": r1, "2D": r2},
        "diagnostics": diagnostics,
        "regression_1D": reg,
        "volatility_quartiles": vol_quartiles,
        "top3_day_removed": tail,
        "chronological_blocks": blocks,
        "ban_mwpl_analysis": ban_mwpl_analysis,
        "controls": {
            "realized_vol_control": "APPLIED" if "realized_vol20_prev" in baseline else "UNAVAILABLE",
            "atm_iv_control": "APPLIED" if controls.get("atm_iv_available") else "UNAVAILABLE_NOT_FABRICATED",
            "mwpl_control": "APPLIED" if controls.get("mwpl_available") else "UNAVAILABLE",
            "historical_membership": "APPLIED" if controls.get("historical_membership_available") else "CURRENT_UNIVERSE_REPLAY_SURVIVORSHIP_BIAS",
            "lot_size_normalization": "APPLIED" if controls.get("lot_size_normalization_available") else "UNAVAILABLE_DISCLOSED",
            "v94_discovery_overlap_guard": "APPLIED" if v94_discovery_guard else "FAILED_OR_TOO_SHORT",
        },
        "inconclusive_reasons": inconclusive,
        "models": model_diag,
        "partitions": {
            "development_days": len(dev_dates), "validation_days": len(val_dates), "final_days": len(final_dates),
            "validation_start": str(min(val_dates).date()) if val_dates else None,
            "validation_end": str(max(val_dates).date()) if val_dates else None,
            "final_start": str(final_start.date()) if final_start is not None else None,
            "data_end": str(data_end.date()) if data_end is not None else None,
        },
        "final_test": {"locked": True, "rows_locked": final_count, "message": "Final 20% is unread; V9.5 has no unlock path."},
        "research_only": True,
    }
