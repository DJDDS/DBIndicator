from pathlib import Path

import numpy as np
import pandas as pd

from app import v99_volume_gate as v99

ROOT = Path(__file__).resolve().parents[1]
BUILD = "2026-09-03-INSTITUTIONAL-V9.9.2-TRIAL20-LOG-RV-INTEGRITY-CLOSURE"


def test_log_variance_fit_returns_training_only_smearing_factor():
    X = np.array([[0.0], [1.0], [2.0], [3.0], [4.0]], dtype=float)
    y = np.exp(np.array([-4.2, -3.8, -3.1, -2.9, -2.3], dtype=float))

    beta, smear = v99._fit_log_variance_model(y, X)

    A = np.column_stack([np.ones(len(X)), np.log(np.maximum(X, v99.VAR_FLOOR))])
    # Production helper logs variance regressors; construct expected residuals
    # through the public helper's returned coefficients rather than re-fitting in levels.
    log_y = np.log(y)
    residual = log_y - A @ beta
    expected_smear = float(np.mean(np.exp(residual)))
    assert np.isclose(smear, expected_smear)
    assert smear > 0


def test_log_rv_oos_forecasts_are_positive_and_future_targets_do_not_leak():
    dates = pd.bdate_range("2014-06-02", "2015-10-30")
    n = len(dates)
    daily = np.linspace(0.00012, 0.00055, n)
    weekly = pd.Series(daily).rolling(5, min_periods=1).mean().to_numpy()
    monthly = pd.Series(daily).rolling(22, min_periods=1).mean().to_numpy()
    abnormal = np.sin(np.arange(n) / 11.0)
    target = np.exp(-8.2 + 0.35 * np.log(daily) + 0.20 * np.log(weekly) + 0.15 * np.log(monthly) + 0.06 * abnormal)
    frame = pd.DataFrame({
        "date": dates,
        "symbol": "AAA",
        "har_daily_var": daily,
        "har_weekly_var": weekly,
        "har_monthly_var": monthly,
        "abnormal_futstk_volume": abnormal,
        "next_yz_var": target,
        "dte_bucket": "11-20",
    })

    p1 = v99._oos_prediction_rows(frame, "next_yz_var", min_train_obs=80, refit_every=20)
    assert not p1.empty
    assert (p1["har_forecast"] > 0).all()
    assert (p1["augmented_forecast"] > 0).all()

    cutoff = pd.Timestamp("2015-09-15")
    changed = frame.copy()
    changed.loc[changed["date"] > cutoff, "next_yz_var"] *= 10_000.0
    p2 = v99._oos_prediction_rows(changed, "next_yz_var", min_train_obs=80, refit_every=20)

    left = p1.loc[p1["date"] <= cutoff, ["date", "har_forecast", "augmented_forecast"]].reset_index(drop=True)
    right = p2.loc[p2["date"] <= cutoff, ["date", "har_forecast", "augmented_forecast"]].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, check_exact=False, rtol=1e-12, atol=1e-15)


def test_forecast_integrity_reports_floor_hits_and_extrema():
    pred = pd.DataFrame({
        "har_forecast": [0.01, v99.VAR_FLOOR, 0.03],
        "augmented_forecast": [0.02, 0.04, v99.VAR_FLOOR],
    })
    diag = v99._forecast_integrity(pred)
    assert diag["har_floor_hits"] == 1
    assert diag["augmented_floor_hits"] == 1
    assert diag["har_min"] == v99.VAR_FLOOR
    assert diag["augmented_max"] == 0.04


def test_trial20_closure_never_promotes_a_post_observation_repair():
    fail = v99._closure_interpretation(False)
    assert fail["status"] == "CLOSED_REJECTED_LOG_RV_CONFIRMED"
    assert fail["promotion_allowed"] is False

    repaired_pass = v99._closure_interpretation(True)
    assert repaired_pass["status"] == "SPECIFICATION_SENSITIVE_NOT_PROMOTED"
    assert repaired_pass["promotion_allowed"] is False


def test_v992_release_and_ui_expose_log_rv_integrity_closure():
    assert v99.BUILD_ID == BUILD
    spec = v99.trial20_spec()
    assert spec["forecast_space"] == "log_realized_variance"
    assert spec["back_transform"] == "training_only_lognormal_smearing"
    assert spec["volume_threshold"] is None
    assert spec["window"] == ["2015-09-01", "2018-08-31"]

    current = "2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE"
    assert (ROOT / "RESEARCH_BUILD.txt").read_text().strip() == current
    assert (ROOT / "PRODUCTION_BUILD.txt").read_text().strip() == '2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
    html = (ROOT / "app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V9.9.2 / Trial 20 · Log-RV Integrity Closure" in html
    assert "Forecast integrity" in html
    assert "variance×1e6 units" in html
    assert "(need ≥" in html
    assert "stability.blocks||[]).length" in html
    assert "SPECIFICATION SENSITIVE NOT PROMOTED" in html or "SPECIFICATION_SENSITIVE_NOT_PROMOTED" in html


def test_v992_does_not_display_a_persisted_v991_result_as_current(monkeypatch, tmp_path):
    from app import backtest
    state_path = tmp_path / "v99-state.json"
    state_path.write_text('{"status":"done","result":{"build":"2026-09-03-INSTITUTIONAL-V9.9.1-TRIAL20-CLUSTER-PERFORMANCE-HOTFIX"}}')
    monkeypatch.setattr(backtest, "_V99_STATE_PATH", state_path)

    state = backtest._load_v99_state()

    assert state["status"] == "idle"
    assert state["result"] is None
    assert state["build"] == BUILD
