"""V11 feasibility competition and Trial-24 residual-momentum replication.

The feasibility competition reads no alpha outcomes.  It compares a pinned
published residual-momentum prior with the redesigned basis candidate and only
registers a runnable trial when the pre-trial gate permits it.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from . import research_feasibility

BUILD_ID = "2026-09-04-INSTITUTIONAL-V11.0.2-IIMA-MF-SCHEMA-HOTFIX"
CONFIRMATORY_T_BAR = 1.645
CONFIRMATORY_T_BAR_NAME = "ONE_SIDED_5PCT_EXACT_PUBLISHED_REPLICATION"
DISCOVERY_T_BAR = 3.25
DISCOVERY_T_BAR_NAME = "MULTIPLICITY_ADJUSTED_DISCOVERY_BAR"
RESIDUAL_MOMENTUM_ANNUAL_RETURN = 0.1120
RESIDUAL_MOMENTUM_ANNUAL_VOL = 0.1249
RESIDUAL_MOMENTUM_MONTHLY_GROSS_PRIOR = RESIDUAL_MOMENTUM_ANNUAL_RETURN / 12.0
TRIAL24_SPREAD_ROUND_TRIP_COST = 0.0036
PLANNED_EVIDENCE_MONTHS = 156
PLANNED_CONFIRMATORY_MONTHS = int(math.floor(PLANNED_EVIDENCE_MONTHS * 0.80))


def trial24_spec() -> dict:
    return {
        "trial": 24,
        "name": "Published Residual Momentum Replication",
        "formation": "12-1M",
        "beta_window_months": 36,
        "factors": "INDIA_FF3_IIMA_PINNED_SURVIVORSHIP_ADJUSTED",
        "holding_months": 1,
        "rebalance": "MONTHLY",
        "portfolio": "TOP_MINUS_BOTTOM_DECILE_200PCT_GROSS",
        "primary_estimand": "DAY_WEIGHTED_FIXED_CAPITAL_MONTHLY_PORTFOLIO_NET",
        "spread_round_trip_cost": TRIAL24_SPREAD_ROUND_TRIP_COST,
        "confirmatory_t_bar": CONFIRMATORY_T_BAR,
        "confirmatory_t_bar_name": CONFIRMATORY_T_BAR_NAME,
        "final_holdout_pct": 20,
        "final_read": False,
        "production_activation": False,
        "parameter_search": False,
    }


def feasibility_competition() -> dict:
    sigma_month = RESIDUAL_MOMENTUM_ANNUAL_VOL / math.sqrt(12.0)
    a = research_feasibility.assess_pretrial_feasibility(
        prior_gross_effect=RESIDUAL_MOMENTUM_MONTHLY_GROSS_PRIOR,
        round_trip_cost=TRIAL24_SPREAD_ROUND_TRIP_COST,
        sigma_day=sigma_month,
        effective_days=PLANNED_CONFIRMATORY_MONTHS,
        t_bar=CONFIRMATORY_T_BAR,
        source="Blitz-Huij-Martens Residual Momentum canonical 1M specification",
        horizon="1_MONTH",
        t_bar_name=CONFIRMATORY_T_BAR_NAME,
    )
    b = research_feasibility.assess_pretrial_feasibility(
        prior_gross_effect=None,
        round_trip_cost=TRIAL24_SPREAD_ROUND_TRIP_COST,
        sigma_day=None,
        effective_days=None,
        t_bar=DISCOVERY_T_BAR,
        source=None,
        horizon="10_TO_21_TRADING_DAYS",
        t_bar_name=DISCOVERY_T_BAR_NAME,
    )
    winner = "TRIAL24_RESIDUAL_MOMENTUM_REPLICATION" if a.get("feasible") and not b.get("feasible") else None
    return {
        "build": BUILD_ID,
        "outcome_data_read": False,
        "candidate_a": {
            "name": "PUBLISHED_RESIDUAL_MOMENTUM_12_1",
            "prior_annual_return": RESIDUAL_MOMENTUM_ANNUAL_RETURN,
            "prior_annual_vol": RESIDUAL_MOMENTUM_ANNUAL_VOL,
            "prior_monthly_gross": RESIDUAL_MOMENTUM_MONTHLY_GROSS_PRIOR,
            "spread_cost": TRIAL24_SPREAD_ROUND_TRIP_COST,
            "t_bar": CONFIRMATORY_T_BAR,
            "assessment": a,
        },
        "candidate_b": {
            "name": "FIXED_COUNT_CROSS_SECTIONAL_BASIS",
            "t_bar": DISCOVERY_T_BAR,
            "assessment": b,
        },
        "winner": winner,
        "trial24_registered": bool(winner),
        "trial24_spec": trial24_spec() if winner else None,
        "final_read": False,
        "production_activation": False,
    }


def _ols_beta(y: np.ndarray, x: np.ndarray) -> np.ndarray | None:
    mask = np.isfinite(y) & np.isfinite(x).all(axis=1)
    if int(mask.sum()) < x.shape[1] + 8:
        return None
    xx = x[mask]
    yy = y[mask]
    try:
        beta, *_ = np.linalg.lstsq(xx, yy, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return beta


def _deciles(values: pd.Series) -> pd.Series:
    valid = values.dropna().sort_values(kind="mergesort")
    if len(valid) < 10:
        return pd.Series(index=values.index, dtype=float)
    # Deterministic equal-rank buckets; decile 10 is strongest.
    ranks = valid.rank(method="first", pct=True)
    dec = np.ceil(ranks * 10.0).clip(1, 10).astype(int)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    out.loc[dec.index] = dec.astype(float)
    return out


def compute_trial24_scores(
    monthly_returns: pd.DataFrame,
    factors: pd.DataFrame,
    membership: pd.DataFrame,
) -> pd.DataFrame:
    """Compute point-in-time 12-1 residual-momentum scores and deciles."""
    rets = monthly_returns.copy().sort_index()
    fac = factors.copy().sort_index()
    mem = membership.copy().reindex(index=rets.index, columns=rets.columns).fillna(False).astype(bool)
    common = rets.index.intersection(fac.index).intersection(mem.index).sort_values()
    rets = rets.reindex(common)
    fac = fac.reindex(common)
    mem = mem.reindex(common)
    required = {"rm_rf", "smb", "hml", "rf"}
    if not required.issubset(fac.columns):
        raise ValueError("Trial 24 requires rm_rf, smb, hml and rf factors")

    rows: list[dict] = []
    for pos in range(36, len(common)):
        date = common[pos]
        # Formation uses months t-12 through t-2; most recent month t-1 is skipped.
        formation_positions = list(range(pos - 12, pos - 1))
        if formation_positions[0] < 0:
            continue
        scores: dict[str, float] = {}
        reg_slice = slice(pos - 36, pos)
        x_base = fac.iloc[reg_slice][["rm_rf", "smb", "hml"]].to_numpy(dtype=float)
        x = np.column_stack([np.ones(len(x_base)), x_base])
        for symbol in rets.columns:
            if not bool(mem.at[date, symbol]):
                continue
            stock = rets.iloc[reg_slice][symbol].to_numpy(dtype=float)
            rf = fac.iloc[reg_slice]["rf"].to_numpy(dtype=float)
            y = stock - rf
            beta = _ols_beta(y, x)
            if beta is None:
                continue
            # Canonical residual momentum excludes fitted alpha from the signal.
            f = fac.iloc[formation_positions][["rm_rf", "smb", "hml"]].to_numpy(dtype=float)
            stock_form = rets.iloc[formation_positions][symbol].to_numpy(dtype=float)
            rf_form = fac.iloc[formation_positions]["rf"].to_numpy(dtype=float)
            if not (np.isfinite(f).all() and np.isfinite(stock_form).all() and np.isfinite(rf_form).all()):
                continue
            residual = (stock_form - rf_form) - (f @ beta[1:])
            sd = float(np.std(residual, ddof=1)) if len(residual) > 1 else float("nan")
            if not math.isfinite(sd) or sd <= 1e-12:
                continue
            scores[symbol] = float(np.sum(residual) / sd)
        if len(scores) < 10:
            continue
        s = pd.Series(scores, dtype=float)
        dec = _deciles(s)
        for symbol, score in s.items():
            d = dec.get(symbol)
            if pd.isna(d):
                continue
            rows.append({"date": pd.Timestamp(date), "symbol": symbol, "score": float(score), "decile": int(d)})
    return pd.DataFrame(rows, columns=["date", "symbol", "score", "decile"])


def _one_sided_t(values: Iterable[float]) -> float | None:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 2:
        return None
    sd = float(np.std(arr, ddof=1))
    if sd <= 0:
        return None
    return float(np.mean(arr) / (sd / math.sqrt(len(arr))))


def evaluate_trial24_from_scores(scores: pd.DataFrame, monthly_returns: pd.DataFrame) -> dict:
    """Evaluate only the first 80% of eligible monthly outcomes.

    The final 20% signal months are counted and date-bounded but their return
    values are never loaded into the result object or statistics.
    """
    if scores.empty:
        return {"status": "INCONCLUSIVE", "final_read": False, "confirmatory_months": 0, "final_months": 0, "months_evaluated": 0}
    rets = monthly_returns.sort_index()
    score_dates = sorted(pd.Timestamp(x) for x in scores["date"].dropna().unique())
    eligible: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    ret_index = pd.DatetimeIndex(rets.index)
    for d in score_dates:
        future = ret_index[ret_index > d]
        if len(future):
            eligible.append((d, pd.Timestamp(future[0])))
    if len(eligible) < 5:
        return {"status": "INCONCLUSIVE", "final_read": False, "confirmatory_months": 0, "final_months": len(eligible), "months_evaluated": 0}

    cut = max(1, int(math.floor(len(eligible) * 0.80)))
    if cut >= len(eligible):
        cut = len(eligible) - 1
    confirm_pairs = eligible[:cut]
    final_pairs = eligible[cut:]

    monthly: list[dict] = []
    for signal_date, outcome_date in confirm_pairs:
        sub = scores[scores["date"].eq(signal_date)]
        long_syms = sub.loc[sub["decile"].eq(10), "symbol"].tolist()
        short_syms = sub.loc[sub["decile"].eq(1), "symbol"].tolist()
        if not long_syms or not short_syms:
            continue
        row = rets.loc[outcome_date]
        longs = pd.to_numeric(row.reindex(long_syms), errors="coerce").dropna()
        shorts = pd.to_numeric(row.reindex(short_syms), errors="coerce").dropna()
        if longs.empty or shorts.empty:
            continue
        gross = float(longs.mean() - shorts.mean())
        monthly.append({"signal_date": signal_date, "outcome_date": outcome_date, "gross": gross, "net": gross - TRIAL24_SPREAD_ROUND_TRIP_COST})

    vals = np.asarray([m["net"] for m in monthly], dtype=float)
    mean_net = float(np.mean(vals)) if len(vals) else None
    t_stat = _one_sided_t(vals)
    # Four chronological blocks of the observed confirmatory months.
    block_positive = 0
    if len(vals):
        for block in np.array_split(vals, 4):
            if len(block) and float(np.mean(block)) > 0:
                block_positive += 1
    if len(vals) > 3:
        keep = np.sort(vals)[: len(vals) - 3]
        top3_removed = float(np.mean(keep))
    else:
        top3_removed = None
    passed = bool(
        len(vals) >= 60
        and mean_net is not None and mean_net > 0
        and t_stat is not None and t_stat >= CONFIRMATORY_T_BAR
        and block_positive >= 3
        and top3_removed is not None and top3_removed > 0
    )
    return {
        "status": "PASS_REPLICATION_PRE_FINAL" if passed else "FAIL_REPLICATION_PRE_FINAL",
        "trial": 24,
        "final_read": False,
        "confirmatory_months": int(len(confirm_pairs)),
        "final_months": int(len(final_pairs)),
        "months_evaluated": int(len(confirm_pairs)),
        "months_with_complete_portfolio": int(len(vals)),
        "mean_net": mean_net,
        "t_stat": t_stat,
        "t_bar": CONFIRMATORY_T_BAR,
        "positive_blocks": int(block_positive),
        "top3_removed_mean_net": top3_removed,
        "final_start": final_pairs[0][0].isoformat() if final_pairs else None,
        "production_activation": False,
    }


def evaluate_trial24_confirmatory_only(scores: pd.DataFrame, monthly_returns: pd.DataFrame, *, planned_final_months: int) -> dict:
    """Evaluate every eligible outcome in a dataset that contains *only* the pre-final window.

    Production V11 uses this function after the data loader has physically
    stopped at the frozen pre-final outcome month.  The untouched final count
    is declared from the preregistration calendar, not inferred from returns.
    """
    if scores.empty:
        return {
            "status": "INCONCLUSIVE", "trial": 24, "final_read": False,
            "confirmatory_months": 0, "final_months": int(planned_final_months),
            "months_evaluated": 0, "production_activation": False,
        }
    rets = monthly_returns.sort_index()
    ret_index = pd.DatetimeIndex(rets.index)
    monthly: list[dict] = []
    for signal_date in sorted(pd.Timestamp(x) for x in scores["date"].dropna().unique()):
        future = ret_index[ret_index > signal_date]
        if not len(future):
            continue
        outcome_date = pd.Timestamp(future[0])
        sub = scores[scores["date"].eq(signal_date)]
        long_syms = sub.loc[sub["decile"].eq(10), "symbol"].tolist()
        short_syms = sub.loc[sub["decile"].eq(1), "symbol"].tolist()
        if not long_syms or not short_syms:
            continue
        row = rets.loc[outcome_date]
        longs = pd.to_numeric(row.reindex(long_syms), errors="coerce").dropna()
        shorts = pd.to_numeric(row.reindex(short_syms), errors="coerce").dropna()
        if longs.empty or shorts.empty:
            continue
        gross = float(longs.mean() - shorts.mean())
        monthly.append({
            "signal_date": signal_date, "outcome_date": outcome_date,
            "gross": gross, "net": gross - TRIAL24_SPREAD_ROUND_TRIP_COST,
        })

    vals = np.asarray([m["net"] for m in monthly], dtype=float)
    mean_net = float(np.mean(vals)) if len(vals) else None
    t_stat = _one_sided_t(vals)
    blocks = 0
    if len(vals):
        for block in np.array_split(vals, 4):
            if len(block) and float(np.mean(block)) > 0:
                blocks += 1
    top3 = float(np.mean(np.sort(vals)[: len(vals)-3])) if len(vals) > 3 else None
    passed = bool(
        len(vals) >= 60 and mean_net is not None and mean_net > 0
        and t_stat is not None and t_stat >= CONFIRMATORY_T_BAR
        and blocks >= 3 and top3 is not None and top3 > 0
    )
    return {
        "status": "PASS_REPLICATION_PRE_FINAL" if passed else "FAIL_REPLICATION_PRE_FINAL",
        "trial": 24,
        "final_read": False,
        "confirmatory_months": int(len(vals)),
        "final_months": int(planned_final_months),
        "months_evaluated": int(len(vals)),
        "mean_net": mean_net,
        "t_stat": t_stat,
        "t_bar": CONFIRMATORY_T_BAR,
        "t_bar_name": CONFIRMATORY_T_BAR_NAME,
        "positive_blocks": int(blocks),
        "top3_removed_mean_net": top3,
        "production_activation": False,
    }
