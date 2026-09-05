"""V11.1 development-only momentum laboratory primitives.

This module never reads the Trial-24 final block.  Inputs must terminate at the
fixed development boundary before any score or candidate economics are formed.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd

from . import v11_research

BUILD_ID = "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB"
DEVELOPMENT_START = pd.Timestamp("2010-01-31")
DEVELOPMENT_END = pd.Timestamp("2023-05-31")
LOCKED_FINAL_MONTHS = 31
PRIMARY_TARGET_ANNUAL_VOL = 0.1249
PRICE_MOMENTUM_TARGET_ANNUAL_VOL = 0.19


def _assert_development_frame(frame: pd.DataFrame, name: str) -> None:
    if frame is None:
        raise ValueError(f"{name} is required")
    if len(frame.index) == 0:
        return
    idx = pd.to_datetime(frame.index, errors="coerce")
    if idx.isna().any():
        raise ValueError(f"{name} has non-date index values")
    if pd.Timestamp(idx.max()) > DEVELOPMENT_END:
        raise ValueError(
            f"V11.1 locked final firewall: {name} extends past {DEVELOPMENT_END:%Y-%m}; "
            "candidate-return reads from the locked final period are forbidden"
        )


def development_only_inputs(monthly_returns: pd.DataFrame, factors: pd.DataFrame,
                            membership: pd.DataFrame) -> dict:
    """Validate that every outcome-bearing frame is development-only.

    We fail rather than silently slice so callers cannot accidentally pass a
    frame containing the unread final block and rely on downstream discipline.
    """
    _assert_development_frame(monthly_returns, "monthly_returns")
    _assert_development_frame(factors, "factors")
    _assert_development_frame(membership, "membership")
    return {
        "monthly_returns": monthly_returns.copy(),
        "factors": factors.copy(),
        "membership": membership.copy(),
        "development_start": DEVELOPMENT_START,
        "development_end": DEVELOPMENT_END,
        "final_months_unread": LOCKED_FINAL_MONTHS,
        "final_read": False,
    }


def compute_residual_momentum_scores(monthly_returns: pd.DataFrame, factors: pd.DataFrame,
                                     membership: pd.DataFrame) -> pd.DataFrame:
    development_only_inputs(monthly_returns, factors, membership)
    return v11_research.compute_trial24_scores(monthly_returns, factors, membership)


def _deciles(values: pd.Series) -> pd.Series:
    valid = values.dropna().sort_values(kind="mergesort")
    if len(valid) < 10:
        return pd.Series(index=values.index, dtype=float)
    ranks = valid.rank(method="first", pct=True)
    dec = np.ceil(ranks * 10.0).clip(1, 10).astype(int)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    out.loc[dec.index] = dec.astype(float)
    return out


def compute_price_momentum_scores(monthly_returns: pd.DataFrame,
                                  membership: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time conventional 12-1 total-return momentum scores.

    At signal month ``t``, the score uses exactly t-12 through t-2 monthly
    returns.  t-1 is skipped and no future return enters the ranking.
    """
    empty_factors = pd.DataFrame(index=monthly_returns.index)
    development_only_inputs(monthly_returns, empty_factors, membership)
    rets = monthly_returns.copy().sort_index()
    mem = membership.copy().reindex(index=rets.index, columns=rets.columns).fillna(False).astype(bool)
    rows: list[dict] = []
    for pos in range(12, len(rets.index)):
        date = pd.Timestamp(rets.index[pos])
        formation = rets.iloc[pos - 12: pos - 1]
        scores: dict[str, float] = {}
        for symbol in rets.columns:
            if not bool(mem.at[date, symbol]):
                continue
            vals = pd.to_numeric(formation[symbol], errors="coerce").to_numpy(dtype=float)
            if len(vals) != 11 or not np.isfinite(vals).all() or np.any(vals <= -1.0):
                continue
            scores[symbol] = float(np.prod(1.0 + vals) - 1.0)
        if len(scores) < 10:
            continue
        s = pd.Series(scores, dtype=float)
        dec = _deciles(s)
        for symbol, score in s.items():
            d = dec.get(symbol)
            if pd.isna(d):
                continue
            rows.append({"date": date, "symbol": symbol, "score": float(score), "decile": int(d)})
    return pd.DataFrame(rows, columns=["date", "symbol", "score", "decile"])


