import numpy as np
import pandas as pd


def _inputs(periods=84):
    idx = pd.date_range("2016-01-31", periods=periods, freq="ME")
    rng = np.random.default_rng(222)
    fac = pd.DataFrame({
        "rm_rf": rng.normal(0.005, 0.025, periods),
        "smb": rng.normal(0.0, 0.018, periods),
        "hml": rng.normal(0.0, 0.018, periods),
        "rf": np.full(periods, 0.003),
    }, index=idx)
    rets = {}
    for j in range(20):
        idio = rng.normal((j - 9.5) * 0.0002, 0.035, periods)
        rets[f"S{j:02d}"] = 0.003 + 0.7 * fac.rm_rf + 0.1 * fac.smb - 0.1 * fac.hml + idio
    monthly = pd.DataFrame(rets, index=idx)
    membership = pd.DataFrame(True, index=idx, columns=monthly.columns)
    contracts = {}
    for d in idx:
        contracts[d] = {
            s: {"expiry": (d + pd.offsets.MonthEnd(1)).date().isoformat(), "lot_size_available": True,
                "price_available": True, "price_field": "settle"}
            for s in monthly.columns
        }
    return {
        "monthly_returns": monthly,
        "factors": fac,
        "membership": membership,
        "futures_contracts_by_month": contracts,
        "data_readiness": True,
        "meta": {"member_return_coverage": 1.0, "factor_coverage": 1.0, "manifest_sha256": "fixture"},
    }


def test_v111_lab_is_development_only_exactly_two_candidates_and_never_runs_trial25():
    from app.v111_lab import run_development_lab
    out = run_development_lab(_inputs(), validation_months=31, power_simulations=800)
    assert out["status"] == "DEVELOPMENT_ONLY_NO_TRIAL25_YET"
    assert out["development_window"] == "2010-01_TO_2023-05"
    assert out["final_months_unread"] == 31
    assert out["final_read"] is False
    assert out["production_activation"] is False
    assert out["trial25_run"] is False
    assert len(out["candidates"]) == 2
    assert [x["name"] for x in out["candidates"]] == [
        "RESIDUAL_MOMENTUM_VOL_DERISK_12_1",
        "LIQUID_PRICE_MOMENTUM_VOL_DERISK_12_1",
    ]


def test_v111_restates_trial24_without_changing_its_registered_verdict():
    from app.v111_lab import trial24_restatement
    out = trial24_restatement()
    assert out["verdict"] == "FAIL_REPLICATION_PRE_FINAL"
    assert out["registered_verdict_changed"] is False
    assert out["final_read"] is False
    assert out["final_months_unread"] == 31
    assert out["observed_net_monthly"] == 0.00188
    assert out["implied_gross_monthly"] == 0.00548
    assert out["realized_annualized_volatility"] == 0.243


def test_v111_candidate_results_cannot_activate_production_even_if_development_passes():
    from app.v111_lab import run_development_lab
    out = run_development_lab(_inputs(), validation_months=31, power_simulations=800)
    for c in out["candidates"]:
        assert c["production_activation"] is False
        assert c["final_read"] is False
    assert out["selection"]["production_activation"] is False


def test_v111_candidate_volatility_targets_are_externally_frozen_not_searched():
    from app.v111_lab import run_development_lab
    out = run_development_lab(_inputs(), validation_months=31, power_simulations=800)
    a, b = out["candidates"]
    assert a["volatility_target"] == 0.1249
    assert a["volatility_target_provenance"] == "BLITZ_HUIJ_MARTENS_CANONICAL_RESIDUAL_MOMENTUM_12P49PCT"
    assert b["volatility_target"] == 0.19
    assert b["volatility_target_provenance"] == "BARROSO_SANTA_CLARA_RISK_MANAGED_MOMENTUM_19PCT_REFERENCE"
