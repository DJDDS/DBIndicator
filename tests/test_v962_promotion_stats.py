import numpy as np
import pandas as pd


def test_same_day_matched_report_uses_only_event_days_and_non_events():
    from app import v96_trial17 as t17
    baseline = pd.DataFrame({
        'date': pd.to_datetime(['2022-01-03']*4 + ['2022-01-04']*4),
        'symbol': ['A','B','C','D']*2,
        'movement_1d_atr': [2.0,1.0,1.0,1.0, 0.5,0.5,0.5,0.5],
        'is_event': [True,False,False,False, False,False,False,False],
    })
    events = baseline[baseline['is_event']].copy()
    out = t17.same_day_matched_report(events, baseline, 'movement_1d_atr', reps=40)
    assert out['event_days'] == 1
    assert out['baseline_count'] == 3
    assert out['event_count'] == 1
    assert round(out['lift'], 6) == 2.0


def test_dte_matched_report_weights_baseline_to_event_bucket_mix():
    from app import v96_trial17 as t17
    baseline = pd.DataFrame({
        'date': pd.bdate_range('2022-01-03', periods=12),
        'symbol': [f'S{i}' for i in range(12)],
        'movement_1d_atr': [1.0]*6 + [2.0]*6,
        'nse_near_dte': [3]*6 + [15]*6,
    })
    events = pd.concat([baseline.iloc[[0]], baseline.iloc[[6]], baseline.iloc[[7]]], ignore_index=True)
    # Event mix is 1/3 low-DTE and 2/3 mid-DTE; matched baseline should use same weights.
    out = t17.dte_matched_report(events, baseline, 'movement_1d_atr')
    assert out['event_count'] == 3
    assert out['dte_buckets_used'] == 2
    assert abs(out['matched_baseline_mean'] - (1.0/3 + 4.0/3)) < 1e-9


def test_two_way_cluster_ols_reports_date_and_symbol_clusters():
    from app import v96_trial17 as t17
    rng = np.random.default_rng(7)
    days = pd.bdate_range('2022-01-03', periods=40)
    syms = [f'S{i}' for i in range(10)]
    rows=[]
    for di,d in enumerate(days):
        for si,s in enumerate(syms):
            z = rng.normal()
            y = 1.0 + 0.45*z + 0.03*(di%5) + 0.02*(si%3) + rng.normal(scale=0.08)
            rows.append((d,s,z,y))
    df=pd.DataFrame(rows, columns=['date','symbol','total_z','y'])
    out=t17.two_way_cluster_robust_ols(df['y'], df[['total_z']], df['date'], df['symbol'])
    assert out['date_clusters'] == 40
    assert out['symbol_clusters'] == 10
    assert out['coef']['total_z'] > 0.3
    assert out['t']['total_z'] > 3.0


def test_dte_matched_report_excludes_event_rows_from_matched_baseline():
    from app import v96_trial17 as t17
    baseline=pd.DataFrame({
        'date':pd.to_datetime(['2022-01-03','2022-01-03','2022-01-04','2022-01-04']),
        'symbol':['EV','N1','EV2','N2'],
        'movement_1d_atr':[10.0,1.0,20.0,2.0],
        'nse_near_dte':[3,3,15,15],
    })
    events=baseline.iloc[[0,2]].copy()
    out=t17.dte_matched_report(events, baseline, 'movement_1d_atr')
    # Equal event bucket weights; non-event matched baseline is (1 + 2)/2 = 1.5.
    assert abs(out['matched_baseline_mean']-1.5)<1e-9