def scores_to_weights(scores: pd.DataFrame) -> pd.DataFrame:
    """Convert decile scores to equal-weight +100%/-100% monthly portfolios."""
    if scores is None or scores.empty:
        return pd.DataFrame(dtype=float)
    rows = []
    all_symbols = sorted(scores["symbol"].dropna().astype(str).unique())
    dates = sorted(pd.Timestamp(x) for x in scores["date"].dropna().unique())
    for date in dates:
        sub = scores[scores["date"].eq(date)]
        longs = sorted(sub.loc[sub["decile"].eq(10), "symbol"].astype(str).tolist())
        shorts = sorted(sub.loc[sub["decile"].eq(1), "symbol"].astype(str).tolist())
        if not longs or not shorts:
            continue
        w = {s: 0.0 for s in all_symbols}
        for s in longs:
            w[s] = 1.0 / len(longs)
        for s in shorts:
            w[s] = -1.0 / len(shorts)
        rows.append(pd.Series(w, name=date, dtype=float))
    if not rows:
        return pd.DataFrame(columns=all_symbols, dtype=float)
    return pd.DataFrame(rows).fillna(0.0).sort_index().reindex(columns=all_symbols, fill_value=0.0)


def portfolio_gross_returns(weights: pd.DataFrame, monthly_returns: pd.DataFrame) -> pd.Series:
    """Evaluate each signal portfolio on the next available monthly return."""
    _assert_development_frame(monthly_returns, "monthly_returns")
    if weights is None or weights.empty:
        return pd.Series(dtype=float, name="gross")
    rets = monthly_returns.sort_index()
    out = {}
    ret_index = pd.DatetimeIndex(rets.index)
    for signal_date, row in weights.sort_index().iterrows():
        future = ret_index[ret_index > pd.Timestamp(signal_date)]
        if not len(future):
            continue
        outcome = pd.Timestamp(future[0])
        aligned = pd.to_numeric(rets.loc[outcome].reindex(weights.columns), errors="coerce")
        active = row.ne(0.0)
        if not active.any() or aligned.loc[active].isna().any():
            continue
        out[outcome] = float((row * aligned.fillna(0.0)).sum())
    return pd.Series(out, dtype=float, name="gross").sort_index()


def lagged_realized_vol_forecast(gross_returns: pd.Series, *, lookback_months: int = 12,
                                 min_periods: int | None = None) -> pd.Series:
    """Annualized portfolio realized-volatility forecast using prior months only."""
    lookback = int(lookback_months)
    if lookback < 2:
        raise ValueError("lookback_months must be at least 2")
    minimum = lookback if min_periods is None else int(min_periods)
    if minimum < 2 or minimum > lookback:
        raise ValueError("min_periods must be between 2 and lookback_months")
    r = pd.to_numeric(pd.Series(gross_returns).sort_index(), errors="coerce")
    # shift first: the outcome month being scaled can never enter its own risk forecast.
    return r.shift(1).rolling(window=lookback, min_periods=minimum).std(ddof=1) * math.sqrt(12.0)


def derisk_exposure(forecast_vol, target_vol: float = PRIMARY_TARGET_ANNUAL_VOL) -> float:
    """Frozen primary scaler: de-risk only; never lever above 1x."""
    try:
        fv = float(forecast_vol)
        tv = float(target_vol)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(fv) and fv > 0 and math.isfinite(tv) and tv > 0):
        return 0.0
    return float(min(1.0, max(0.0, tv / fv)))


def apply_exposure(gross_returns: pd.Series, forecast_vol: pd.Series,
                   target_vol: float = PRIMARY_TARGET_ANNUAL_VOL) -> pd.DataFrame:
    gross = pd.to_numeric(pd.Series(gross_returns).sort_index(), errors="coerce")
    forecast = pd.to_numeric(pd.Series(forecast_vol).reindex(gross.index), errors="coerce")
    exposure = forecast.map(lambda x: derisk_exposure(x, target_vol=target_vol))
    return pd.DataFrame({
        "gross_unscaled": gross,
        "forecast_annual_vol": forecast,
        "exposure": exposure,
        "gross": gross * exposure,
    })


