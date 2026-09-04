import numpy as np
import pandas as pd
from app import v10_directional_edge as v10


def _events(n_days=140, per_day=2, ret=0.004):
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    rows=[]
    for i,d in enumerate(dates):
        for j in range(per_day):
            rows.append({"date":d,"symbol":f"S{j+(i%20):02d}","sector":f"SEC{(j+i)%8}","net":ret + (0.0002 if i%2 else -0.0001)})
    return pd.DataFrame(rows)


def test_direction_report_passes_only_with_all_frozen_gates():
    ev=_events()
    r=v10._direction_report(ev, "net")
    assert r["event_count"] >= 250
    assert r["event_days"] >= 120
    assert r["mean_net"] > 0
    assert r["profit_factor_infinite"] is True or r["profit_factor"] >= 1.25
    assert r["day_cluster_t"] >= 3.0
    assert r["positive_blocks"] >= 3
    assert r["top3_removed_mean"] > 0
    assert r["pass"] is True


def test_direction_report_fails_negative_expectancy_even_with_many_events():
    ev=_events(ret=-0.003)
    r=v10._direction_report(ev, "net")
    assert r["pass"] is False
    assert "EXPECTANCY" in r["failed_gates"]


def test_evaluate_v10_keeps_trial23_locked():
    out=v10.evaluate_v10({}, {})
    assert out["trial23_state"] == "CLOSED_COMPONENT_TRIALS_FAILED_NOT_EVALUATED"
    assert out["production_activation"] is False
    assert out["active_playbooks_unchanged"] is True

def test_trial_reports_include_2d_secondary_but_do_not_use_it_to_rescue():
    idx=pd.bdate_range("2018-09-03", periods=900)
    frame=pd.DataFrame(index=idx)
    frame["date"]=idx; frame["resid_5d"]=np.linspace(-2,2,len(idx)); frame["sector_5d"]=frame["resid_5d"]
    frame["abs_ret_20d"]=np.sign(frame["resid_5d"])*0.05
    frame["long_1d_net"]=-0.001; frame["short_1d_net"]=-0.001
    frame["long_2d_net"]=0.01; frame["short_2d_net"]=0.01
    out=v10.evaluate_trial21({"AAA":frame}, bootstrap_reps=20)
    assert "bull_2d" in out and "bear_2d" in out
    assert out["pass"] is False

def test_direction_report_is_strict_json_safe_when_no_losses():
    import json
    ev=_events(ret=0.004)
    r=v10._direction_report(ev,'net',bootstrap_reps=20)
    # Browser APIs must never receive Infinity/NaN tokens.
    json.dumps(r, allow_nan=False)
    assert r['profit_factor_infinite'] is True
    assert r['profit_factor'] is None

def test_evaluate_v10_is_strict_json_safe_with_missing_outcomes():
    import json
    idx=pd.bdate_range('2018-09-03', periods=100)
    f=pd.DataFrame(index=idx)
    f['basis_innovation_z']=2.0; f['curve_slope_ann']=0.1
    for c in ('long_1d_net','short_1d_net','long_2d_net','short_2d_net'): f[c]=np.nan
    out=v10.evaluate_trial22({'AAA':f}, bootstrap_reps=10)
    json.dumps(out, allow_nan=False)
