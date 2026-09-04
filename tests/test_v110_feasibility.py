import math


def test_v11_feasibility_selects_residual_momentum_without_outcomes():
    from app import v11_research
    out = v11_research.feasibility_competition()
    assert out["outcome_data_read"] is False
    assert out["candidate_a"]["name"] == "PUBLISHED_RESIDUAL_MOMENTUM_12_1"
    assert out["candidate_a"]["assessment"]["feasible"] is True
    assert out["candidate_a"]["t_bar"] == v11_research.CONFIRMATORY_T_BAR
    assert out["candidate_b"]["assessment"]["feasible"] is False
    assert out["candidate_b"]["assessment"]["decision"] == "DO_NOT_RUN_PRIOR_EFFECT_REQUIRED"
    assert out["winner"] == "TRIAL24_RESIDUAL_MOMENTUM_REPLICATION"
    assert out["trial24_registered"] is True


def test_v11_prior_is_canonical_one_month_spread_and_costs_both_legs():
    from app import v11_research
    assert math.isclose(v11_research.RESIDUAL_MOMENTUM_ANNUAL_RETURN, 0.1120, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(v11_research.RESIDUAL_MOMENTUM_ANNUAL_VOL, 0.1249, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(v11_research.TRIAL24_SPREAD_ROUND_TRIP_COST, 0.0036, rel_tol=0, abs_tol=1e-12)
    expected = v11_research.RESIDUAL_MOMENTUM_ANNUAL_RETURN / 12.0
    assert math.isclose(v11_research.RESIDUAL_MOMENTUM_MONTHLY_GROSS_PRIOR, expected, rel_tol=0, abs_tol=1e-12)


def test_v11_trial24_spec_keeps_final_20_percent_unread():
    from app import v11_research
    spec = v11_research.trial24_spec()
    assert spec["formation"] == "12-1M"
    assert spec["beta_window_months"] == 36
    assert spec["holding_months"] == 1
    assert spec["portfolio"] == "TOP_MINUS_BOTTOM_DECILE_200PCT_GROSS"
    assert spec["final_holdout_pct"] == 20
    assert spec["final_read"] is False
    assert spec["production_activation"] is False
