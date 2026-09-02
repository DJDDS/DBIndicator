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

BUILD_ID = "2026-09-02-INSTITUTIONAL-V9.6.0-TRIAL17-INDEPENDENT-TOTAL-OI"
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
        "regression_1D": reg,
        "top3_day_removed": tail,
        "chronological_blocks": blocks,
        "dte_buckets": _dte_bucket_reports(events, baseline),
        "concentration": _concentration(events),
        "mwpl_analysis": mwpl_analysis,
        "controls": {
            "historical_membership": "APPLIED" if controls.get("historical_membership_available") else "UNAVAILABLE",
            "lot_size_normalization": "APPLIED" if controls.get("lot_size_normalization_available") else "UNAVAILABLE",
            "mwpl_control": "APPLIED" if controls.get("mwpl_available") else "UNAVAILABLE",
            "realized_vol_control": "APPLIED" if "realized_vol20_prev" in baseline.columns else "UNAVAILABLE",
        },
        "gates": {
            "sample_ok": sample_ok, "lift_ok": lift_ok, "t_stat_ok": t_ok,
            "tail_ok": tail_ok, "stability_ok": stability_ok,
        },
    }
