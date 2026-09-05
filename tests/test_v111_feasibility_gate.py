import math


def test_v2_refuses_effect_that_clears_old_mde_but_has_less_than_80pct_power():
    from app import research_feasibility as rf
    sigma = 0.04
    n = 100
    t_bar = 1.645
    old_mde = rf.minimum_detectable_effect(sigma_day=sigma, effective_days=n, t_bar=t_bar)
    effect = old_mde * 1.05
    old = rf.assess_pretrial_feasibility(
        prior_gross_effect=effect,
        round_trip_cost=0.0,
        sigma_day=sigma,
        effective_days=n,
        t_bar=t_bar,
        source="external prior",
        horizon="1_MONTH",
    )
    assert old["feasible"] is True

    new = rf.assess_pretrial_feasibility_v2(
        expected_gross_effect=effect,
        expected_cost=0.0,
        sigma_period=sigma,
        effective_periods=n,
        t_bar=t_bar,
        source="external prior",
        horizon="1_MONTH",
        minimum_power=0.80,
        volatility_provenance="DEVELOPMENT",
        cost_provenance="MEASURED_TURNOVER",
    )
    assert new["primary_power"] < 0.80
    assert new["feasible"] is False
    assert new["decision"] == "DO_NOT_RUN_UNDERPOWERED"
    assert new["required_net_effect"] > old_mde


def test_development_volatility_supersedes_external_source_volatility():
    from app import research_feasibility as rf
    selected = rf.select_target_sigma(
        development_sigma=0.07,
        external_sigma=0.036,
        development_source="V11.1 development monthly net returns",
        external_source="published source market",
    )
    assert math.isclose(selected["sigma"], 0.07)
    assert selected["provenance"] == "DEVELOPMENT_TARGET_MARKET"
    assert "V11.1" in selected["source"]


def test_joint_power_simulates_dependent_battery_instead_of_multiplying_marginals():
    from app import research_feasibility as rf

    def identical_two_gate_battery(sample):
        passed = float(sample.mean()) > 0.0
        return {"primary": passed, "same_gate_again": passed}

    out = rf.estimate_joint_battery_power(
        true_effect=0.002,
        sigma_period=0.02,
        sample_size=24,
        simulations=6000,
        seed=17,
        battery_fn=identical_two_gate_battery,
    )
    p = out["marginal_power"]["primary"]
    assert abs(out["joint_power"] - p) < 1e-12
    assert abs(out["joint_power"] - (p * p)) > 0.05


def test_v2_reports_required_periods_for_declared_minimum_primary_power():
    from app import research_feasibility as rf

    out = rf.assess_pretrial_feasibility_v2(
        expected_gross_effect=0.006,
        expected_cost=0.001,
        sigma_period=0.04,
        effective_periods=31,
        t_bar=1.645,
        source="development",
        horizon="1_MONTH",
        minimum_power=0.80,
        volatility_provenance="DEVELOPMENT_TARGET_MARKET",
        cost_provenance="MEASURED_TURNOVER",
        effect_provenance="DEVELOPMENT_TARGET_MARKET",
    )
    z_beta = rf.NormalDist().inv_cdf(0.80)
    expected = math.ceil((((1.645 + z_beta) * 0.04) / 0.005) ** 2)
    assert out["required_periods_for_minimum_primary_power"] == expected
    assert expected > 31


def test_v2_required_periods_is_none_when_expected_net_effect_is_not_positive():
    from app import research_feasibility as rf

    out = rf.assess_pretrial_feasibility_v2(
        expected_gross_effect=0.001,
        expected_cost=0.002,
        sigma_period=0.04,
        effective_periods=31,
        t_bar=1.645,
        source="development",
        horizon="1_MONTH",
        minimum_power=0.80,
    )
    assert out["required_periods_for_minimum_primary_power"] is None
    assert out["decision"] == "DO_NOT_RUN_COST_WALL"
