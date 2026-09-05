import math
import numpy as np
import pandas as pd


def test_candidate_metrics_reports_required_risk_and_turnover_fields():
    from app.v111_development import candidate_metrics
    idx = pd.date_range("2018-01-31", periods=36, freq="ME")
    net = pd.Series(np.linspace(-0.02, 0.03, len(idx)), index=idx)
    econ = pd.DataFrame({
        "gross": net + 0.001,
        "turnover": np.linspace(0.2, 0.8, len(idx)),
        "measured_cost": 0.001,
        "net_measured": net,
        "stress_cost": 0.0036,
        "net_stress": net - 0.0026,
    }, index=idx)
    out = candidate_metrics(econ)
    for key in [
        "gross_annualized_mean", "net_annualized_mean", "annualized_volatility",
        "sharpe", "sortino", "max_drawdown", "worst_month", "skewness",
        "kurtosis", "cvar_5", "average_turnover", "median_turnover",
        "stress_net_monthly_mean", "stress_net_annualized_mean",
        "complete_months", "positive_blocks", "top3_removed_mean_net",
    ]:
        assert key in out
    assert out["complete_months"] == 36


def test_projected_power_uses_development_measured_sigma_and_80pct_gate():
    from app.v111_development import project_confirmatory_power
    idx = pd.date_range("2018-01-31", periods=48, freq="ME")
    # modest edge with high variance: should be underpowered on 31 months
    gross = pd.Series([0.006 + (0.04 if i % 2 == 0 else -0.04) for i in range(48)], index=idx)
    econ = pd.DataFrame({
        "gross": gross,
        "turnover": 0.35,
        "measured_cost": 0.00063,
        "net_measured": gross - 0.00063,
        "stress_cost": 0.0036,
        "net_stress": gross - 0.0036,
    }, index=idx)
    out = project_confirmatory_power(econ, validation_months=31, simulations=3000, seed=19)
    assert out["volatility_provenance"] == "DEVELOPMENT_TARGET_MARKET"
    assert out["minimum_power"] == 0.80
    assert out["joint_power_method"] == "JOINT_MONTE_CARLO_DEPENDENT_BATTERY"
    assert out["decision"] == "DO_NOT_RUN_UNDERPOWERED"


def _candidate(name, *, power, sharpe, turnover, net=0.01, integrity=True, coverage=True):
    return {
        "name": name,
        "integrity_pass": integrity,
        "execution_coverage_pass": coverage,
        "metrics": {
            "net_monthly_mean": net,
            "sharpe": sharpe,
            "average_turnover": turnover,
        },
        "power": {"joint_power": power, "decision": "GO_REGISTER_PREREGISTERED_TRIAL" if power >= 0.80 else "DO_NOT_RUN_UNDERPOWERED"},
    }


def test_no_development_winner_when_both_candidates_are_underpowered():
    from app.v111_development import select_development_winner
    out = select_development_winner(_candidate("A", power=0.55, sharpe=1.0, turnover=0.2), _candidate("B", power=0.60, sharpe=2.0, turnover=0.1))
    assert out["winner"] is None
    assert out["status"] == "NO_DEVELOPMENT_WINNER"


def test_higher_sharpe_wins_only_after_both_clear_hard_gates():
    from app.v111_development import select_development_winner
    out = select_development_winner(_candidate("A", power=0.85, sharpe=0.8, turnover=0.3), _candidate("B", power=0.90, sharpe=1.0, turnover=0.5))
    assert out["winner"] == "B"
    assert out["status"] == "ELIGIBLE_FOR_TRIAL_25"


def test_lower_turnover_breaks_practical_sharpe_tie():
    from app.v111_development import select_development_winner
    out = select_development_winner(_candidate("A", power=0.85, sharpe=0.8000, turnover=0.25), _candidate("B", power=0.90, sharpe=0.8004, turnover=0.40))
    assert out["winner"] == "A"
    assert out["tie_break"] == "LOWER_TURNOVER"


def test_project_power_fails_closed_for_zero_variance_development_returns():
    import pandas as pd
    from app import v111_development as dev

    idx = pd.date_range("2019-01-31", periods=18, freq="ME")
    economics = pd.DataFrame({
        "gross": [0.01] * len(idx),
        "measured_cost": [0.001] * len(idx),
        "net_measured": [0.009] * len(idx),
    }, index=idx)

    out = dev.project_confirmatory_power(economics, validation_months=31, simulations=1000)
    assert out["decision"] == "DO_NOT_RUN_POWER_INPUTS_REQUIRED"
    assert out["feasible"] is False
    assert out["required_periods_for_minimum_primary_power"] is None
