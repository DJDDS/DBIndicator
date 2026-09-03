import pandas as pd
from app import backtest
from app import v10_directional_edge as v10


def _price(idx, drift=0.001):
    close=100*(1+drift)**pd.Series(range(len(idx)),index=idx)
    return pd.DataFrame({"open":close*0.999,"high":close*1.01,"low":close*0.99,"close":close},index=idx)


def test_v10_runner_is_research_only_and_keeps_trial23_locked(monkeypatch):
    idx=pd.bdate_range("2018-05-01", "2026-08-31")
    px=_price(idx,0.001)
    near_exp=pd.Series(idx+pd.Timedelta(days=20),index=idx)
    next_exp=pd.Series(idx+pd.Timedelta(days=50),index=idx)
    hist={"membership":pd.Series(True,index=idx),"near_settle":px.close*1.001,"next_settle":px.close*1.002,
          "near_expiry":near_exp,"next_expiry":next_exp,"near_dte":pd.Series(20,index=idx),"next_dte":pd.Series(50,index=idx)}
    integrity={"nse_history_by_symbol":{"AAA":hist,"_meta":{"date_coverage":1.0}},
               "nse_cash_by_symbol":{"AAA":px,"_meta":{"date_coverage":1.0}},
               "market_history":_price(idx,0.0005),
               "sector_history_by_symbol":{"NIFTY IT":_price(idx,0.0007)},
               "sector_map":{"AAA":"NIFTY IT"}}
    result=backtest.run_v10_directional_lab(None,symbols=["AAA"],integrity_data=integrity)
    assert result["build"] == v10.BUILD_ID
    assert result["research_only"] is True
    assert result["production_activation"] is False
    assert result["trial23_state"] == "LOCKED_PENDING_TRIAL21_AND_22"
    assert result["final_read"] is False
    assert result["trial21"]["final_locked"] is True
    assert result["trial22"]["final_locked"] is True

def test_v10_runner_compacts_symbol_frames_before_cross_sectional_evaluation(monkeypatch):
    idx=pd.bdate_range("2018-05-01", periods=120)
    px=_price(idx,0.001)
    hist={"membership":pd.Series(True,index=idx),"near_settle":px.close*1.001,"next_settle":px.close*1.002,
          "near_expiry":pd.Series(idx+pd.Timedelta(days=20),index=idx),"next_expiry":pd.Series(idx+pd.Timedelta(days=50),index=idx)}
    integrity={"nse_history_by_symbol":{"AAA":hist,"_meta":{"date_coverage":1.0}},"nse_cash_by_symbol":{"AAA":px,"_meta":{"date_coverage":1.0}},
               "market_history":_price(idx,0.0005),"sector_history_by_symbol":{"NIFTY IT":_price(idx,0.0007)},"sector_map":{"AAA":"NIFTY IT"}}
    seen={}
    def fake_eval(t21,t22,**kwargs):
        seen['t21']=set(t21['AAA'].columns); seen['t22']=set(t22['AAA'].columns)
        return {"trial21":{"final_locked":True},"trial22":{"final_locked":True},"trial23_state":"LOCKED_PENDING_TRIAL21_AND_22"}
    monkeypatch.setattr(backtest.v10_directional_edge,'evaluate_v10',fake_eval)
    backtest.run_v10_directional_lab(None,symbols=['AAA'],integrity_data=integrity)
    assert 'high' not in seen['t21'] and 'low' not in seen['t21']
    assert 'high' not in seen['t22'] and 'low' not in seen['t22']
    assert {'resid_5d','sector_5d','abs_ret_20d','long_1d_net','short_1d_net'} <= seen['t21']
    assert {'basis_innovation_z','curve_slope_ann','long_1d_net','short_1d_net'} <= seen['t22']
