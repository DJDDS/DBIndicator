"""V9.6 Trial 17 — independent validation of fresh total FUTSTK OI expansion.

The event definition was discovered in V9.5.3 and is frozen here before reading
an older, non-overlapping evidence window.  This module is directionless,
research-only and has no production activation path.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from . import v95_daily_evidence as v95
from . import v953_contract_structure as cs

BUILD_ID = "2026-09-02-INSTITUTIONAL-V9.6.2-TRIAL17-PROMOTION-CONTROLS"
TRIAL17_NUMBER = 17
TRIAL18_NUMBER = 18
TOTAL_OI_Z_MIN = 1.5
INDEPENDENT_START = pd.Timestamp("2021-09-01")
INDEPENDENT_END = pd.Timestamp("2023-09-01")
MIN_EVENTS = 250
MIN_EVENT_DAYS = 100
MIN_1D_LIFT = 1.10
T_STAT_HURDLE = 3.0


def trial17_spec() -> dict:
    return {
        "trial_number": TRIAL17_NUMBER,
        "name": "Fresh Total Futures OI Expansion -> Next-session Magnitude",
        "total_oi_z_min": TOTAL_OI_Z_MIN,
        "independent_start": str(INDEPENDENT_START.date()),
        "independent_end": str(INDEPENDENT_END.date()),
        "primary_horizon": "1D",
        "secondary_horizon": "2D",
        "secondary_2D_cannot_rescue_1D": True,
        "directional_prediction": False,
        "research_only": True,
        "min_events": MIN_EVENTS,
        "min_event_days": MIN_EVENT_DAYS,
        "min_1d_lift": MIN_1D_LIFT,
        "t_stat_hurdle": T_STAT_HURDLE,
        "prior_locked_finals_untouched": True,
        "message": (
            "Pre-registered after V9.5.3 feature research: validate total share-equivalent "
            "FUTSTK OI z>=1.5 on older non-overlapping NSE history only."
        ),
    }


def trial18_spec() -> dict:
    return {
        "trial_number": TRIAL18_NUMBER,
        "name": "Direction conditional on validated fresh total OI expansion",
        "locked": True,
        "auto_run": False,
        "eligibility": "Only after Trial 17 passes independent validation",
        "research_only": True,
    }


def _prepare_symbol(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    x = cs.build_contract_structure_frame(frame)
    x = x.copy()
    x["date"] = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
    x = x[(x["date"] >= INDEPENDENT_START) & (x["date"] <= INDEPENDENT_END)].copy()
    if x.empty:
        return x
    x["symbol"] = str(symbol).upper()
    eligible = x.get("eligible", True)
    if not isinstance(eligible, pd.Series):
        eligible = pd.Series(bool(eligible), index=x.index)
    if "fno_member_pti" in x:
        eligible = eligible.fillna(False).astype(bool) & x["fno_member_pti"].fillna(False).astype(bool)
    x["trial17_eligible"] = eligible.fillna(False).astype(bool)
    return x.reset_index(drop=True)


def _stack(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows = [_prepare_symbol(symbol, frame) for symbol, frame in frames.items()]
    rows = [r for r in rows if r is not None and not r.empty]
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()



def same_day_matched_report(events: pd.DataFrame, baseline: pd.DataFrame, field: str, *, reps=1000) -> dict:
    """Compare events only with eligible non-events from the same trading dates."""
    if events is None or baseline is None or events.empty or baseline.empty:
        return {"event_count": 0, "baseline_count": 0, "event_days": 0, "lift": None, "ci95_low": None, "ci95_high": None}
    ev = events.copy(); ba = baseline.copy()
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize(); ba["date"] = pd.to_datetime(ba["date"]).dt.normalize()
    event_days = sorted(set(ev["date"].dropna()))
    matched = ba[ba["date"].isin(event_days)].copy()
    if "symbol" in ev.columns and "symbol" in matched.columns:
        ev_keys = pd.MultiIndex.from_frame(ev[["date", "symbol"]].astype({"symbol": str}))
        ba_keys = pd.MultiIndex.from_frame(matched[["date", "symbol"]].astype({"symbol": str}))
        matched = matched.loc[~ba_keys.isin(ev_keys)].copy()
    boot = v95.day_cluster_bootstrap_lift(ev, matched, field, reps=reps, seed=962) if len(matched) else {"lift": None, "ci95_low": None, "ci95_high": None, "clusters": 0, "reps": int(reps)}
    return {
        "event_count": int(len(ev)), "baseline_count": int(len(matched)), "event_days": int(len(event_days)),
        "event_mean": float(pd.to_numeric(ev[field], errors="coerce").mean()) if len(ev) else None,
        "matched_baseline_mean": float(pd.to_numeric(matched[field], errors="coerce").mean()) if len(matched) else None,
        **boot,
    }


def _dte_bucket_series(frame: pd.DataFrame) -> pd.Series:
    field = "nse_near_dte" if "nse_near_dte" in frame.columns else "days_to_expiry"
    dte = pd.to_numeric(frame.get(field), errors="coerce")
    return pd.cut(dte, bins=[-0.001, 5, 10, 20, np.inf], labels=["0-5", "6-10", "11-20", "21+"], include_lowest=True)


def dte_matched_report(events: pd.DataFrame, baseline: pd.DataFrame, field: str) -> dict:
    """Match the baseline to the event DTE-bucket distribution without dropping buckets."""
    if events is None or baseline is None or events.empty or baseline.empty:
        return {"event_count": 0, "baseline_count": 0, "dte_buckets_used": 0, "lift": None, "matched_baseline_mean": None}
    ev = events.copy(); ba = baseline.copy()
    if {"date", "symbol"}.issubset(ev.columns) and {"date", "symbol"}.issubset(ba.columns):
        ev_keys = pd.MultiIndex.from_frame(ev[["date", "symbol"]].assign(date=lambda x: pd.to_datetime(x["date"]).dt.normalize(), symbol=lambda x: x["symbol"].astype(str)))
        ba_keys = pd.MultiIndex.from_frame(ba[["date", "symbol"]].assign(date=lambda x: pd.to_datetime(x["date"]).dt.normalize(), symbol=lambda x: x["symbol"].astype(str)))
        ba = ba.loc[~ba_keys.isin(ev_keys)].copy()
    ev["_dte_bucket"] = _dte_bucket_series(ev); ba["_dte_bucket"] = _dte_bucket_series(ba)
    evv = pd.to_numeric(ev[field], errors="coerce"); bav = pd.to_numeric(ba[field], errors="coerce")
    event_mean = float(evv.mean()) if evv.notna().any() else None
    counts = ev["_dte_bucket"].value_counts(dropna=True)
    total = int(counts.sum())
    weighted = 0.0; used = 0; details = {}
    for bucket, count in counts.items():
        vals = bav[ba["_dte_bucket"] == bucket].dropna()
        if vals.empty:
            continue
        mean = float(vals.mean()); weight = float(count / total) if total else 0.0
        weighted += weight * mean; used += 1
        details[str(bucket)] = {"event_count": int(count), "baseline_count": int(len(vals)), "baseline_mean": mean, "weight": weight}
    lift = float(event_mean / weighted) if event_mean is not None and weighted > 1e-12 else None
    return {
        "event_count": int(len(ev)), "baseline_count": int(len(ba)), "dte_buckets_used": int(used),
        "event_mean": event_mean, "matched_baseline_mean": float(weighted) if used else None, "lift": lift, "buckets": details,
    }


def two_way_cluster_robust_ols(y, x: pd.DataFrame, date_clusters, symbol_clusters) -> dict:
    """OLS with Cameron-Gelbach-Miller two-way clustered covariance (date + symbol)."""
    y = pd.Series(y).reset_index(drop=True)
    X = pd.DataFrame(x).reset_index(drop=True)
    dc = pd.Series(date_clusters).reset_index(drop=True)
    sc = pd.Series(symbol_clusters).reset_index(drop=True)
    valid = y.notna() & dc.notna() & sc.notna() & X.notna().all(axis=1)
    yv = y.loc[valid].astype(float).to_numpy(); Xv = X.loc[valid].astype(float)
    dv = dc.loc[valid].astype(str).to_numpy(); sv = sc.loc[valid].astype(str).to_numpy()
    names = list(Xv.columns)
    if len(yv) <= len(names) + 2:
        return {"n": int(len(yv)), "date_clusters": int(pd.Series(dv).nunique()), "symbol_clusters": int(pd.Series(sv).nunique()), "coef": {}, "se": {}, "t": {}}
    A = np.column_stack([np.ones(len(Xv)), Xv.to_numpy()]); N, k = A.shape
    bread = np.linalg.pinv(A.T @ A); beta = bread @ A.T @ yv; resid = yv - A @ beta

    def _cluster_cov(labels):
        labels = np.asarray(labels, dtype=object); uniq = pd.unique(labels); meat = np.zeros((k, k), dtype=float)
        for g in uniq:
            mask = labels == g; score = A[mask].T @ resid[mask]; meat += np.outer(score, score)
        G = int(len(uniq)); corr = (G/(G-1))*((N-1)/(N-k)) if G > 1 and N > k else 1.0
        return bread @ meat @ bread * corr

    pair = np.asarray([f"{d}\x1f{s}" for d, s in zip(dv, sv)], dtype=object)
    cov = _cluster_cov(dv) + _cluster_cov(sv) - _cluster_cov(pair)
    se = np.sqrt(np.maximum(0.0, np.diag(cov)))
    tvals = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    all_names = ["intercept"] + names
    return {
        "n": int(N), "date_clusters": int(pd.Series(dv).nunique()), "symbol_clusters": int(pd.Series(sv).nunique()),
        "coef": {n: float(v) for n, v in zip(all_names, beta)},
        "se": {n: float(v) for n, v in zip(all_names, se)},
        "t": {n: float(v) for n, v in zip(all_names, tvals)},
    }

def _dte_bucket_reports(events: pd.DataFrame, baseline: pd.DataFrame) -> dict:
    dte_field = "nse_near_dte" if "nse_near_dte" in baseline.columns else "days_to_expiry"
    dte = pd.to_numeric(baseline.get(dte_field), errors="coerce")
    edte = pd.to_numeric(events.get(dte_field), errors="coerce")
    defs = {
        "0-5": (0, 5),
        "6-10": (6, 10),
        "11-20": (11, 20),
        "21+": (21, np.inf),
    }
    out = {}
    for name, (lo, hi) in defs.items():
        bm = (dte >= lo) & (dte <= hi)
        em = (edte >= lo) & (edte <= hi)
        bb = baseline[bm].copy()
        ee = events[em].copy()
        out[name] = {
            "events": int(len(ee)),
            "baseline": int(len(bb)),
            "lift_1D": v95._safe_lift(ee.get("movement_1d_atr"), bb.get("movement_1d_atr")) if len(ee) else None,
        }
    return out


def _concentration(events: pd.DataFrame) -> dict:
    if events.empty or "symbol" not in events:
        return {"symbols": 0, "top5_symbol_event_share": None, "top_symbols": []}
    counts = events["symbol"].astype(str).value_counts()
    total = int(counts.sum())
    top5 = counts.head(5)
    return {
        "symbols": int(counts.size),
        "top5_symbol_event_share": float(top5.sum() / total) if total else None,
        "top_symbols": [{"symbol": str(k), "events": int(v)} for k, v in top5.items()],
    }


def _mwpl_analysis(baseline: pd.DataFrame) -> dict:
    if baseline.empty or not {"mwpl_pct", "ban_flag"}.issubset(baseline.columns):
        return {}
    mwpl = pd.to_numeric(baseline["mwpl_pct"], errors="coerce")
    ban = baseline["ban_flag"].fillna(False).astype(bool)
    populations = {
        "normal": (~ban) & (mwpl < 80),
        "high_mwpl_preban": (~ban) & (mwpl >= 80) & (mwpl < 95),
        "ban_or_95": ban | (mwpl >= 95),
    }
    out = {}
    for name, mask in populations.items():
        bb = baseline[mask].copy()
        ee = bb[pd.to_numeric(bb.get("total_z"), errors="coerce") >= TOTAL_OI_Z_MIN].copy() if len(bb) else bb
        out[name] = {
            "events": int(len(ee)),
            "baseline": int(len(bb)),
            "lift_1D": v95._safe_lift(ee.get("movement_1d_atr"), bb.get("movement_1d_atr")) if len(ee) else None,
        }
    return out



def promotion_verdict(*, frozen_status: str, integrity_ok: bool, earnings_coverage: float,
                      earnings_report: dict, same_day_report: dict, two_way_reg: dict,
                      market_regime_coverage: float, dte_report: dict) -> str:
    """Fail-closed promotion gate declared before V9.6.2 results are read."""
    if frozen_status != "PASS_INDEPENDENT_VALIDATION":
        return "LOCKED_TRIAL17_NOT_PASSED"
    if not integrity_ok:
        return "INCONCLUSIVE_INTEGRITY_CONTROLS"
    if float(earnings_coverage or 0.0) < 0.90:
        return "INCONCLUSIVE_EARNINGS_COVERAGE"
    if float(market_regime_coverage or 0.0) < 0.90:
        return "INCONCLUSIVE_MARKET_REGIME_COVERAGE"
    elift = earnings_report.get("lift")
    eci = earnings_report.get("ci95_low")
    if elift is None or eci is None or not (float(elift) > 1.0 and float(eci) > 1.0):
        return "FAIL_EARNINGS_CONFOUND"
    mlift = same_day_report.get("lift")
    mci = same_day_report.get("ci95_low")
    if mlift is None or mci is None or not (float(mlift) > 1.0 and float(mci) > 1.0):
        return "FAIL_SAME_DAY_MATCH"
    coef = (two_way_reg.get("coef") or {}).get("total_z")
    tval = (two_way_reg.get("t") or {}).get("total_z")
    if coef is None or tval is None or not np.isfinite(coef) or not np.isfinite(tval) or float(coef) <= 0 or float(tval) < T_STAT_HURDLE:
        return "FAIL_TWO_WAY_INFERENCE"
    dlift = dte_report.get("lift")
    if dlift is None or not np.isfinite(dlift) or float(dlift) <= 1.0:
        return "FAIL_DTE_MATCH"
    return "PASS_PROMOTION_CONTROLS"


def _earnings_window_mask(frame: pd.DataFrame, earnings_dates, *, sessions: int = 5) -> pd.Series:
    if frame.empty:
        return pd.Series(False, index=frame.index, dtype=bool)
    dates = pd.DatetimeIndex(pd.to_datetime(frame["date"]).dt.normalize())
    unique = pd.DatetimeIndex(sorted(set(dates)))
    flagged = set()
    raw_dates = [] if earnings_dates is None else list(earnings_dates)
    for value in pd.DatetimeIndex(pd.to_datetime(raw_dates, errors="coerce")).dropna():
        d = pd.Timestamp(value).tz_localize(None).normalize() if getattr(value, "tzinfo", None) else pd.Timestamp(value).normalize()
        pos = int(unique.searchsorted(d, side="left"))
        if pos >= len(unique):
            continue
        lo = max(0, pos - int(sessions)); hi = min(len(unique), pos + int(sessions) + 1)
        flagged.update(unique[lo:hi])
    return pd.Series(dates.isin(flagged), index=frame.index, dtype=bool)


def evaluate_promotion_controls(symbol_frames: Mapping[str, pd.DataFrame], *, frozen_result: dict,
                                controls=None, earnings_map=None, market_regime=None,
                                bootstrap_reps=1000) -> dict:
    """Evaluate V9.6.2 promotion controls without changing frozen Trial 17."""
    controls = dict(controls or {}); earnings_map = dict(earnings_map or {})
    stacked = _stack(symbol_frames)
    if stacked.empty:
        return {"status": "INCONCLUSIVE_NO_DATA", "trial18_eligible": False, "research_only": True}
    baseline = stacked[stacked["trial17_eligible"].fillna(False).astype(bool)].copy()
    baseline = baseline[baseline["movement_1d_atr"].notna()].copy()
    if controls.get("mwpl_available") and {"mwpl_pct", "ban_flag"}.issubset(baseline.columns):
        mwpl = pd.to_numeric(baseline["mwpl_pct"], errors="coerce")
        ban = baseline["ban_flag"].fillna(False).astype(bool)
        baseline = baseline[(~ban) & (mwpl < 95.0)].copy()
    events = baseline[pd.to_numeric(baseline.get("total_z"), errors="coerce") >= TOTAL_OI_Z_MIN].copy()

    # Same-day control removes market-wide day selection mechanically.
    same_day = same_day_matched_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps)
    dte_matched = dte_matched_report(events, baseline, "movement_1d_atr")

    # Earnings control uses only symbols whose NSE filing calendar fetch succeeded.
    emeta = dict(earnings_map.get("_meta") or {})
    loaded_symbols = set(str(s).upper() for s in emeta.get("loaded_symbols") or [])
    event_symbols = set(events.get("symbol", pd.Series(dtype=str)).astype(str).str.upper())
    earnings_coverage = float(len(event_symbols & loaded_symbols) / len(event_symbols)) if event_symbols else 0.0
    ebase = baseline[baseline["symbol"].astype(str).str.upper().isin(loaded_symbols)].copy() if loaded_symbols else baseline.iloc[0:0].copy()
    if not ebase.empty:
        masks=[]
        for symbol, group in ebase.groupby(ebase["symbol"].astype(str).str.upper(), sort=False):
            mask = _earnings_window_mask(group, earnings_map.get(str(symbol).upper(), pd.DatetimeIndex([])), sessions=5)
            masks.append(pd.Series(mask.to_numpy(), index=group.index))
        near = pd.concat(masks).sort_index() if masks else pd.Series(False, index=ebase.index)
        ebase = ebase.loc[~near.reindex(ebase.index).fillna(False)].copy()
    eevents = ebase[pd.to_numeric(ebase.get("total_z"), errors="coerce") >= TOTAL_OI_Z_MIN].copy() if not ebase.empty else ebase.copy()
    earnings_report = v95._horizon_report(eevents, ebase, "movement_1d_atr", reps=bootstrap_reps)
    earnings_report["events_removed"] = int(max(0, len(events) - len(eevents)))

    # Market regime: same-day India VIX plus lagged NIFTY realized volatility.
    regime = pd.DataFrame(market_regime).copy() if market_regime is not None else pd.DataFrame()
    if not regime.empty:
        regime.index = pd.to_datetime(regime.index).tz_localize(None).normalize()
        for col in ("india_vix", "nifty_rv20_prev"):
            if col in regime:
                mapper = pd.to_numeric(regime[col], errors="coerce")
                baseline[col] = pd.to_datetime(baseline["date"]).dt.normalize().map(mapper)
    event_days = pd.DatetimeIndex(pd.to_datetime(events.get("date", pd.Series(dtype="datetime64[ns]"))).dropna()).normalize().unique()
    if len(event_days) and {"india_vix", "nifty_rv20_prev"}.issubset(baseline.columns):
        daily = baseline.groupby(pd.to_datetime(baseline["date"]).dt.normalize())[["india_vix", "nifty_rv20_prev"]].first()
        market_cov = float(daily.reindex(event_days).notna().all(axis=1).mean())
    else:
        market_cov = 0.0

    dte_field = "nse_near_dte" if "nse_near_dte" in baseline.columns else "days_to_expiry"
    reg_cols = [c for c in ("total_z", "realized_vol20_prev", "atr_pct_prev", dte_field, "india_vix", "nifty_rv20_prev") if c in baseline.columns]
    tw = two_way_cluster_robust_ols(
        pd.to_numeric(baseline.get("movement_1d_atr"), errors="coerce"),
        baseline[reg_cols] if reg_cols else pd.DataFrame(index=baseline.index),
        baseline.get("date", pd.Series(index=baseline.index, dtype=object)),
        baseline.get("symbol", pd.Series(index=baseline.index, dtype=object)),
    ) if "total_z" in reg_cols and len(reg_cols) >= 5 else {"n": 0, "date_clusters": 0, "symbol_clusters": 0, "coef": {}, "se": {}, "t": {}}

    integrity_ok = all(bool(controls.get(k)) for k in (
        "historical_membership_available", "historical_cash_price_available",
        "lot_size_normalization_available", "mwpl_available",
    ))
    status = promotion_verdict(
        frozen_status=str(frozen_result.get("status") or ""), integrity_ok=integrity_ok,
        earnings_coverage=earnings_coverage, earnings_report=earnings_report,
        same_day_report=same_day, two_way_reg=tw, market_regime_coverage=market_cov,
        dte_report=dte_matched,
    )
    eligible = status == "PASS_PROMOTION_CONTROLS"
    return {
        "status": status, "trial18_eligible": eligible,
        "trial18_state": "ELIGIBLE_FOR_PREREGISTRATION" if eligible else "LOCKED",
        "research_only": True, "production_activation": False,
        "frozen_trial17_status": frozen_result.get("status"),
        "earnings_symbol_coverage": earnings_coverage,
        "earnings_excluded_1D": earnings_report,
        "same_day_matched_1D": same_day,
        "two_way_regression_1D": tw,
        "market_regime_event_day_coverage": market_cov,
        "dte_matched_1D": dte_matched,
        "controls": {
            "historical_membership": "APPLIED" if controls.get("historical_membership_available") else "UNAVAILABLE",
            "historical_cash_price": "APPLIED" if controls.get("historical_cash_price_available") else "UNAVAILABLE",
            "lot_size_normalization": "APPLIED" if controls.get("lot_size_normalization_available") else "UNAVAILABLE",
            "mwpl_control": "APPLIED" if controls.get("mwpl_available") else "UNAVAILABLE",
            "earnings_calendar": "APPLIED" if earnings_coverage >= 0.90 else "INCOMPLETE",
            "market_regime": "APPLIED" if market_cov >= 0.90 else "INCOMPLETE",
            "atm_iv": "UNAVAILABLE_NOT_FABRICATED",
        },
    }

def evaluate_trial17(symbol_frames: Mapping[str, pd.DataFrame], *, controls=None, bootstrap_reps=1000) -> dict:
    controls = dict(controls or {})
    stacked = _stack(symbol_frames)
    if stacked.empty:
        return {
            "build": BUILD_ID, "trial17": trial17_spec(), "trial18": trial18_spec(),
            "status": "INCONCLUSIVE_NO_DATA", "primary_pass": False, "research_only": True,
            "production_activation": False, "prior_locked_finals_untouched": True,
            "evidence_window": {"start": str(INDEPENDENT_START.date()), "end": str(INDEPENDENT_END.date())},
        }

    baseline_all = stacked[stacked["trial17_eligible"].fillna(False).astype(bool)].copy()
    baseline_all = baseline_all[baseline_all["movement_1d_atr"].notna()].copy()
    baseline = baseline_all
    mwpl_analysis = _mwpl_analysis(baseline_all)
    if controls.get("mwpl_available") and {"mwpl_pct", "ban_flag"}.issubset(baseline_all.columns):
        mwpl = pd.to_numeric(baseline_all["mwpl_pct"], errors="coerce")
        ban = baseline_all["ban_flag"].fillna(False).astype(bool)
        baseline = baseline_all[(~ban) & (mwpl < 95)].copy()

    events = baseline[pd.to_numeric(baseline.get("total_z"), errors="coerce") >= TOTAL_OI_Z_MIN].copy()
    r1 = v95._horizon_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps)
    r2 = v95._horizon_report(events, baseline, "movement_2d_atr", reps=bootstrap_reps)

    dte_field = "nse_near_dte" if "nse_near_dte" in baseline.columns else "days_to_expiry"
    reg_cols = [c for c in ("total_z", "realized_vol20_prev", "atr_pct_prev", dte_field) if c in baseline.columns]
    reg = v95.cluster_robust_ols(
        pd.to_numeric(baseline.get("movement_1d_atr"), errors="coerce"),
        baseline[reg_cols] if len(reg_cols) >= 3 else pd.DataFrame(index=baseline.index),
        baseline.get("date", pd.Series(index=baseline.index, dtype=object)),
    ) if len(reg_cols) >= 3 else {"n": 0, "clusters": 0, "coef": {}, "se": {}, "t": {}}
    t_oi = (reg.get("t") or {}).get("total_z")
    tail = v95.top_days_removed_lift(events, baseline, "movement_1d_atr", top_n=3)
    blocks = v95.chronological_block_lifts(events, baseline, "movement_1d_atr", blocks=4)

    sample_ok = bool(r1.get("event_count", 0) >= MIN_EVENTS and r1.get("distinct_days", 0) >= MIN_EVENT_DAYS)
    lift_ok = bool((r1.get("lift") or 0) >= MIN_1D_LIFT and (r1.get("ci95_low") or 0) > 1.0)
    t_ok = bool(t_oi is not None and np.isfinite(t_oi) and t_oi >= T_STAT_HURDLE)
    tail_ok = bool((tail.get("lift") or 0) > 1.0)
    stability_ok = bool(blocks and sum(1 for b in blocks if (b.get("lift") or 0) > 1.0) >= 3)

    if not sample_ok:
        status = "FAIL_INSUFFICIENT_SAMPLE"
    elif not lift_ok:
        status = "FAIL_NO_INDEPENDENT_LIFT"
    elif not t_ok:
        status = "FAIL_VOL_REGIME_CONTROL"
    elif not tail_ok:
        status = "FAIL_TAIL_DEPENDENCE"
    elif not stability_ok:
        status = "FAIL_TIME_STABILITY"
    elif not controls.get("historical_membership_available"):
        status = "INCONCLUSIVE_SURVIVORSHIP_BIAS"
    elif not controls.get("historical_cash_price_available"):
        status = "INCONCLUSIVE_HISTORICAL_PRICE_COVERAGE"
    elif not controls.get("lot_size_normalization_available"):
        status = "INCONCLUSIVE_OI_NORMALIZATION"
    elif not controls.get("mwpl_available"):
        status = "INCONCLUSIVE_MISSING_MWPL_CONTROL"
    else:
        status = "PASS_INDEPENDENT_VALIDATION"

    primary_pass = status == "PASS_INDEPENDENT_VALIDATION"
    return {
        "build": BUILD_ID,
        "trial17": trial17_spec(),
        "trial18": trial18_spec(),
        "status": status,
        "primary_pass": primary_pass,
        "trial17_closed": status.startswith("FAIL_"),
        "research_only": True,
        "production_activation": False,
        "directional_prediction": False,
        "prior_locked_finals_untouched": True,
        "evidence_window": {
            "start": str(INDEPENDENT_START.date()), "end": str(INDEPENDENT_END.date()),
            "days": int(pd.to_datetime(baseline.get("date", pd.Series(dtype="datetime64[ns]"))).dt.normalize().nunique()),
        },
        "validation": {"1D": r1, "2D": r2},
        "event_symbols": sorted(events.get("symbol", pd.Series(dtype=str)).astype(str).str.upper().unique().tolist()) if not events.empty else [],
        "regression_1D": reg,
        "top3_day_removed": tail,
        "chronological_blocks": blocks,
        "dte_buckets": _dte_bucket_reports(events, baseline),
        "concentration": _concentration(events),
        "mwpl_analysis": mwpl_analysis,
        "controls": {
            "historical_membership": "APPLIED" if controls.get("historical_membership_available") else "UNAVAILABLE",
            "historical_cash_price": "APPLIED" if controls.get("historical_cash_price_available") else "UNAVAILABLE",
            "lot_size_normalization": "APPLIED" if controls.get("lot_size_normalization_available") else "UNAVAILABLE",
            "mwpl_control": "APPLIED" if controls.get("mwpl_available") else "UNAVAILABLE",
            "realized_vol_control": "APPLIED" if "realized_vol20_prev" in baseline.columns else "UNAVAILABLE",
        },
        "gates": {
            "sample_ok": sample_ok, "lift_ok": lift_ok, "t_stat_ok": t_ok,
            "tail_ok": tail_ok, "stability_ok": stability_ok,
        },
    }
