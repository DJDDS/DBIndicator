"""V9.8 incremental validation for the frozen Trial-19 extreme-OI event.

This module does not alter Trial 19.  It asks whether the already-frozen
``total FUTSTK OI z >= 1.5`` binary event adds next-session variance
information beyond a standard HAR volatility state, abnormal FUTSTK volume
and the earnings calendar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import v96_trial17 as v96
from . import v97_trial19 as v97

BUILD_ID = "2026-09-03-INSTITUTIONAL-V9.8.0-INCREMENTAL-OI-VALIDATION"
T_STAT_HURDLE = 3.0
MIN_TARGET_COVERAGE = 0.90
VAR_SCALE = 1_000_000.0


def spec() -> dict:
    return {
        "name": "Incremental OI Validation",
        "frozen_event": "total FUTSTK OI z >= 1.5",
        "window": [str(v97.INDEPENDENT_START.date()), str(v97.INDEPENDENT_END.date())],
        "primary_target": "next_yz_var",
        "robustness_target": "next_gk_var",
        "benchmark": "HAR_DAILY_WEEKLY_MONTHLY_PLUS_ABNORMAL_FUTSTK_VOLUME",
        "post_control_t_hurdle": T_STAT_HURDLE,
        "trial18_locked": True,
        "production_activation": False,
    }


def _stack(symbol_frames):
    return v97._stack(symbol_frames)


def _efficacy_ok(frozen_result: dict) -> bool:
    gates = dict((frozen_result or {}).get("gates") or {})
    return all(bool(gates.get(k)) for k in ("sample_ok", "matched_lift_ok", "binary_event_t_ok", "tail_ok", "stability_ok"))


def _eligible_frame(symbol_frames) -> pd.DataFrame:
    x = _stack(symbol_frames)
    if x is None or x.empty:
        return pd.DataFrame()
    flag = x.get("trial19_eligible", False)
    if not isinstance(flag, pd.Series):
        flag = pd.Series(bool(flag), index=x.index)
    x = x.loc[flag.fillna(False).astype(bool)].copy()
    x["date"] = pd.to_datetime(x["date"]).dt.normalize()
    if "dte_bucket" not in x:
        x["dte_bucket"] = v97._dte_bucket(x)
    x["extreme_oi_event"] = x.get("extreme_oi_event", False).fillna(False).astype(bool)
    return x


def _within_date_dte(frame: pd.DataFrame, target: str, columns: list[str]):
    use = frame[["date", "dte_bucket", "symbol", target] + columns].copy()
    use[target] = pd.to_numeric(use[target], errors="coerce") * VAR_SCALE
    for c in columns:
        if c == "extreme_oi_event":
            use[c] = use[c].fillna(False).astype(float)
        elif c.startswith("har_"):
            use[c] = pd.to_numeric(use[c], errors="coerce") * VAR_SCALE
        else:
            use[c] = pd.to_numeric(use[c], errors="coerce")
    valid = use[target].notna() & use[columns].notna().all(axis=1) & use["date"].notna() & use["dte_bucket"].notna() & use["symbol"].notna()
    use = use.loc[valid].copy()
    if use.empty:
        return use, pd.Series(dtype=float), pd.DataFrame(columns=columns)
    g = use.groupby(["date", "dte_bucket"], observed=True)
    y = use[target] - g[target].transform("mean")
    X = pd.DataFrame(index=use.index)
    for c in columns:
        X[c] = use[c] - g[c].transform("mean")
    return use, y, X


def _ols_r2(y: pd.Series, X: pd.DataFrame) -> float | None:
    if y is None or X is None or len(y) <= X.shape[1] + 2:
        return None
    valid = y.notna() & X.notna().all(axis=1)
    if int(valid.sum()) <= X.shape[1] + 2:
        return None
    yv = y.loc[valid].to_numpy(dtype=float)
    A = np.column_stack([np.ones(len(yv)), X.loc[valid].to_numpy(dtype=float)])
    beta = np.linalg.pinv(A) @ yv
    resid = yv - A @ beta
    sse = float(np.dot(resid, resid))
    centered = yv - float(np.mean(yv))
    sst = float(np.dot(centered, centered))
    if not np.isfinite(sst) or sst <= 1e-18:
        return None
    return float(1.0 - sse / sst)


def _regression(frame: pd.DataFrame, target: str, columns: list[str]) -> dict:
    use, y, X = _within_date_dte(frame, target, columns)
    if use.empty:
        return {"n": 0, "date_clusters": 0, "symbol_clusters": 0, "coef": {}, "se": {}, "t": {}, "r2": None, "columns": list(columns)}
    report = v96.two_way_cluster_robust_ols(y, X, use["date"], use["symbol"])
    report["r2"] = _ols_r2(y, X)
    report["columns"] = list(columns)
    return report


def _incremental_r2(frame: pd.DataFrame, target: str) -> dict:
    base_cols = ["har_daily_var", "har_weekly_var", "har_monthly_var", "futures_volume_z"]
    aug_cols = base_cols + ["extreme_oi_event"]
    _, yb, Xb = _within_date_dte(frame, target, base_cols)
    _, ya, Xa = _within_date_dte(frame, target, aug_cols)
    base_r2 = _ols_r2(yb, Xb)
    aug_r2 = _ols_r2(ya, Xa)
    delta = None if base_r2 is None or aug_r2 is None else float(aug_r2 - base_r2)
    return {"base_r2": base_r2, "augmented_r2": aug_r2, "delta_r2": delta, "base": "HAR+VOLUME", "augmented": "HAR+VOLUME+EXTREME_OI"}


def _matched_target(frame: pd.DataFrame, field: str, *, reps: int, seed: int) -> dict:
    valid = frame[pd.to_numeric(frame.get(field), errors="coerce").notna()].copy()
    events = valid[valid["extreme_oi_event"].fillna(False).astype(bool)].copy()
    return v97.same_day_dte_matched_report(events, valid, field, reps=reps, seed=seed)


def evaluate_incremental_core(symbol_frames, *, frozen_result: dict, bootstrap_reps: int = 500) -> dict:
    if not _efficacy_ok(frozen_result):
        return {"build": BUILD_ID, "status": "LOCKED_TRIAL19_EFFICACY_NOT_PASSED", "pass": False, "trial18_state": "LOCKED", "research_only": True}
    frame = _eligible_frame(symbol_frames)
    if frame.empty:
        return {"build": BUILD_ID, "status": "INCONCLUSIVE_NO_DATA", "pass": False, "trial18_state": "LOCKED", "research_only": True}

    required = ["next_yz_var", "next_gk_var", "har_daily_var", "har_weekly_var", "har_monthly_var", "futures_volume_z"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        return {"build": BUILD_ID, "status": "INCONCLUSIVE_MISSING_INCREMENTAL_FIELDS", "pass": False, "missing_fields": missing, "trial18_state": "LOCKED", "research_only": True}

    total = max(len(frame), 1)
    yz_valid = pd.to_numeric(frame["next_yz_var"], errors="coerce").notna()
    vol_valid = pd.to_numeric(frame["futures_volume_z"], errors="coerce").notna()
    har_valid = frame[["har_daily_var", "har_weekly_var", "har_monthly_var"]].apply(pd.to_numeric, errors="coerce").notna().all(axis=1)
    coverage = {
        "next_yz_var": float(yz_valid.mean()),
        "next_gk_var": float(pd.to_numeric(frame["next_gk_var"], errors="coerce").notna().mean()),
        "har": float(har_valid.mean()),
        "futures_volume_z": float(vol_valid.mean()),
        "joint": float((yz_valid & vol_valid & har_valid).sum() / total),
    }

    har_cols = ["har_daily_var", "har_weekly_var", "har_monthly_var", "extreme_oi_event"]
    joint_cols = ["har_daily_var", "har_weekly_var", "har_monthly_var", "futures_volume_z", "extreme_oi_event"]
    har_oi = _regression(frame, "next_yz_var", har_cols)
    joint = _regression(frame, "next_yz_var", joint_cols)
    gk_joint = _regression(frame, "next_gk_var", joint_cols)
    inc = _incremental_r2(frame, "next_yz_var")
    yz_match = _matched_target(frame, "next_yz_var", reps=bootstrap_reps, seed=9801)
    gk_match = _matched_target(frame, "next_gk_var", reps=bootstrap_reps, seed=9802)

    oi_coef = (joint.get("coef") or {}).get("extreme_oi_event")
    oi_t = (joint.get("t") or {}).get("extreme_oi_event")
    coverage_ok = bool(coverage["joint"] >= MIN_TARGET_COVERAGE)
    oi_ok = bool(oi_coef is not None and np.isfinite(oi_coef) and oi_coef > 0 and oi_t is not None and np.isfinite(oi_t) and oi_t >= T_STAT_HURDLE)
    r2_ok = bool(inc.get("delta_r2") is not None and np.isfinite(inc["delta_r2"]) and inc["delta_r2"] > 0)
    variance_ok = bool((yz_match.get("lift") or 0) > 1.0 and (yz_match.get("ci95_low") or 0) > 1.0 and (gk_match.get("lift") or 0) > 1.0)
    passed = coverage_ok and oi_ok and r2_ok and variance_ok
    if not coverage_ok:
        status = "INCONCLUSIVE_INCREMENTAL_COVERAGE"
    elif not variance_ok:
        status = "FAIL_VARIANCE_TARGET"
    elif not oi_ok:
        status = "FAIL_OI_NOT_INCREMENTAL_AFTER_HAR_VOLUME"
    elif not r2_ok:
        status = "FAIL_NO_INCREMENTAL_R2"
    else:
        status = "PASS_INCREMENTAL_CORE"

    return {
        "build": BUILD_ID,
        "spec": spec(),
        "status": status,
        "pass": passed,
        "primary_target": "next_yz_var",
        "coverage": coverage,
        "matched_variance": {"yang_zhang": yz_match, "garman_klass": gk_match},
        "har_plus_oi": har_oi,
        "har_volume_oi": joint,
        "gk_har_volume_oi": gk_joint,
        "incremental_r2": inc,
        "gates": {"coverage_ok": coverage_ok, "variance_target_ok": variance_ok, "post_har_volume_oi_t_ok": oi_ok, "incremental_r2_ok": r2_ok},
        "trial18_state": "LOCKED",
        "research_only": True,
        "production_activation": False,
    }


def finalize_v98(core: dict, *, earnings_control: dict | None) -> dict:
    core = dict(core or {})
    earnings_control = dict(earnings_control or {})
    if not core.get("pass"):
        status = str(core.get("status") or "INCONCLUSIVE_INCREMENTAL_CORE")
    elif not earnings_control.get("audit_valid"):
        status = "INCONCLUSIVE_EARNINGS_JOIN"
    elif not earnings_control.get("outside_earnings_pass"):
        status = "FAIL_EARNINGS_CONFOUND"
    else:
        status = "PASS_INCREMENTAL_OI"
    return {
        "build": BUILD_ID,
        "status": status,
        "pass": status == "PASS_INCREMENTAL_OI",
        "core": core,
        "earnings": earnings_control,
        "trial18_state": "LOCKED",
        "eligible_for_direction_preregistration": False,
        "research_only": True,
        "production_activation": False,
    }


MIN_EARNINGS_SYMBOL_DATE_COVERAGE = 0.80


def _canonical_symbol(value) -> str:
    return "".join(ch for ch in str(value).strip().upper() if ch.isalnum())


def _earnings_window_flags(frame: pd.DataFrame, earnings_map: dict, radius: int = 5):
    x = frame.copy()
    x["date"] = pd.to_datetime(x["date"]).dt.normalize()
    x["symbol"] = x["symbol"].astype(str).str.upper()
    by_canon = {}
    for key, dates in dict(earnings_map or {}).items():
        if key == "_meta" or dates is None:
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(list(dates), errors="coerce")).dropna().normalize().unique().sort_values()
        if len(idx):
            by_canon.setdefault(_canonical_symbol(key), []).extend(pd.Timestamp(d).normalize() for d in idx)
    flags = pd.Series(False, index=x.index, dtype=bool)
    nearest = {}
    matched_symbols = set()
    examples = []
    for sym, grp in x.groupby("symbol", sort=False):
        dates = sorted(set(by_canon.get(_canonical_symbol(sym), [])))
        if not dates:
            continue
        calendar = pd.DatetimeIndex(sorted(pd.to_datetime(grp["date"]).dt.normalize().unique()))
        if calendar.empty:
            continue
        positions = {pd.Timestamp(d).normalize(): i for i, d in enumerate(calendar)}
        matched_this_symbol = False
        for earn_date in dates:
            # Ignore result dates that are clearly outside the observed symbol
            # calendar.  A small buffer allows the +/-5-session window to
            # reach across the evidence boundary without anchoring a date
            # months away to the last available row.
            if earn_date < calendar[0] - pd.Timedelta(days=14) or earn_date > calendar[-1] + pd.Timedelta(days=14):
                continue
            matched_this_symbol = True
            pos = int(calendar.searchsorted(earn_date))
            # Anchor a non-trading filing/meeting date to the first observed
            # session on/after it, then include +/- radius observed sessions.
            anchor = min(pos, len(calendar) - 1)
            lo = max(0, anchor - int(radius)); hi = min(len(calendar), anchor + int(radius) + 1)
            window = set(pd.Timestamp(d).normalize() for d in calendar[lo:hi])
            mask = grp["date"].isin(window)
            flags.loc[grp.index[mask]] = True
            for idx in grp.index[mask]:
                dd = pd.Timestamp(x.at[idx, "date"]).normalize()
                dist = positions.get(dd, anchor) - anchor
                nearest[idx] = (earn_date, int(dist))
        if matched_this_symbol:
            matched_symbols.add(sym)
    event_flag = x.get("extreme_oi_event", False)
    if not isinstance(event_flag, pd.Series):
        event_flag = pd.Series(bool(event_flag), index=x.index)
    for idx in x.index[flags & event_flag.fillna(False).astype(bool)][:10]:
        earn_date, dist = nearest.get(idx, (None, None))
        examples.append({
            "symbol": str(x.at[idx, "symbol"]),
            "event_date": str(pd.Timestamp(x.at[idx, "date"]).date()),
            "earnings_date": str(pd.Timestamp(earn_date).date()) if earn_date is not None else None,
            "session_distance": dist,
        })
    return flags, matched_symbols, examples


def evaluate_earnings_split(symbol_frames, *, frozen_result: dict, earnings_map=None, bootstrap_reps: int = 500) -> dict:
    if not _efficacy_ok(frozen_result):
        return {"status": "LOCKED_TRIAL19_EFFICACY_NOT_PASSED", "audit": {"audit_valid": False}, "outside_earnings_pass": False, "research_only": True}
    frame = _eligible_frame(symbol_frames)
    if frame.empty or "next_yz_var" not in frame:
        return {"status": "INCONCLUSIVE_NO_DATA", "audit": {"audit_valid": False}, "outside_earnings_pass": False, "research_only": True}
    emap = dict(earnings_map or {})
    meta = dict(emap.get("_meta") or {})
    flags, matched_symbols, examples = _earnings_window_flags(frame, emap, radius=5)
    frame["earnings_window"] = flags
    event_flag = frame["extreme_oi_event"].fillna(False).astype(bool)
    overlap = int((event_flag & flags).sum())
    requested = int(meta.get("symbols_requested") or len({k for k in emap if k != "_meta"}))
    symbols_with_dates = int(meta.get("symbols_with_dates") or sum(1 for k, v in emap.items() if k != "_meta" and v is not None and len(v)))
    result_dates = int(meta.get("result_dates_loaded") or sum(len(v) for k, v in emap.items() if k != "_meta" and v is not None))
    symbol_date_coverage = float(meta.get("symbol_date_coverage") if meta.get("symbol_date_coverage") is not None else (symbols_with_dates / requested if requested else 0.0))
    audit_valid = bool(requested > 0 and symbol_date_coverage >= MIN_EARNINGS_SYMBOL_DATE_COVERAGE and result_dates > 0 and len(matched_symbols) > 0 and overlap > 0)

    inside = frame.loc[flags].copy()
    outside = frame.loc[~flags].copy()
    inside_events = inside[inside["extreme_oi_event"].fillna(False).astype(bool)].copy()
    outside_events = outside[outside["extreme_oi_event"].fillna(False).astype(bool)].copy()
    inside_match = _matched_target(inside, "next_yz_var", reps=bootstrap_reps, seed=9811) if len(inside) else {"event_count": 0, "lift": None, "ci95_low": None, "ci95_high": None}
    outside_match = _matched_target(outside, "next_yz_var", reps=bootstrap_reps, seed=9812) if len(outside) else {"event_count": 0, "lift": None, "ci95_low": None, "ci95_high": None}
    outside_pass = bool(audit_valid and (outside_match.get("lift") or 0) > 1.0 and (outside_match.get("ci95_low") or 0) > 1.0)
    status = "PASS_EARNINGS_SPLIT" if outside_pass else ("INCONCLUSIVE_EARNINGS_JOIN" if not audit_valid else "FAIL_EARNINGS_CONFOUND")
    audit = {
        "audit_valid": audit_valid,
        "symbols_requested": requested,
        "symbols_loaded": int(meta.get("symbols_loaded") or 0),
        "symbols_with_dates": symbols_with_dates,
        "symbol_date_coverage": symbol_date_coverage,
        "result_dates_loaded": result_dates,
        "matched_symbol_count": int(len(matched_symbols)),
        "event_overlap_count": overlap,
        "event_overlap_fraction": float(overlap / max(int(event_flag.sum()), 1)),
        "examples": examples,
    }
    return {
        "status": status,
        "audit": audit,
        "event_overlap_count": overlap,
        "inside_earnings": {"event_count": int(len(inside_events)), "eligible_rows": int(len(inside)), "matched": inside_match},
        "outside_earnings": {"event_count": int(len(outside_events)), "eligible_rows": int(len(outside)), "matched": outside_match},
        "outside_earnings_pass": outside_pass,
        "research_only": True,
        "production_activation": False,
    }
