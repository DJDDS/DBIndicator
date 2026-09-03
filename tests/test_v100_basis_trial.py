import numpy as np
import pandas as pd
from app import v10_directional_edge as v10


def test_trial22_basis_is_annualized_and_curve_aware():
    idx = pd.bdate_range("2022-01-03", periods=80)
    spot = pd.Series(100.0, index=idx)
    near = pd.Series(101.0, index=idx)
    nxt = pd.Series(102.0, index=idx)
    near_exp = pd.Series(idx + pd.Timedelta(days=10), index=idx)
    next_exp = pd.Series(idx + pd.Timedelta(days=40), index=idx)
    frame = pd.DataFrame({"close": spot, "near_settle": near, "next_settle": nxt,
                          "near_expiry": near_exp, "next_expiry": next_exp}, index=idx)
    out = v10.trial22_features(frame, min_fit_obs=20, refit_every=10)
    assert np.isclose(out["near_basis_ann"].iloc[-1], np.log(1.01)*365/10)
    assert out["curve_slope_ann"].iloc[-1] > 0
    assert out["basis_innovation_z"].iloc[:20].isna().all()


def test_trial22_rule_does_not_use_oi_or_volume():
    d = pd.Timestamp("2024-01-02")
    rows = pd.DataFrame({"date":[d,d], "symbol":["A","B"],
                         "basis_innovation_z":[1.6,-1.7], "curve_slope_ann":[0.02,-0.03],
                         "open_interest":[np.nan,np.nan], "volume":[np.nan,np.nan]})
    out = v10.apply_trial22_rules(rows)
    assert bool(out.loc[0,"trial22_bull"])
    assert bool(out.loc[1,"trial22_bear"])
    assert v10.TRIAL22_BASIS_Z == 1.5
