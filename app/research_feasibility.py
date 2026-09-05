"""Pre-trial feasibility gate for DBIndicator research.

This module does not evaluate alpha.  It decides whether a proposed design is
worth registering before historical outcome data are spent.
"""
from __future__ import annotations

import math
from statistics import NormalDist


class TrialRegistrationRefused(RuntimeError):
    """Raised when a proposed research trial fails the pre-trial feasibility gate."""



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
    t_bar_name: str | None = None,
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
        "t_bar_name": t_bar_name or "UNNAMED_T_BAR",
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


def require_feasible_registration(assessment: dict) -> dict:
    """Return a feasible assessment or refuse registration explicitly."""
    if not bool((assessment or {}).get("feasible")):
        decision = str((assessment or {}).get("decision") or "DO_NOT_RUN")
        raise TrialRegistrationRefused(f"Trial registration refused: {decision}")
    return assessment


def _finite_positive(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def select_target_sigma(*, development_sigma=None, external_sigma=None,
                        development_source=None, external_source=None) -> dict:
    """Choose target-market development volatility whenever available.

    External/source-market volatility remains useful context, but it cannot
    override volatility measured from the exact target-market implementation.
    """
    dev = _finite_positive(development_sigma)
    ext = _finite_positive(external_sigma)
    if dev is not None:
        return {
            "sigma": dev,
            "provenance": "DEVELOPMENT_TARGET_MARKET",
            "source": development_source or "TARGET_MARKET_DEVELOPMENT",
            "external_sigma": ext,
            "external_source": external_source,
        }
    if ext is not None:
        return {
            "sigma": ext,
            "provenance": "EXTERNAL_SOURCE_MARKET",
            "source": external_source or "EXTERNAL_SOURCE",
            "external_sigma": ext,
            "external_source": external_source,
        }
    return {
        "sigma": None,
        "provenance": "MISSING",
        "source": None,
        "external_sigma": ext,
        "external_source": external_source,
    }


def estimate_joint_battery_power(*, true_effect: float, sigma_period: float,
                                 sample_size: int, simulations: int = 20000,
                                 seed: int = 111, battery_fn=None) -> dict:
    """Monte-Carlo probability of passing a complete dependent gate battery.

    ``battery_fn`` receives one simulated return vector and returns either a
    bool or a mapping of named gate booleans.  The same simulated path is used
    for every gate, preserving their dependence.  Marginal gate powers are
    reported only as diagnostics; they are never multiplied together.
    """
    sigma = _finite_positive(sigma_period)
    try:
        effect = float(true_effect)
        n = int(sample_size)
        sims = int(simulations)
    except (TypeError, ValueError):
        raise ValueError("joint power requires finite effect, sigma, sample size and simulations")
    if sigma is None or not math.isfinite(effect) or n < 2 or sims < 100:
        raise ValueError("joint power requires sigma>0, sample_size>=2 and simulations>=100")
    if battery_fn is None:
        raise ValueError("battery_fn is required for joint battery power")

    import numpy as np

    rng = np.random.default_rng(int(seed))
    joint_pass = 0
    gate_pass_counts: dict[str, int] = {}
    for _ in range(sims):
        sample = rng.normal(loc=effect, scale=sigma, size=n)
        result = battery_fn(sample)
        if isinstance(result, dict):
            gates = {str(k): bool(v) for k, v in result.items()}
        else:
            gates = {"primary": bool(result)}
        for name, passed in gates.items():
            gate_pass_counts[name] = gate_pass_counts.get(name, 0) + int(passed)
        if gates and all(gates.values()):
            joint_pass += 1
    return {
        "joint_power": joint_pass / sims,
        "marginal_power": {k: v / sims for k, v in sorted(gate_pass_counts.items())},
        "simulations": sims,
        "seed": int(seed),
        "method": "JOINT_MONTE_CARLO_DEPENDENT_BATTERY",
    }


def assess_pretrial_feasibility_v2(
    *,
    expected_gross_effect: float | None,
    expected_cost: float,
    sigma_period: float | None,
    effective_periods: int | None,
    t_bar: float,
    source: str | None,
    horizon: str | None,
    minimum_power: float = 0.80,
    volatility_provenance: str | None = None,
    cost_provenance: str | None = None,
    effect_provenance: str | None = None,
    joint_power: float | None = None,
    joint_power_method: str | None = None,
    t_bar_name: str | None = None,
) -> dict:
    """V11.1 feasibility gate: require prospective power, not just MDE.

    Historical ``assess_pretrial_feasibility`` is intentionally left intact so
    prior trial records remain reproducible.  New trials use this explicit
    power contract.
    """
    try:
        cost = float(expected_cost)
        bar = float(t_bar)
        min_power = float(minimum_power)
    except (TypeError, ValueError):
        raise ValueError("invalid feasibility V2 numeric inputs")
    if not (0.5 < min_power < 1.0):
        raise ValueError("minimum_power must be between 0.5 and 1.0")

    sigma = _finite_positive(sigma_period)
    try:
        periods = int(effective_periods) if effective_periods is not None else None
    except (TypeError, ValueError):
        periods = None
    gross = None if expected_gross_effect is None else float(expected_gross_effect)
    net = None if gross is None else gross - cost
    primary_power = normal_approx_power(
        true_effect=net,
        sigma_day=sigma,
        effective_days=periods,
        t_bar=bar,
    ) if net is not None else None
    mde = minimum_detectable_effect(sigma_day=sigma, effective_days=periods, t_bar=bar)
    required_net_effect = None
    required_periods = None
    if sigma is not None and periods and periods > 0 and math.isfinite(bar):
        z_beta = NormalDist().inv_cdf(min_power)
        required_net_effect = (bar + z_beta) * sigma / math.sqrt(periods)
        if net is not None and math.isfinite(net) and net > 0:
            required_periods = int(math.ceil((((bar + z_beta) * sigma) / net) ** 2))

    out = {
        "source": source,
        "horizon": horizon,
        "effect_provenance": effect_provenance or source,
        "expected_gross_effect": gross,
        "expected_cost": cost,
        "expected_net_effect": net,
        "cost_provenance": cost_provenance,
        "sigma_period": sigma,
        "volatility_provenance": volatility_provenance,
        "effective_periods": periods,
        "t_bar": bar,
        "t_bar_name": t_bar_name or "UNNAMED_T_BAR",
        "minimum_power": min_power,
        "old_mde_net_effect": mde,
        "required_net_effect": required_net_effect,
        "required_periods_for_minimum_primary_power": required_periods,
        "primary_power": primary_power,
        "joint_power": None,
        "joint_power_method": None,
        "feasible": False,
        "decision": None,
    }
    if gross is None or not source:
        out["decision"] = "DO_NOT_RUN_PRIOR_EFFECT_REQUIRED"
        return out
    if net is None or net <= 0:
        out["decision"] = "DO_NOT_RUN_COST_WALL"
        return out
    if sigma is None or not periods or primary_power is None:
        out["decision"] = "DO_NOT_RUN_POWER_INPUTS_REQUIRED"
        return out
    jp = primary_power if joint_power is None else float(joint_power)
    if not math.isfinite(jp) or jp < 0 or jp > 1:
        out["decision"] = "DO_NOT_RUN_POWER_INPUTS_REQUIRED"
        return out
    out["joint_power"] = jp
    out["joint_power_method"] = joint_power_method or (
        "PRIMARY_GATE_ONLY" if joint_power is None else "SUPPLIED_JOINT_BATTERY_POWER"
    )
    if primary_power < min_power or jp < min_power:
        out["decision"] = "DO_NOT_RUN_UNDERPOWERED"
        return out
    out["feasible"] = True
    out["decision"] = "GO_REGISTER_PREREGISTERED_TRIAL"
    return out
