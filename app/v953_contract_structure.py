"""V9.5.3 contract-structure feature research.

Measures whether point-in-time near/next/far futures OI structure contains
next-session *magnitude* information.  This is feature research only: it has
no trial number, no direction call and no production activation path.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from . import v95_daily_evidence as v95

BUILD_ID = v95.BUILD_ID

FEATURE_DEFINITIONS = {
    "fresh_near_creation": "near OI z>=1.5 and total OI z>=1.0",
    "rollover_dominant": "near OI z<=-1.0, next OI z>=1.0, |total OI z|<1.0",
    "fresh_total_expansion": "total OI z>=1.5",
    "abnormal_unwind": "total OI z<=-1.5",
}


def classify_from_z(frame: pd.DataFrame) -> dict[str, pd.Series]:
    near = pd.to_numeric(frame.get("near_z"), errors="coerce")
    nxt = pd.to_numeric(frame.get("next_z"), errors="coerce")
    total = pd.to_numeric(frame.get("total_z"), errors="coerce")
    return {
        "fresh_near_creation": (near >= 1.5) & (total >= 1.0),
        "rollover_dominant": (near <= -1.0) & (nxt >= 1.0) & (total.abs() < 1.0),
        "fresh_total_expansion": total >= 1.5,
        "abnormal_unwind": total <= -1.5,
    }


def build_contract_structure_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for src, stem in (("nse_near_oi", "near"), ("nse_next_oi", "next"), ("nse_total_oi", "total")):
        series = pd.to_numeric(out.get(src), errors="coerce")
        chg = series.pct_change(fill_method=None) * 100.0
        out[f"{stem}_chg_pct"] = chg
        out[f"{stem}_z"] = v95._rolling_z(chg, window=60, min_obs=20)
    masks = classify_from_z(out)
    for name, mask in masks.items():
        out[name] = mask.fillna(False).astype(bool)
    return out


def _stack_validation(frames: Mapping[str, pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    _, val_dates, final_dates = v95._partition_dates(frames)
    rows = []
    for symbol, frame in frames.items():
        if frame is None or frame.empty:
            continue
        x = build_contract_structure_frame(frame)
        x["date"] = pd.DatetimeIndex(x.index).normalize()
        x = x[x["date"].isin(val_dates)].copy()
        x["symbol"] = symbol
        rows.append(x.reset_index(drop=True))
    final_rows = int(sum(pd.DatetimeIndex(f.index).normalize().isin(final_dates).sum() for f in frames.values() if f is not None and not f.empty))
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), final_rows


def evaluate_contract_structure(symbol_frames: Mapping[str, pd.DataFrame], *, bootstrap_reps: int = 300) -> dict:
    validation, final_rows = _stack_validation(symbol_frames)
    if validation.empty:
        return {
            "build": BUILD_ID, "status": "NO_DATA", "research_only": True,
            "trial_number": None, "final_20_locked": True, "final_rows_locked": final_rows,
            "definitions": dict(FEATURE_DEFINITIONS), "features": {},
        }
    eligible = validation.get("eligible", True)
    if not isinstance(eligible, pd.Series):
        eligible = pd.Series(bool(eligible), index=validation.index)
    baseline = validation[eligible.fillna(False).astype(bool)].copy()
    baseline = baseline[baseline["movement_1d_atr"].notna()].copy()
    features = {}
    for name in FEATURE_DEFINITIONS:
        events = baseline[baseline.get(name, False).fillna(False).astype(bool)].copy() if name in baseline else baseline.iloc[0:0].copy()
        features[name] = {
            "definition": FEATURE_DEFINITIONS[name],
            "1D": v95._horizon_report(events, baseline, "movement_1d_atr", reps=bootstrap_reps),
            "2D": v95._horizon_report(events, baseline, "movement_2d_atr", reps=bootstrap_reps),
        }
    return {
        "build": BUILD_ID,
        "status": "FEATURE_RESEARCH_ONLY",
        "research_only": True,
        "directional_prediction": False,
        "trial_number": None,
        "final_20_locked": True,
        "final_rows_locked": final_rows,
        "validation_days": int(pd.to_datetime(baseline.get("date", pd.Series(dtype="datetime64[ns]")).dropna()).dt.normalize().nunique()),
        "definitions": dict(FEATURE_DEFINITIONS),
        "features": features,
        "message": "Exploratory feature decomposition after Trial 15 closure; cannot unlock Trial 16 or production playbooks.",
    }
