import numpy as np
import pandas as pd


def test_trial19_spec_is_nonlinear_frozen_and_third_window():
    from app import v97_trial19 as t19
    spec=t19.trial19_spec()
    assert spec['trial_number']==19
    assert spec['total_oi_z_min']==1.5
    assert spec['independent_start']=='2018-09-01'
    assert spec['independent_end']=='2021-08-31'
    assert spec['primary_baseline']=='SAME_DAY_SAME_DTE_NON_EVENT'
    assert spec['inference_variable']=='extreme_oi_event'
    assert spec['primary_horizon']=='1D'
    assert spec['directional_prediction'] is False
    assert t19.trial18_spec()['locked'] is True


def test_same_day_dte_matched_report_excludes_events_and_wrong_dte():
    from app import v97_trial19 as t19
    df=pd.DataFrame({
        'date':pd.to_datetime(['2020-01-02']*5 + ['2020-01-03']*2),
        'symbol':['EV','A','B','C','D','X','Y'],
        'movement_1d_atr':[2.0,1.0,1.0,9.0,9.0,0.5,0.5],
        'nse_near_dte':[3,3,3,15,15,3,3],
        'extreme_oi_event':[True,False,False,False,False,False,False],
    })
    ev=df[df.extreme_oi_event].copy()
    out=t19.same_day_dte_matched_report(ev, df, 'movement_1d_atr', reps=40)
    assert out['event_count']==1
    assert out['baseline_count']==2
    assert out['matched_groups']==1
    assert round(out['lift'],6)==2.0


def test_binary_event_two_way_inference_detects_within_day_dte_effect():
    from app import v97_trial19 as t19
    rng=np.random.default_rng(19)
    rows=[]
    days=pd.bdate_range('2019-01-02', periods=80)
    syms=[f'S{i}' for i in range(12)]
    for di,d in enumerate(days):
        for si,s in enumerate(syms):
            event=(si in (0,1,2))
            dte=3 if si<6 else 15
            y=1.0 + (0.25 if event else 0.0) + 0.01*(di%4) + rng.normal(scale=0.04)
            rows.append((d,s,dte,event,y,0.2,0.02))
    df=pd.DataFrame(rows,columns=['date','symbol','nse_near_dte','extreme_oi_event','movement_1d_atr','realized_vol20_prev','atr_pct_prev'])
    out=t19.binary_event_two_way_report(df)
    assert out['date_clusters']==80
    assert out['symbol_clusters']==6
    assert out['coef']['extreme_oi_event']>0.15
    assert out['t']['extreme_oi_event']>3.0


def test_trial19_integrity_is_fail_closed_even_when_effect_metrics_are_supplied():
    from app import v97_trial19 as t19
    # Empty evaluator must never pretend validation or unlock Trial 18.
    out=t19.evaluate_trial19({}, controls={'historical_membership_available':False,'historical_cash_price_available':False,'lot_size_normalization_available':False,'mwpl_available':False}, bootstrap_reps=10)
    assert out['status']=='INCONCLUSIVE_NO_DATA'
    assert out['trial18']['locked'] is True
    assert out['production_activation'] is False


def test_trial19_pass_gate_uses_binary_event_t_not_continuous_total_z(monkeypatch):
    from app import v97_trial19 as t19
    # Directly verify verdict helper so continuous-z inference cannot be reintroduced.
    status=t19.trial19_verdict(
        event_count=300,event_days=260,matched_lift=1.12,ci_low=1.05,
        binary_event_t=3.2,top3_lift=1.08,positive_blocks=4,
        integrity_ok=True,
    )
    assert status=='PASS_TRIAL19_INDEPENDENT'
    assert t19.trial19_verdict(event_count=300,event_days=260,matched_lift=1.12,ci_low=1.05,binary_event_t=2.9,top3_lift=1.08,positive_blocks=4,integrity_ok=True)=='FAIL_BINARY_EVENT_INFERENCE'

def test_same_day_dte_report_drops_event_groups_without_non_event_control():
    from app import v97_trial19 as t19
    df=pd.DataFrame({
        'date':pd.to_datetime(['2020-01-02','2020-01-02','2020-01-03']),
        'symbol':['EV','N','SOLO'],
        'movement_1d_atr':[2.0,1.0,20.0],
        'nse_near_dte':[3,3,15],
        'extreme_oi_event':[True,False,True],
    })
    out=t19.same_day_dte_matched_report(df[df.extreme_oi_event],df,'movement_1d_atr',reps=20)
    assert out['event_count']==1
    assert out['matched_groups']==1
    assert round(out['event_mean'],6)==2.0
