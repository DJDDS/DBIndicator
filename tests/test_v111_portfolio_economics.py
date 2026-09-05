import math
import numpy as np
import pandas as pd


def test_derisk_scaler_never_levers_and_uses_frozen_target():
    from app.v111_development import derisk_exposure, PRIMARY_TARGET_ANNUAL_VOL
    assert derisk_exposure(0.05) == 1.0
    assert math.isclose(derisk_exposure(PRIMARY_TARGET_ANNUAL_VOL * 2), 0.5)
    assert 0.0 <= derisk_exposure(10.0) <= 1.0


def test_lagged_vol_forecast_does_not_use_current_month_return():
    from app.v111_development import lagged_realized_vol_forecast
    idx = pd.date_range("2020-01-31", periods=15, freq="ME")
    r = pd.Series(np.linspace(-0.03, 0.04, 15), index=idx)
    f1 = lagged_realized_vol_forecast(r, lookback_months=12)
    changed = r.copy()
    changed.iloc[-1] = 9.0
    f2 = lagged_realized_vol_forecast(changed, lookback_months=12)
    assert math.isclose(float(f1.iloc[-1]), float(f2.iloc[-1]), rel_tol=0, abs_tol=1e-12)


def test_scores_to_weights_and_turnover_use_explicit_200pct_gross_convention():
    from app.v111_development import scores_to_weights, portfolio_turnover
    d1 = pd.Timestamp("2022-01-31")
    d2 = pd.Timestamp("2022-02-28")
    rows = []
    for d, longs, shorts in [
        (d1, ["A", "B"], ["C", "D"]),
        (d2, ["A", "E"], ["C", "F"]),
    ]:
        for s in longs:
            rows.append({"date": d, "symbol": s, "score": 1.0, "decile": 10})
        for s in shorts:
            rows.append({"date": d, "symbol": s, "score": -1.0, "decile": 1})
    w = scores_to_weights(pd.DataFrame(rows))
    assert math.isclose(w.loc[d1].abs().sum(), 2.0)
    t = portfolio_turnover(w)
    assert math.isclose(t.loc[d1], 1.0)  # opening a 200%-gross book from cash
    assert math.isclose(t.loc[d2], 1.0)  # half of each leg is sold and replaced: 100% of gross-book notional traded


def test_measured_cost_and_stress_cost_are_separate_not_overwritten():
    from app.v111_development import apply_measured_costs
    idx = pd.date_range("2022-01-31", periods=2, freq="ME")
    gross = pd.Series([0.02, 0.01], index=idx)
    turnover = pd.Series([1.0, 0.35], index=idx)
    out = apply_measured_costs(gross, turnover, per_turnover_cost=0.0018, stress_cost=0.0036)
    assert list(out.columns) == ["gross", "turnover", "measured_cost", "net_measured", "stress_cost", "net_stress"]
    assert math.isclose(out.iloc[1]["measured_cost"], 0.00063)
    assert math.isclose(out.iloc[1]["net_measured"], 0.00937)
    assert math.isclose(out.iloc[1]["net_stress"], 0.0064)
    assert out.iloc[1]["net_measured"] != out.iloc[1]["net_stress"]
