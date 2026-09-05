"""V11.1 Development & Feasibility Lab orchestration.

V11.1 is deliberately not Trial 25.  It uses only the already-read Trial-24
pre-final data as development and keeps the final 31 months physically unread.
"""
from __future__ import annotations

import pandas as pd

from . import v111_development as dev

BUILD_ID = dev.BUILD_ID
CANDIDATE_A = "RESIDUAL_MOMENTUM_VOL_DERISK_12_1"
CANDIDATE_B = "LIQUID_PRICE_MOMENTUM_VOL_DERISK_12_1"
DEFAULT_VALIDATION_MONTHS = 31


def trial24_restatement() -> dict:
    """Non-rescuing permanent-record note using already-read Trial-24 figures."""
    return {
        "trial": 24,
        "verdict": "FAIL_REPLICATION_PRE_FINAL",
        "registered_verdict_changed": False,
        "observed_net_monthly": 0.00188,
        "registered_stress_cost_monthly": 0.00360,
        "implied_gross_monthly": 0.00548,
        "realized_annualized_volatility": 0.243,
        "external_source_annualized_volatility": 0.1249,
        "volatility_interpretation": "SOURCE_MARKET_VOLATILITY_DID_NOT_TRANSPORT_TO_TARGET_MARKET",
        "top3_and_blocks_role": "ROBUSTNESS_DIAGNOSTICS_NOT_INDEPENDENT_HARD_FAILURES",
        "final_months_unread": 31,
        "final_read": False,
        "production_activation": False,
    }


def _serializable_candidate(candidate: dict) -> dict:
    return {k: v for k, v in candidate.items() if k not in {"economics", "weights"}}


def run_development_lab(inputs: dict, *, validation_months: int = DEFAULT_VALIDATION_MONTHS,
                        power_simulations: int = 12000) -> dict:
    """Evaluate exactly two frozen candidates using development-only inputs."""
    if not bool((inputs or {}).get("data_readiness")):
        return {
            "build": BUILD_ID,
            "status": "INCONCLUSIVE_DATA_READINESS",
            "development_window": "2010-01_TO_2023-05",
            "final_months_unread": 31,
            "final_read": False,
            "trial25_run": False,
            "production_activation": False,
            "data_meta": (inputs or {}).get("meta") or {},
            "candidates": [],
            "selection": {"status": "NO_DEVELOPMENT_WINNER", "winner": None, "production_activation": False},
            "trial24_record": trial24_restatement(),
        }
    monthly = inputs["monthly_returns"]
    factors = inputs["factors"]
    membership = inputs["membership"]
    validated = dev.development_only_inputs(monthly, factors, membership)
    monthly = validated["monthly_returns"]
    factors = validated["factors"]
    membership = validated["membership"]
    contracts = inputs.get("futures_contracts_by_month") or {}

    residual_scores = dev.compute_residual_momentum_scores(monthly, factors, membership)
    price_scores = dev.compute_price_momentum_scores(monthly, membership)
    # Fair comparison: both candidates are evaluated on identical signal dates.
    residual_dates = set(pd.Timestamp(x) for x in residual_scores["date"].unique()) if not residual_scores.empty else set()
    price_dates = set(pd.Timestamp(x) for x in price_scores["date"].unique()) if not price_scores.empty else set()
    common_dates = sorted(residual_dates.intersection(price_dates))
    residual_scores = residual_scores[residual_scores["date"].isin(common_dates)].copy()
    price_scores = price_scores[price_scores["date"].isin(common_dates)].copy()

    a = dev.build_candidate_development(
        name=CANDIDATE_A,
        scores=residual_scores,
        monthly_returns=monthly,
        futures_contracts_by_month=contracts,
        validation_months=int(validation_months),
        power_simulations=int(power_simulations),
        power_seed=111,
    )
    b = dev.build_candidate_development(
        name=CANDIDATE_B,
        scores=price_scores,
        monthly_returns=monthly,
        futures_contracts_by_month=contracts,
        validation_months=int(validation_months),
        power_simulations=int(power_simulations),
        power_seed=112,
        target_vol=dev.PRICE_MOMENTUM_TARGET_ANNUAL_VOL,
        target_vol_provenance="BARROSO_SANTA_CLARA_RISK_MANAGED_MOMENTUM_19PCT_REFERENCE",
    )
    selection = dev.select_development_winner(a, b)
    selection = {**selection, "production_activation": False, "trial25_run": False}
    return {
        "build": BUILD_ID,
        "status": "DEVELOPMENT_ONLY_NO_TRIAL25_YET",
        "development_window": "2010-01_TO_2023-05",
        "development_classification": "ALREADY_READ_TRIAL24_PREFINAL_RECLASSIFIED_DEVELOPMENT_ONLY",
        "comparison_signal_months": int(len(common_dates)),
        "declared_power_projection_months": int(validation_months),
        "power_projection_window_role": "SIZE_ONLY_FINAL_RETURNS_UNREAD",
        "final_months_unread": 31,
        "final_read": False,
        "trial25_run": False,
        "trial25_registered": False,
        "production_activation": False,
        "data_meta": inputs.get("meta") or {},
        "trial24_record": trial24_restatement(),
        "candidates": [_serializable_candidate(a), _serializable_candidate(b)],
        "selection": selection,
    }
