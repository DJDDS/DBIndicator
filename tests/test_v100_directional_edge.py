import numpy as np
import pandas as pd
from app import v10_directional_edge as v10


def _prices(start="2020-01-01", n=90, drift=0.001):
    idx = pd.bdate_range(start, periods=n)
    r = np.full(n, drift)
    close = 100*np.cumprod(1+r)
    return pd.DataFrame({"open": close*0.999, "high": close*1.01, "low": close*0.99, "close": close}, index=idx)


def test_trial21_rolling_residual_uses_only_past_beta_window():
    stock = _prices(n=90, drift=0.002)
    market = _prices(n=90, drift=0.001)
    sector = _prices(n=90, drift=0.0012)
    a = v10.trial21_features(stock, market, sector)
    stock2 = stock.copy(); stock2.iloc[-1, stock2.columns.get_loc("close")] *= 10
    b = v10.trial21_features(stock2, market, sector)
    # A future mutation cannot change yesterday's feature.
    assert np.isclose(a["resid_5d"].iloc[-2], b["resid_5d"].iloc[-2], equal_nan=True)
    assert a["resid_5d"].iloc[:40].isna().all()


def test_directional_outcome_enters_next_open_and_charges_cost():
    idx = pd.bdate_range("2020-01-01", periods=4)
    f = pd.DataFrame({"open":[100,101,102,103],"close":[100,102,101,106]}, index=idx)
    out = v10.build_directional_outcomes(f)
    expected_long = 102/101 - 1 - v10.ROUND_TRIP_COST
    expected_short = 101/102 - 1 - v10.ROUND_TRIP_COST
    assert abs(out.loc[idx[0], "long_1d_net"] - expected_long) < 1e-12
    assert abs(out.loc[idx[0], "short_1d_net"] - expected_short) < 1e-12


def test_cross_sectional_trial21_rules_are_frozen():
    d = pd.Timestamp("2024-01-02")
    rows = pd.DataFrame({
        "date":[d,d,d,d], "symbol":["A","B","C","D"],
        "resid_5d":[4.0,2.0,-2.0,-4.0], "sector_5d":[3.0,2.0,-2.0,-3.0],
        "abs_ret_20d":[0.10,0.05,-0.05,-0.10]
    })
    out = v10.apply_trial21_cross_sectional_rules(rows)
    assert bool(out.loc[out.symbol.eq("A"), "trial21_bull"].iloc[0])
    assert bool(out.loc[out.symbol.eq("D"), "trial21_bear"].iloc[0])
    assert v10.TRIAL21_RESID_BULL_PCT == 90.0
    assert v10.TRIAL21_SECTOR_BULL_PCT == 70.0

def test_sector_percentile_ranks_unique_sectors_not_stock_count():
    d=pd.Timestamp("2024-01-02")
    rows=pd.DataFrame({
      "date":[d]*5,"symbol":["A1","A2","A3","B1","C1"],"sector":["A","A","A","B","C"],
      "resid_5d":[5,4,3,2,1],"sector_5d":[0.03,0.03,0.03,0.02,0.01],"abs_ret_20d":[0.1]*5})
    out=v10.apply_trial21_cross_sectional_rules(rows)
    # Unique-sector ranks are C=0, B=50, A=100 irrespective of 3 A constituents.
    assert set(out.loc[out.sector.eq("A"),"sector_pct"]) == {100.0}
    assert set(out.loc[out.sector.eq("B"),"sector_pct"]) == {50.0}
    assert set(out.loc[out.sector.eq("C"),"sector_pct"]) == {0.0}