def portfolio_turnover(weights: pd.DataFrame, exposure: pd.Series | None = None) -> pd.Series:
    """Monthly traded notional as a fraction of the 200%-gross book.

    Convention: ``sum(abs(w_t - w_{t-1})) / 2``.  Opening a +100%/-100% book
    from cash is 100% turnover; replacing every long and short name is 200%.
    """
    if weights is None or weights.empty:
        return pd.Series(dtype=float, name="turnover")
    w = weights.copy().fillna(0.0).sort_index()
    if exposure is not None:
        exp = pd.to_numeric(pd.Series(exposure).reindex(w.index), errors="coerce").fillna(0.0).clip(0.0, 1.0)
        w = w.mul(exp, axis=0)
    prev = pd.Series(0.0, index=w.columns)
    values = {}
    for date, row in w.iterrows():
        values[pd.Timestamp(date)] = float((row - prev).abs().sum() / 2.0)
        prev = row
    return pd.Series(values, dtype=float, name="turnover")


def apply_measured_costs(gross_returns: pd.Series, turnover: pd.Series, *,
                         per_turnover_cost: float = 0.0018,
                         stress_cost: float = 0.0036) -> pd.DataFrame:
    """Keep measured-turnover economics separate from the stress scenario."""
    gross = pd.to_numeric(pd.Series(gross_returns).sort_index(), errors="coerce")
    turn = pd.to_numeric(pd.Series(turnover).reindex(gross.index), errors="coerce")
    measured = turn * float(per_turnover_cost)
    stress = pd.Series(float(stress_cost), index=gross.index, dtype=float)
    return pd.DataFrame({
        "gross": gross,
        "turnover": turn,
        "measured_cost": measured,
        "net_measured": gross - measured,
        "stress_cost": stress,
        "net_stress": gross - stress,
    })

from . import research_feasibility


