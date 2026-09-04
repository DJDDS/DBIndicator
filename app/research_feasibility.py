"""Pre-trial feasibility gate for DBIndicator research.

This module does not evaluate alpha.  It decides whether a proposed design is
worth registering before historical outcome data are spent.
"""
from __future__ import annotations

import math
from statistics import NormalDist


def minimum_detectable_effect(*, sigma_day: float | None, effective_days: int | None, t_bar: float) -> float | None:
    try:
        sigma = float(sigma_day)
        days = int(effective_days)
        bar = float(t_bar)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sigma) or sigma <= 0 or days <= 0 or not math.isfinite(bar) or bar <= 0:
        return None
    return bar * sigma / math.sqrt(days)


def normal_approx_power(*, true_effect: float | None, sigma_day: float | None, effective_days: int | None, t_bar: float) -> float | None:
    """One-sided normal-approximation probability of clearing ``t_bar``."""
    mde = minimum_detectable_effect(sigma_day=sigma_day, effective_days=effective_days, t_bar=t_bar)
    if mde is None:
        return None
    try:
        effect = float(true_effect)
        sigma = float(sigma_day)
        days = int(effective_days)
        bar = float(t_bar)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(effect):
        return None
    expected_t = effect / (sigma / math.sqrt(days))
    power = 1.0 - NormalDist().cdf(bar - expected_t)
    return min(1.0, max(0.0, float(power)))


def assess_pretrial_feasibility(
    *,
    prior_gross_effect: float | None,
    round_trip_cost: float,
    sigma_day: float | None,
    effective_days: int | None,
    t_bar: float,
    source: str | None,
    horizon: str | None,
) -> dict:
    """Fail-closed feasibility decision made before a trial is registered.

    A design may run only when a cited prior magnitude exists, its expected
    return remains positive after frozen cost, and the design's MDE is no
    larger than that expected net effect.
    """
    base = {
        "source": source,
        "horizon": horizon,
        "round_trip_cost": float(round_trip_cost),
        "t_bar": float(t_bar),
        "effective_days": int(effective_days) if effective_days is not None else None,
        "sigma_day": float(sigma_day) if sigma_day is not None else None,
        "prior_gross_effect": None,
        "prior_net_effect": None,
        "mde_net_effect": minimum_detectable_effect(sigma_day=sigma_day, effective_days=effective_days, t_bar=t_bar),
        "power": None,
        "feasible": False,
        "decision": None,
    }
    if prior_gross_effect is None or not source:
        base["decision"] = "DO_NOT_RUN_PRIOR_EFFECT_REQUIRED"
        return base
    gross = float(prior_gross_effect)
    net = gross - float(round_trip_cost)
    base["prior_gross_effect"] = gross
    base["prior_net_effect"] = net
    base["power"] = normal_approx_power(true_effect=net, sigma_day=sigma_day, effective_days=effective_days, t_bar=t_bar)
    if net <= 0:
        base["decision"] = "DO_NOT_RUN_COST_WALL"
        return base
    if base["mde_net_effect"] is None:
        base["decision"] = "DO_NOT_RUN_POWER_INPUTS_REQUIRED"
        return base
    if base["mde_net_effect"] > net:
        base["decision"] = "DO_NOT_RUN_UNDERPOWERED"
        return base
    base["feasible"] = True
    base["decision"] = "GO_REGISTER_PREREGISTERED_TRIAL"
    return base
