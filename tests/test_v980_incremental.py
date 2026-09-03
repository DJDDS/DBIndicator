import numpy as np
import pandas as pd
import pytest

from app import v98_incremental_oi as v98


def _frozen_pass():
    return {"gates": {"sample_ok": True, "matched_lift_ok": True, "binary_event_t_ok": True, "tail_ok": True, "stability_ok": True}}


def _stacked(effect=0.00010, volume_event_corr=0.0, days=90, symbols=30):
    rows=[]
    rng=np.random.default_rng(98)
    dates=pd.bdate_range('2020-01-01', periods=days)
    for di,d in enumerate(dates):
        common=0.00020 + 0.00004*np.sin(di/7)
        for si in range(symbols):
            event = (si % 10 == di % 10)
            hd=0.00008 + 0.00001*(si%5) + rng.normal(0, 0.000003)
            hw=0.00009 + 0.000005*(si%4)
            hm=0.00010 + 0.000004*(si%3)
            vz=rng.normal(0, 1) + (volume_event_corr if event else 0.0)
            y=max(1e-8, common + 0.35*hd + 0.20*hw + 0.15*hm + 0.00001*vz + (effect if event else 0) + rng.normal(0, 0.000015))
            rows.append({
                'date':d,'symbol':f'S{si:02d}','dte_bucket':'11-20','trial19_eligible':True,
                'extreme_oi_event':event,'next_yz_var':y,'next_gk_var':y*0.92,
                'har_daily_var':hd,'har_weekly_var':hw,'har_monthly_var':hm,'futures_volume_z':vz,
            })
    return pd.DataFrame(rows)


def test_v980_incremental_core_requires_frozen_trial19_efficacy(monkeypatch):
    bad=_frozen_pass(); bad['gates']['binary_event_t_ok']=False
    out=v98.evaluate_incremental_core({}, frozen_result=bad)
    assert out['status']=='LOCKED_TRIAL19_EFFICACY_NOT_PASSED'
    assert out['pass'] is False


def test_v980_har_and_volume_horse_race_keep_strong_independent_oi(monkeypatch):
    df=_stacked(effect=0.00010, volume_event_corr=0.4)
    monkeypatch.setattr(v98, '_stack', lambda frames: df.copy())
    out=v98.evaluate_incremental_core({'x':pd.DataFrame()}, frozen_result=_frozen_pass(), bootstrap_reps=40)
    assert out['status']=='PASS_INCREMENTAL_CORE'
    assert out['primary_target']=='next_yz_var'
    assert out['har_plus_oi']['t']['extreme_oi_event'] >= 3.0
    assert out['har_volume_oi']['t']['extreme_oi_event'] >= 3.0
    assert out['incremental_r2']['delta_r2'] > 0
    assert out['matched_variance']['yang_zhang']['lift'] > 1.0
    assert out['matched_variance']['garman_klass']['lift'] > 1.0


def test_v980_volume_can_make_oi_fail_incremental_gate(monkeypatch):
    # Outcome is driven by volume; event is correlated with volume but has no own effect.
    df=_stacked(effect=0.0, volume_event_corr=3.0)
    df['next_yz_var'] += 0.00008 * df['futures_volume_z']
    df['next_gk_var'] = df['next_yz_var'] * 0.92
    monkeypatch.setattr(v98, '_stack', lambda frames: df.copy())
    out=v98.evaluate_incremental_core({'x':pd.DataFrame()}, frozen_result=_frozen_pass(), bootstrap_reps=30)
    assert out['har_volume_oi']['t'].get('futures_volume_z') is not None
    assert out['pass'] is False or out['har_volume_oi']['t'].get('extreme_oi_event', 0) < 3.0


def test_v980_final_gate_never_unlocks_trial18_and_fails_closed_on_earnings():
    core={'pass':True,'status':'PASS_INCREMENTAL_CORE'}
    bad=v98.finalize_v98(core, earnings_control={'audit_valid':False,'outside_earnings_pass':False})
    assert bad['status']=='INCONCLUSIVE_EARNINGS_JOIN'
    assert bad['trial18_state']=='LOCKED'
    good=v98.finalize_v98(core, earnings_control={'audit_valid':True,'outside_earnings_pass':True})
    assert good['status']=='PASS_INCREMENTAL_OI'
    assert good['trial18_state']=='LOCKED'
    assert good['eligible_for_direction_preregistration'] is False