def _finite_or_none(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def candidate_metrics(economics: pd.DataFrame) -> dict:
    """Development diagnostics for one frozen candidate economics path."""
    if economics is None or economics.empty or "net_measured" not in economics:
        return {
            "gross_annualized_mean": None, "net_monthly_mean": None,
            "net_annualized_mean": None, "annualized_volatility": None,
            "sharpe": None, "sortino": None, "max_drawdown": None,
            "worst_month": None, "skewness": None, "kurtosis": None,
            "cvar_5": None, "average_turnover": None, "median_turnover": None,
            "stress_net_monthly_mean": None, "stress_net_annualized_mean": None,
            "complete_months": 0, "positive_blocks": 0,
            "top3_removed_mean_net": None,
        }
    frame = economics.copy().sort_index()
    net = pd.to_numeric(frame["net_measured"], errors="coerce").dropna()
    gross = pd.to_numeric(frame.get("gross"), errors="coerce").reindex(net.index)
    turnover = pd.to_numeric(frame.get("turnover"), errors="coerce").reindex(net.index)
    stress_net = pd.to_numeric(frame.get("net_stress"), errors="coerce").reindex(net.index) if "net_stress" in frame else pd.Series(index=net.index, dtype=float)
    n = int(len(net))
    mean_net = float(net.mean()) if n else None
    mean_gross = float(gross.mean()) if n else None
    sd = float(net.std(ddof=1)) if n > 1 else None
    ann_vol = sd * math.sqrt(12.0) if sd is not None and math.isfinite(sd) else None
    sharpe = (mean_net / sd) * math.sqrt(12.0) if sd and sd > 0 else None
    downside = net[net < 0]
    downside_dev = float(np.sqrt(np.mean(np.square(downside.to_numpy(dtype=float))))) if len(downside) else None
    sortino = (mean_net * math.sqrt(12.0) / downside_dev) if downside_dev and downside_dev > 0 else None
    wealth = (1.0 + net).cumprod() if n else pd.Series(dtype=float)
    drawdown = wealth / wealth.cummax() - 1.0 if n else pd.Series(dtype=float)
    k = max(1, int(math.ceil(n * 0.05))) if n else 0
    cvar = float(net.nsmallest(k).mean()) if n else None
    positive_blocks = 0
    if n:
        for block in np.array_split(net.to_numpy(dtype=float), 4):
            if len(block) and float(np.mean(block)) > 0:
                positive_blocks += 1
    top3_removed = None
    if n > 3:
        arr = np.sort(net.to_numpy(dtype=float))
        top3_removed = float(np.mean(arr[:-3]))
    return {
        "gross_annualized_mean": mean_gross * 12.0 if mean_gross is not None else None,
        "net_monthly_mean": mean_net,
        "net_annualized_mean": mean_net * 12.0 if mean_net is not None else None,
        "annualized_volatility": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": float(drawdown.min()) if n else None,
        "worst_month": float(net.min()) if n else None,
        "skewness": _finite_or_none(net.skew()) if n >= 3 else None,
        "kurtosis": _finite_or_none(net.kurt()) if n >= 4 else None,
        "cvar_5": cvar,
        "average_turnover": _finite_or_none(turnover.mean()) if n else None,
        "median_turnover": _finite_or_none(turnover.median()) if n else None,
        "stress_net_monthly_mean": _finite_or_none(stress_net.mean()) if n else None,
        "stress_net_annualized_mean": (_finite_or_none(stress_net.mean()) * 12.0) if n and _finite_or_none(stress_net.mean()) is not None else None,
        "complete_months": n,
        "positive_blocks": int(positive_blocks),
        "top3_removed_mean_net": top3_removed,
    }


def project_confirmatory_power(economics: pd.DataFrame, *, validation_months: int,
                               minimum_power: float = 0.80,
                               t_bar: float = 1.645,
                               simulations: int = 12000,
                               seed: int = 111) -> dict:
    """Project Trial-25 feasibility from development-measured economics only."""
    if economics is None or economics.empty:
        return research_feasibility.assess_pretrial_feasibility_v2(
            expected_gross_effect=None, expected_cost=0.0, sigma_period=None,
            effective_periods=validation_months, t_bar=t_bar, source=None,
            horizon="1_MONTH", minimum_power=minimum_power,
            volatility_provenance="MISSING", cost_provenance="MEASURED_TURNOVER",
            effect_provenance="V11.1_DEVELOPMENT",
        )
    frame = economics.copy().dropna(subset=["gross", "net_measured", "measured_cost"])
    if len(frame) < 2:
        return research_feasibility.assess_pretrial_feasibility_v2(
            expected_gross_effect=None, expected_cost=0.0, sigma_period=None,
            effective_periods=validation_months, t_bar=t_bar, source=None,
            horizon="1_MONTH", minimum_power=minimum_power,
            volatility_provenance="MISSING", cost_provenance="MEASURED_TURNOVER",
            effect_provenance="V11.1_DEVELOPMENT",
        )
    gross_mean = float(frame["gross"].mean())
    avg_cost = float(frame["measured_cost"].mean())
    net = pd.to_numeric(frame["net_measured"], errors="coerce").dropna()
    sigma = float(net.std(ddof=1))
    true_net = gross_mean - avg_cost
    if not math.isfinite(sigma) or sigma <= 1e-12:
        return research_feasibility.assess_pretrial_feasibility_v2(
            expected_gross_effect=gross_mean, expected_cost=avg_cost, sigma_period=None,
            effective_periods=validation_months, t_bar=t_bar,
            source="V11.1 target-market development estimate", horizon="1_MONTH",
            minimum_power=minimum_power, volatility_provenance="DEVELOPMENT_TARGET_MARKET_INVALID_ZERO_VARIANCE",
            cost_provenance="MEASURED_TURNOVER", effect_provenance="DEVELOPMENT_TARGET_MARKET",
            t_bar_name="ONE_SIDED_5PCT_PREREGISTERED_CONFIRMATION",
        )

    def battery(sample):
        arr = np.asarray(sample, dtype=float)
        if len(arr) < 2:
            return {"primary_t": False, "positive_net": False}
        s = float(np.std(arr, ddof=1))
        t = float(np.mean(arr) / (s / math.sqrt(len(arr)))) if s > 0 else float("-inf")
        return {"primary_t": t >= float(t_bar), "positive_net": float(np.mean(arr)) > 0.0}

    joint = research_feasibility.estimate_joint_battery_power(
        true_effect=true_net,
        sigma_period=sigma,
        sample_size=int(validation_months),
        simulations=int(simulations),
        seed=int(seed),
        battery_fn=battery,
    )
    assessment = research_feasibility.assess_pretrial_feasibility_v2(
        expected_gross_effect=gross_mean,
        expected_cost=avg_cost,
        sigma_period=sigma,
        effective_periods=int(validation_months),
        t_bar=float(t_bar),
        source="V11.1 target-market development estimate",
        horizon="1_MONTH",
        minimum_power=float(minimum_power),
        volatility_provenance="DEVELOPMENT_TARGET_MARKET",
        cost_provenance="MEASURED_TURNOVER",
        effect_provenance="DEVELOPMENT_TARGET_MARKET",
        joint_power=float(joint["joint_power"]),
        joint_power_method=joint["method"],
        t_bar_name="ONE_SIDED_5PCT_PREREGISTERED_CONFIRMATION",
    )
    assessment["joint_power_marginals"] = joint["marginal_power"]
    assessment["joint_power_simulations"] = joint["simulations"]
    assessment["joint_power_seed"] = joint["seed"]
    return assessment


def _candidate_hard_pass(candidate: dict) -> bool:
    metrics = (candidate or {}).get("metrics") or {}
    power = (candidate or {}).get("power") or {}
    return bool(
        (candidate or {}).get("integrity_pass")
        and (candidate or {}).get("execution_coverage_pass")
        and (metrics.get("net_monthly_mean") is not None and float(metrics.get("net_monthly_mean")) > 0)
        and (power.get("joint_power") is not None and float(power.get("joint_power")) >= 0.80)
        and power.get("decision") == "GO_REGISTER_PREREGISTERED_TRIAL"
    )


def select_development_winner(candidate_a: dict, candidate_b: dict, *,
                              sharpe_tie_tolerance: float = 0.001) -> dict:
    """Frozen, non-optimizing hierarchy for Trial-25 eligibility."""
    a_ok = _candidate_hard_pass(candidate_a)
    b_ok = _candidate_hard_pass(candidate_b)
    if not a_ok and not b_ok:
        return {"status": "NO_DEVELOPMENT_WINNER", "winner": None, "tie_break": None,
                "candidate_a_pass": False, "candidate_b_pass": False}
    if a_ok and not b_ok:
        return {"status": "ELIGIBLE_FOR_TRIAL_25", "winner": candidate_a.get("name"),
                "tie_break": "ONLY_HARD_GATE_PASSER", "candidate_a_pass": True, "candidate_b_pass": False}
    if b_ok and not a_ok:
        return {"status": "ELIGIBLE_FOR_TRIAL_25", "winner": candidate_b.get("name"),
                "tie_break": "ONLY_HARD_GATE_PASSER", "candidate_a_pass": False, "candidate_b_pass": True}

    a_metrics = candidate_a.get("metrics") or {}
    b_metrics = candidate_b.get("metrics") or {}
    a_sharpe = float(a_metrics.get("sharpe") or float("-inf"))
    b_sharpe = float(b_metrics.get("sharpe") or float("-inf"))
    if math.isfinite(a_sharpe) and math.isfinite(b_sharpe) and abs(a_sharpe - b_sharpe) <= float(sharpe_tie_tolerance):
        a_turn = float(a_metrics.get("average_turnover") or float("inf"))
        b_turn = float(b_metrics.get("average_turnover") or float("inf"))
        winner = candidate_a if a_turn <= b_turn else candidate_b
        return {"status": "ELIGIBLE_FOR_TRIAL_25", "winner": winner.get("name"),
                "tie_break": "LOWER_TURNOVER", "candidate_a_pass": True, "candidate_b_pass": True}
    winner = candidate_a if a_sharpe > b_sharpe else candidate_b
    return {"status": "ELIGIBLE_FOR_TRIAL_25", "winner": winner.get("name"),
            "tie_break": "HIGHER_MEASURED_COST_SHARPE", "candidate_a_pass": True, "candidate_b_pass": True}


def audit_futstk_execution_coverage(weights: pd.DataFrame, metadata_by_month: dict,
                                    *, minimum_coverage: float = 1.0) -> dict:
    """Coverage-only audit for the frozen V11.1 futures execution contract.

    No futures return is computed here.  A required active portfolio name is
    covered only when the nearest non-expired contract exists and both a board
    lot and an executable settle/close price field are present.
    """
    rule = {
        "contract_selection_rule": "NEAREST_NONEXPIRED_FUTSTK_AT_SIGNAL_MONTH_END",
        "price_rule": "SETTLE_IF_POSITIVE_ELSE_CLOSE",
        "roll_rule": "MONTHLY_RESELECT_AT_SIGNAL_MONTH_END",
        "expiry_handling": "EXCLUDE_ALREADY_EXPIRED_CONTRACTS",
        "lot_size_source": "OFFICIAL_NSE_CONTRACT_ARCHIVE_OR_INFERRED_LEGACY_BHAVCOPY",
        "missing_contract_policy": "FAIL_CLOSED_NO_CASH_PNL_SUBSTITUTION",
    }
    if weights is None or weights.empty:
        return {**rule, "required": 0, "covered": 0, "missing_required": 0,
                "coverage": 0.0, "pass": False,
                "status": "FUTSTK_EXECUTION_COVERAGE_INSUFFICIENT"}
    normalized = {}
    for key, value in (metadata_by_month or {}).items():
        month = pd.Timestamp(key).to_period("M").to_timestamp("M")
        normalized[month] = value or {}
    required = covered = 0
    missing: list[dict] = []
    for date, row in weights.sort_index().iterrows():
        month = pd.Timestamp(date).to_period("M").to_timestamp("M")
        month_meta = normalized.get(month) or {}
        for symbol in row.index[row.ne(0.0)]:
            required += 1
            meta = month_meta.get(str(symbol)) or {}
            ok = bool(meta and meta.get("lot_size_available") and meta.get("price_available") and meta.get("expiry"))
            if ok:
                covered += 1
            else:
                missing.append({"month": month.date().isoformat(), "symbol": str(symbol)})
    coverage = covered / required if required else 0.0
    passed = bool(required > 0 and coverage >= float(minimum_coverage))
    return {
        **rule,
        "required": int(required),
        "covered": int(covered),
        "missing_required": int(required - covered),
        "coverage": float(coverage),
        "minimum_coverage": float(minimum_coverage),
        "pass": passed,
        "status": "FUTSTK_EXECUTION_COVERAGE_OK" if passed else "FUTSTK_EXECUTION_COVERAGE_INSUFFICIENT",
        "missing_examples": missing[:25],
    }


def _signal_to_outcome_map(signal_dates: Iterable, return_index: Iterable) -> dict[pd.Timestamp, pd.Timestamp]:
    ret_index = pd.DatetimeIndex(pd.to_datetime(list(return_index))).sort_values()
    mapping = {}
    for d in sorted(pd.Timestamp(x) for x in signal_dates):
        future = ret_index[ret_index > d]
        if len(future):
            mapping[d] = pd.Timestamp(future[0])
    return mapping


def scale_signal_weights_for_outcome_exposure(weights: pd.DataFrame, monthly_returns: pd.DataFrame,
                                              exposure_by_outcome: pd.Series) -> pd.DataFrame:
    if weights is None or weights.empty:
        return pd.DataFrame(dtype=float)
    mapping = _signal_to_outcome_map(weights.index, monthly_returns.index)
    scaled = weights.copy().astype(float)
    exp = pd.to_numeric(pd.Series(exposure_by_outcome), errors="coerce")
    for signal_date in scaled.index:
        outcome = mapping.get(pd.Timestamp(signal_date))
        value = float(exp.get(outcome, 0.0)) if outcome is not None and pd.notna(exp.get(outcome, np.nan)) else 0.0
        scaled.loc[signal_date] = scaled.loc[signal_date] * max(0.0, min(1.0, value))
    return scaled


def align_signal_series_to_outcomes(signal_series: pd.Series, monthly_returns: pd.DataFrame) -> pd.Series:
    mapping = _signal_to_outcome_map(signal_series.index, monthly_returns.index)
    out = {}
    for signal_date, value in pd.Series(signal_series).items():
        outcome = mapping.get(pd.Timestamp(signal_date))
        if outcome is not None:
            out[outcome] = value
    return pd.Series(out, dtype=float).sort_index()


def build_candidate_development(*, name: str, scores: pd.DataFrame,
                                monthly_returns: pd.DataFrame,
                                futures_contracts_by_month: dict,
                                validation_months: int,
                                per_turnover_cost: float = 0.0018,
                                stress_cost: float = 0.0036,
                                target_vol: float = PRIMARY_TARGET_ANNUAL_VOL,
                                target_vol_provenance: str = "BLITZ_HUIJ_MARTENS_CANONICAL_RESIDUAL_MOMENTUM_12P49PCT",
                                vol_lookback_months: int = 12,
                                power_simulations: int = 12000,
                                power_seed: int = 111) -> dict:
    """Construct one frozen V11.1 development candidate end to end."""
    _assert_development_frame(monthly_returns, "monthly_returns")
    weights = scores_to_weights(scores)
    unscaled_gross = portfolio_gross_returns(weights, monthly_returns)
    forecast = lagged_realized_vol_forecast(unscaled_gross, lookback_months=vol_lookback_months)
    scaled = apply_exposure(unscaled_gross, forecast, target_vol=target_vol)
    valid = scaled["forecast_annual_vol"].notna() & scaled["gross_unscaled"].notna()
    scaled = scaled.loc[valid].copy()
    scaled_weights = scale_signal_weights_for_outcome_exposure(weights, monthly_returns, scaled["exposure"])
    # Coverage and turnover apply only once the volatility forecaster is live.
    active_signal_dates = []
    mapping = _signal_to_outcome_map(scaled_weights.index, monthly_returns.index)
    valid_outcomes = set(pd.Timestamp(x) for x in scaled.index)
    for signal_date, outcome in mapping.items():
        if outcome in valid_outcomes:
            active_signal_dates.append(signal_date)
    scaled_weights = scaled_weights.reindex(active_signal_dates).fillna(0.0)
    turnover_signal = portfolio_turnover(scaled_weights)
    turnover_outcome = align_signal_series_to_outcomes(turnover_signal, monthly_returns).reindex(scaled.index)
    economics = apply_measured_costs(
        scaled["gross"], turnover_outcome,
        per_turnover_cost=per_turnover_cost, stress_cost=stress_cost,
    ).dropna(subset=["gross", "turnover", "net_measured"])
    metrics = candidate_metrics(economics)
    power = project_confirmatory_power(
        economics, validation_months=int(validation_months), simulations=int(power_simulations), seed=int(power_seed)
    )
    coverage = audit_futstk_execution_coverage(scaled_weights, futures_contracts_by_month)
    return {
        "name": str(name),
        "integrity_pass": bool(len(economics) > 0 and economics.index.max() <= DEVELOPMENT_END),
        "execution_coverage_pass": bool(coverage.get("pass")),
        "scores_rows": int(len(scores)),
        "portfolio_months": int(len(economics)),
        "volatility_target": float(target_vol),
        "volatility_target_provenance": str(target_vol_provenance),
        "volatility_lookback_months": int(vol_lookback_months),
        "scaler": "MIN_1_TARGET_VOL_OVER_LAGGED_12M_PORTFOLIO_VOL",
        "max_exposure": 1.0,
        "per_turnover_cost": float(per_turnover_cost),
        "cost_provenance": "FROZEN_BUILD_0P18PCT_ROUND_TRIP_PER_100PCT_TURNOVER",
        "stress_cost": float(stress_cost),
        "metrics": metrics,
        "power": power,
        "execution_coverage": coverage,
        "economics": economics,
        "weights": scaled_weights,
        "final_read": False,
        "production_activation": False,
    }
