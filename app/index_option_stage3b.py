"""Stage 3B kill-test diagnostics for frozen NIFTY index-option research.

Research-only. This module does not change the frozen Stage-3 signal, does not
place orders, and does not promote any production playbook.

Stage 3B is deliberately a falsification layer over the already-viewed Stage-3
historical proxy ledger. Historical Dhan OHLC remains NON-EXECUTABLE evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .index_option_stage3 import FROZEN_SPEC_SHA256, evaluate_frozen_spec, session_bootstrap_mean_ci


STAGE3B_PREREG_DATE = "2026-09-22"
SOURCE_STAGE3_HEAD = "21b23934124980c7c2f4bf36d49c7d7a33fe7d70"
PRIMARY_EXPRESSION = "ITM1"
COMPARATOR_EXPRESSION = "ATM"

WINDOW_START = pd.Timestamp("2021-09-22")
WINDOW_END = pd.Timestamp("2026-08-31")
DEV_END = pd.Timestamp("2023-12-31")
VALIDATION_START = pd.Timestamp("2024-01-01")
VALIDATION_END = pd.Timestamp("2025-12-31")
HOLDOUT_START = pd.Timestamp("2026-01-01")
HOLDOUT_END = WINDOW_END

EARLY_KILL_GROSS_FLOOR_POINTS = 1.50
PILOT_NET_OVERALL_POINTS = 1.00
MIN_FRICTION_SAMPLES = 20
TRIM_FRACTION_EACH_TAIL = 0.05

REFINEMENT_NAME = "EXCLUDE_EXPIRY_DAY"
MAX_REFINEMENT_ROUNDS = 1

PROXY_CLASS = "PROXY_OHLC_NO_BID_ASK"


def _split_label(ts: pd.Timestamp) -> str | None:
    ts = pd.Timestamp(ts).normalize()
    if WINDOW_START <= ts <= DEV_END:
        return "DEV_2021_09_22_TO_2023"
    if VALIDATION_START <= ts <= VALIDATION_END:
        return "VALIDATION_2024_2025"
    if HOLDOUT_START <= ts <= HOLDOUT_END:
        return "HOLDOUT_2026_TO_AUG31"
    return None


def validate_proxy_ledger(frame: pd.DataFrame) -> pd.DataFrame:
    """Fail closed if historical proxy data is mislabeled as executable."""
    if frame is None or frame.empty:
        raise ValueError("historical proxy ledger is empty")
    required = {"session", "moneyness", "status", "premium_points"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"historical proxy ledger missing columns: {missing}")

    out = frame.copy()
    out["session"] = pd.to_datetime(out["session"], errors="coerce").dt.normalize()
    if out["session"].isna().any():
        raise ValueError("historical proxy ledger contains invalid session dates")

    if "executable" in out.columns and out["executable"].fillna(False).astype(bool).any():
        raise ValueError("Stage3B refuses executable=True in Dhan historical proxy ledger")
    if (
        "can_satisfy_stage3_executable_gate" in out.columns
        and out["can_satisfy_stage3_executable_gate"].fillna(False).astype(bool).any()
    ):
        raise ValueError("historical proxy must never satisfy Stage-3 executable gate")
    if "data_quality" in out.columns:
        observed = set(out["data_quality"].dropna().astype(str).unique())
        if observed and observed != {PROXY_CLASS}:
            raise ValueError(f"unexpected historical data_quality values: {sorted(observed)}")

    out["moneyness"] = out["moneyness"].astype(str).str.upper()
    out["premium_points"] = pd.to_numeric(out["premium_points"], errors="coerce")
    out["split"] = out["session"].map(_split_label)
    out = out[
        out["session"].between(WINDOW_START, WINDOW_END, inclusive="both")
    ].copy()
    return out.reset_index(drop=True)


def _ok_expression(frame: pd.DataFrame, expression: str) -> pd.DataFrame:
    work = frame[
        frame["status"].astype(str).eq("OK_PROXY")
        & frame["moneyness"].astype(str).str.upper().eq(str(expression).upper())
    ].copy()
    return work.dropna(subset=["premium_points"])


def _trimmed_mean(values: pd.Series, fraction_each_tail: float = TRIM_FRACTION_EACH_TAIL):
    x = pd.to_numeric(values, errors="coerce").dropna().sort_values().reset_index(drop=True)
    n = len(x)
    if n == 0:
        return None
    k = int(math.floor(n * float(fraction_each_tail)))
    if 2 * k >= n:
        return float(x.mean())
    return float(x.iloc[k:n-k].mean())


def _tail_row(values: pd.Series, fraction: float) -> dict:
    x = pd.to_numeric(values, errors="coerce").dropna().sort_values(ascending=False)
    n = len(x)
    if n == 0:
        return {
            "tail_fraction": fraction,
            "tail_count": 0,
            "tail_sum_points": None,
            "total_sum_points": None,
            "tail_share_of_total_pct": None,
        }
    k = max(1, int(math.ceil(n * float(fraction))))
    tail_sum = float(x.iloc[:k].sum())
    total = float(x.sum())
    return {
        "tail_fraction": float(fraction),
        "tail_count": int(k),
        "tail_sum_points": tail_sum,
        "total_sum_points": total,
        "tail_share_of_total_pct": float(tail_sum / total * 100.0) if total != 0 else None,
    }


def expression_summary(frame: pd.DataFrame, expression: str) -> dict:
    g = _ok_expression(frame, expression)
    x = g["premium_points"]
    ci = session_bootstrap_mean_ci(
        g.assign(session=g["session"].astype(str)),
        "premium_points",
        samples=5000,
        seed=3301,
    ) if not g.empty else {"mean": None, "ci_low": None, "ci_high": None, "n_sessions": 0}
    return {
        "moneyness": str(expression).upper(),
        "trade_count": int(len(x)),
        "session_count": int(g["session"].nunique()) if not g.empty else 0,
        "mean_gross_points": float(x.mean()) if len(x) else None,
        "median_gross_points": float(x.median()) if len(x) else None,
        "trimmed_mean_5pct_each_tail": _trimmed_mean(x),
        "win_rate_pct": float((x > 0).mean() * 100.0) if len(x) else None,
        "break_even_friction_points": float(x.mean()) if len(x) else None,
        "bootstrap_ci_low_points": ci.get("ci_low"),
        "bootstrap_ci_high_points": ci.get("ci_high"),
        "bootstrap_n_sessions": ci.get("n_sessions"),
    }


def split_summary(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for expression in (PRIMARY_EXPRESSION, COMPARATOR_EXPRESSION):
        base = _ok_expression(frame, expression)
        for split in (
            "DEV_2021_09_22_TO_2023",
            "VALIDATION_2024_2025",
            "HOLDOUT_2026_TO_AUG31",
        ):
            g = base[base["split"].eq(split)].copy()
            x = g["premium_points"]
            ci = session_bootstrap_mean_ci(
                g.assign(session=g["session"].astype(str)),
                "premium_points",
                samples=5000,
                seed=3302,
            ) if not g.empty else {"ci_low": None, "ci_high": None, "n_sessions": 0}
            rows.append({
                "moneyness": expression,
                "split": split,
                "trade_count": int(len(x)),
                "mean_gross_points": float(x.mean()) if len(x) else None,
                "median_gross_points": float(x.median()) if len(x) else None,
                "trimmed_mean_5pct_each_tail": _trimmed_mean(x),
                "win_rate_pct": float((x > 0).mean() * 100.0) if len(x) else None,
                "bootstrap_ci_low_points": ci.get("ci_low"),
                "bootstrap_ci_high_points": ci.get("ci_high"),
            })
    return pd.DataFrame.from_records(rows)


def year_direction_summary(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    ok = frame[frame["status"].astype(str).eq("OK_PROXY")].copy()
    ok["year"] = ok["session"].dt.year
    direction_col = "direction" if "direction" in ok.columns else None
    keys = ["moneyness", "year"] + ([direction_col] if direction_col else [])
    for group_key, g in ok.groupby(keys, dropna=False, sort=True):
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        mapping = dict(zip(keys, group_key))
        x = pd.to_numeric(g["premium_points"], errors="coerce").dropna()
        rows.append({
            "moneyness": mapping["moneyness"],
            "year": int(mapping["year"]),
            "direction": mapping.get("direction"),
            "trade_count": int(len(x)),
            "mean_gross_points": float(x.mean()) if len(x) else None,
            "median_gross_points": float(x.median()) if len(x) else None,
            "win_rate_pct": float((x > 0).mean() * 100.0) if len(x) else None,
        })
    return pd.DataFrame.from_records(rows)


def paired_atm_itm1(frame: pd.DataFrame) -> dict:
    ok = frame[
        frame["status"].astype(str).eq("OK_PROXY")
        & frame["moneyness"].isin([PRIMARY_EXPRESSION, COMPARATOR_EXPRESSION])
    ].copy()
    pivot = ok.pivot_table(
        index="session",
        columns="moneyness",
        values="premium_points",
        aggfunc="first",
    ).dropna(subset=[PRIMARY_EXPRESSION, COMPARATOR_EXPRESSION])
    if pivot.empty:
        return {"paired_sessions": 0}
    pivot["itm1_minus_atm_points"] = (
        pivot[PRIMARY_EXPRESSION] - pivot[COMPARATOR_EXPRESSION]
    )
    tmp = pivot.reset_index()[["session", "itm1_minus_atm_points"]].copy()
    ci = session_bootstrap_mean_ci(
        tmp.assign(session=tmp["session"].astype(str)),
        "itm1_minus_atm_points",
        samples=5000,
        seed=3303,
    )
    d = pivot["itm1_minus_atm_points"]
    return {
        "paired_sessions": int(len(d)),
        "mean_itm1_minus_atm_points": float(d.mean()),
        "median_itm1_minus_atm_points": float(d.median()),
        "itm1_better_session_pct": float((d > 0).mean() * 100.0),
        "bootstrap_ci_low_points": ci.get("ci_low"),
        "bootstrap_ci_high_points": ci.get("ci_high"),
        "selection_warning": (
            "ITM1 is an audit-declared challenger after Stage-3 aggregates were viewed; "
            "this paired result must not be presented as untouched instrument selection."
        ),
    }


def tail_concentration(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for expression in (PRIMARY_EXPRESSION, COMPARATOR_EXPRESSION):
        x = _ok_expression(frame, expression)["premium_points"]
        for fraction in (0.01, 0.05, 0.10):
            row = _tail_row(x, fraction)
            row["moneyness"] = expression
            rows.append(row)
    return pd.DataFrame.from_records(rows)


def summarize_friction(friction_log: pd.DataFrame | None) -> dict:
    """Primary friction estimator is preregistered as the arithmetic mean."""
    if friction_log is None or friction_log.empty:
        return {
            "status": "WAITING_FRICTION",
            "sample_count": 0,
            "minimum_samples": MIN_FRICTION_SAMPLES,
            "primary_estimator": "MEAN_ROUND_TRIP_FRICTION_POINTS",
        }
    if "round_trip_friction_points" not in friction_log.columns:
        raise ValueError("friction log missing round_trip_friction_points")
    work = friction_log.copy()
    if "moneyness" in work.columns:
        work = work[work["moneyness"].astype(str).str.upper().eq(PRIMARY_EXPRESSION)]
    x = pd.to_numeric(work["round_trip_friction_points"], errors="coerce").dropna()
    if (x < 0).any():
        raise ValueError("friction points must be non-negative")
    n = len(x)
    return {
        "status": "READY" if n >= MIN_FRICTION_SAMPLES else "WAITING_FRICTION",
        "sample_count": int(n),
        "minimum_samples": MIN_FRICTION_SAMPLES,
        "primary_estimator": "MEAN_ROUND_TRIP_FRICTION_POINTS",
        "mean_friction_points": float(x.mean()) if n else None,
        "median_friction_points": float(x.median()) if n else None,
        "p75_friction_points": float(x.quantile(0.75)) if n else None,
        "p90_friction_points": float(x.quantile(0.90)) if n else None,
    }


def _mean_for_dates(frame: pd.DataFrame, start: str, end: str, expression: str = PRIMARY_EXPRESSION):
    g = _ok_expression(frame, expression)
    mask = g["session"].between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both")
    x = g.loc[mask, "premium_points"]
    return float(x.mean()) if len(x) else None


def _net_mean(gross_mean: float | None, friction_points: float | None):
    if gross_mean is None or friction_points is None:
        return None
    return float(gross_mean - friction_points)


def banknifty_gross_replication(bars_1m: pd.DataFrame | None) -> dict:
    if bars_1m is None or bars_1m.empty:
        return {"status": "WAITING_REPLICATION", "trade_count": 0, "mean_120m_points": None}
    ledger = evaluate_frozen_spec(bars_1m, instrument="BANK NIFTY")
    x = pd.to_numeric(ledger.get("return_120m_points"), errors="coerce").dropna()
    return {
        "status": "READY",
        "trade_count": int(len(x)),
        "mean_120m_points": float(x.mean()) if len(x) else None,
        "positive_gross": bool(len(x) and float(x.mean()) > 0),
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "retuned": False,
    }


def decision_report(
    frame: pd.DataFrame,
    *,
    friction_log: pd.DataFrame | None = None,
    banknifty_replication: dict | None = None,
) -> dict:
    """Apply the preregistered Pilot/Park/Kill decision without retuning."""
    val_gross = _mean_for_dates(frame, "2024-01-01", "2025-12-31")
    hold_gross = _mean_for_dates(frame, "2026-01-01", "2026-08-31")
    combined_gross = _mean_for_dates(frame, "2024-01-01", "2026-08-31")
    friction = summarize_friction(friction_log)

    report = {
        "primary_expression": PRIMARY_EXPRESSION,
        "comparator_expression": COMPARATOR_EXPRESSION,
        "gross_validation_points": val_gross,
        "gross_holdout_points": hold_gross,
        "gross_2024_to_2026_points": combined_gross,
        "early_kill_floor_gross_points": EARLY_KILL_GROSS_FLOOR_POINTS,
        "friction": friction,
        "banknifty_replication": banknifty_replication or {
            "status": "WAITING_REPLICATION",
            "mean_120m_points": None,
        },
        "refinement_allowed": REFINEMENT_NAME,
        "max_refinement_rounds": MAX_REFINEMENT_ROUNDS,
        "production_deployed": False,
    }

    if combined_gross is not None and combined_gross <= EARLY_KILL_GROSS_FLOOR_POINTS:
        report.update({
            "decision": "KILL",
            "reason": "ITM1 2024-2026 zero-friction gross mean is at/below preregistered 1.50-point floor.",
        })
        return report

    if friction["status"] != "READY":
        report.update({
            "decision": "WAITING_FRICTION",
            "reason": f"Need at least {MIN_FRICTION_SAMPLES} ITM1 live friction observations.",
        })
        return report

    f = friction["mean_friction_points"]
    net_all = _net_mean(combined_gross, f)
    net_val = _net_mean(val_gross, f)
    net_hold = _net_mean(hold_gross, f)
    report.update({
        "net_2024_to_2026_points": net_all,
        "net_validation_points": net_val,
        "net_holdout_points": net_hold,
    })

    if net_all is not None and net_all <= 0:
        report.update({
            "decision": "KILL",
            "reason": "ITM1 historical mean net of measured friction is at/below zero.",
        })
        return report

    bank = report["banknifty_replication"]
    bank_ready = bank.get("status") == "READY" and bank.get("mean_120m_points") is not None
    if not bank_ready:
        report.update({
            "decision": "WAITING_REPLICATION",
            "reason": "NIFTY survived the net-cost kill test; BANK NIFTY zero-retune gross replication is still required.",
        })
        return report

    bank_positive = float(bank["mean_120m_points"]) > 0
    mixed_splits = (
        net_val is None or net_hold is None or net_val <= 0 or net_hold <= 0
    )
    if net_all is not None and net_all > PILOT_NET_OVERALL_POINTS and not mixed_splits and bank_positive:
        report.update({
            "decision": "PILOT",
            "reason": (
                "ITM1 mean net is above +1 point, validation and holdout are each positive, "
                "and BANK NIFTY gross replication is positive."
            ),
        })
        return report

    report.update({
        "decision": "PARK",
        "reason": (
            "Net expectancy is positive but <= +1 point, splits are mixed, "
            "or BANK NIFTY gross replication is non-positive."
        ),
    })
    return report


def apply_expiry_day_refinement(
    frame: pd.DataFrame,
    expiry_calendar: pd.DataFrame | None,
    *,
    friction_log: pd.DataFrame | None = None,
    banknifty_replication: dict | None = None,
) -> dict:
    """The one and only allowed refinement: exclude predeclared expiry-day sessions."""
    if expiry_calendar is None or expiry_calendar.empty:
        return {
            "status": "WAITING_EXPIRY_CALENDAR",
            "refinement": REFINEMENT_NAME,
            "decision": None,
        }
    required = {"session", "is_expiry_day"}
    missing = sorted(required.difference(expiry_calendar.columns))
    if missing:
        raise ValueError(f"expiry calendar missing columns: {missing}")
    cal = expiry_calendar.copy()
    cal["session"] = pd.to_datetime(cal["session"], errors="coerce").dt.normalize()
    raw_expiry = cal["is_expiry_day"]
    if raw_expiry.dtype == bool:
        parsed_expiry = raw_expiry
    else:
        normalized = raw_expiry.astype(str).str.strip().str.lower()
        mapping = {
            "true": True, "1": True, "yes": True, "y": True,
            "false": False, "0": False, "no": False, "n": False,
        }
        parsed_expiry = normalized.map(mapping)
        if parsed_expiry.isna().any():
            raise ValueError("expiry calendar contains unparseable is_expiry_day values")
    cal["is_expiry_day"] = parsed_expiry.astype(bool)
    work = frame.merge(cal[["session", "is_expiry_day"]], on="session", how="left")
    if work["is_expiry_day"].isna().any():
        return {
            "status": "WAITING_COMPLETE_EXPIRY_CALENDAR",
            "refinement": REFINEMENT_NAME,
            "decision": None,
        }
    filtered = work[~work["is_expiry_day"]].drop(columns=["is_expiry_day"])
    out = decision_report(
        filtered,
        friction_log=friction_log,
        banknifty_replication=banknifty_replication,
    )
    out["status"] = "COMPLETE"
    out["refinement"] = REFINEMENT_NAME
    out["refinement_round"] = 1
    out["cannot_overwrite_base_decision"] = True
    return out


def alignment_check(
    dhan_proxy_rows: pd.DataFrame,
    kite_nifty_1m: pd.DataFrame,
    *,
    sessions: int = 5,
) -> dict:
    """Deterministic 5-session timestamp check using evenly spaced common sessions."""
    if dhan_proxy_rows is None or dhan_proxy_rows.empty or kite_nifty_1m is None or kite_nifty_1m.empty:
        return {"status": "WAITING_DATA"}
    if "timestamp" not in dhan_proxy_rows.columns or "spot" not in dhan_proxy_rows.columns:
        raise ValueError("Dhan rows require timestamp and spot")
    if "timestamp" in kite_nifty_1m.columns:
        kite = kite_nifty_1m.copy()
        kite["timestamp"] = pd.to_datetime(kite["timestamp"], errors="coerce")
    else:
        kite = kite_nifty_1m.reset_index().rename(columns={kite_nifty_1m.index.name or "index": "timestamp"})
        kite["timestamp"] = pd.to_datetime(kite["timestamp"], errors="coerce")
    close_col = "close" if "close" in kite.columns else "spot"
    if close_col not in kite.columns:
        raise ValueError("Kite 1m bars require close or spot")

    dhan = dhan_proxy_rows.copy()
    dhan["timestamp"] = pd.to_datetime(dhan["timestamp"], errors="coerce").dt.floor("min")
    kite["timestamp"] = kite["timestamp"].dt.floor("min")
    dhan["session"] = dhan["timestamp"].dt.normalize()
    kite["session"] = kite["timestamp"].dt.normalize()
    common = sorted(set(dhan["session"].dropna()).intersection(set(kite["session"].dropna())))
    if not common:
        return {"status": "NO_COMMON_SESSIONS"}
    if len(common) <= sessions:
        chosen = common
    else:
        idx = np.linspace(0, len(common) - 1, sessions).round().astype(int)
        chosen = [common[i] for i in idx]
    d = dhan[dhan["session"].isin(chosen)][["timestamp", "spot"]].drop_duplicates("timestamp")
    k = kite[kite["session"].isin(chosen)][["timestamp", close_col]].drop_duplicates("timestamp")
    merged = d.merge(k, on="timestamp", how="inner")
    if merged.empty:
        return {"status": "NO_MATCHED_MINUTES", "sessions": [str(x.date()) for x in chosen]}
    diff = (pd.to_numeric(merged["spot"], errors="coerce") - pd.to_numeric(merged[close_col], errors="coerce")).abs().dropna()
    return {
        "status": "COMPLETE",
        "sessions": [str(x.date()) for x in chosen],
        "matched_minutes": int(len(diff)),
        "mean_abs_spot_difference_points": float(diff.mean()) if len(diff) else None,
        "median_abs_spot_difference_points": float(diff.median()) if len(diff) else None,
        "p95_abs_spot_difference_points": float(diff.quantile(0.95)) if len(diff) else None,
        "max_abs_spot_difference_points": float(diff.max()) if len(diff) else None,
        "selection_method": "5 evenly spaced common sessions; no cherry-picking",
    }


def write_stage3b_artifacts(
    output_dir,
    *,
    proxy_ledger: pd.DataFrame,
    friction_log: pd.DataFrame | None = None,
    banknifty_replication: dict | None = None,
    expiry_calendar: pd.DataFrame | None = None,
    source_files: Iterable[str | Path] = (),
) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    validated = validate_proxy_ledger(proxy_ledger)
    overall = pd.DataFrame.from_records([
        expression_summary(validated, PRIMARY_EXPRESSION),
        expression_summary(validated, COMPARATOR_EXPRESSION),
    ])
    splits = split_summary(validated)
    years = year_direction_summary(validated)
    tails = tail_concentration(validated)
    paired = paired_atm_itm1(validated)
    decision = decision_report(
        validated,
        friction_log=friction_log,
        banknifty_replication=banknifty_replication,
    )
    refinement = apply_expiry_day_refinement(
        validated,
        expiry_calendar,
        friction_log=friction_log,
        banknifty_replication=banknifty_replication,
    )

    files = {
        "overall": output / "stage3b_overall_summary.csv",
        "splits": output / "stage3b_split_summary.csv",
        "year_direction": output / "stage3b_year_direction.csv",
        "tails": output / "stage3b_tail_concentration.csv",
        "paired": output / "stage3b_paired_atm_itm1.json",
        "decision": output / "stage3b_decision.json",
        "refinement": output / "stage3b_refinement_expiry_exclusion.json",
    }
    overall.to_csv(files["overall"], index=False)
    splits.to_csv(files["splits"], index=False)
    years.to_csv(files["year_direction"], index=False)
    tails.to_csv(files["tails"], index=False)
    files["paired"].write_text(json.dumps(paired, indent=2, sort_keys=True, default=str), encoding="utf-8")
    files["decision"].write_text(json.dumps(decision, indent=2, sort_keys=True, default=str), encoding="utf-8")
    files["refinement"].write_text(json.dumps(refinement, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def sha(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    manifest = {
        "research_stage": "Index Option Buying V1 / Stage 3B kill test",
        "preregistered_on": STAGE3B_PREREG_DATE,
        "source_stage3_head": SOURCE_STAGE3_HEAD,
        "frozen_spec_sha256": FROZEN_SPEC_SHA256,
        "historical_data_class": PROXY_CLASS,
        "historical_executable_gate_satisfied": False,
        "primary_expression": PRIMARY_EXPRESSION,
        "comparator_expression": COMPARATOR_EXPRESSION,
        "window": [str(WINDOW_START.date()), str(WINDOW_END.date())],
        "splits": {
            "dev": ["2021-09-22", "2023-12-31"],
            "validation": ["2024-01-01", "2025-12-31"],
            "holdout": ["2026-01-01", "2026-08-31"],
        },
        "decision_thresholds": {
            "early_kill_gross_2024_2026_le_points": EARLY_KILL_GROSS_FLOOR_POINTS,
            "pilot_net_2024_2026_gt_points": PILOT_NET_OVERALL_POINTS,
            "pilot_validation_net_gt_points": 0.0,
            "pilot_holdout_net_gt_points": 0.0,
            "pilot_banknifty_gross_gt_points": 0.0,
            "kill_net_2024_2026_le_points": 0.0,
            "minimum_friction_samples": MIN_FRICTION_SAMPLES,
            "primary_friction_estimator": "arithmetic mean round_trip_friction_points",
        },
        "allowed_refinement": REFINEMENT_NAME,
        "max_refinement_rounds": MAX_REFINEMENT_ROUNDS,
        "source_files": [str(Path(x)) for x in source_files],
        "artifacts": {
            name: {"file": path.name, "sha256": sha(path)}
            for name, path in files.items()
        },
        "research_only": True,
        "production_deployed": False,
        "v12_v121_recorder_modified": False,
        "trial25_modified": False,
        "notes": [
            "Stage 3 remains frozen and is not overwritten by Stage 3B.",
            "ITM1 is an audit-declared challenger after Stage-3 aggregates were viewed; it is not an untouched instrument-selection result.",
            "ATM remains a comparator and is not discarded.",
            "No OR length, trigger clock, confirmation, range gate, buffer, stop or 120-minute holding period may be retuned.",
            "Historical Dhan OHLC cannot satisfy any executable bid/ask gate.",
        ],
    }
    manifest_path = output / "stage3b_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "decision": decision,
        "refinement": refinement,
        "artifact_paths": files,
        **{f"{name}_path": path for name, path in files.items()},
    }
