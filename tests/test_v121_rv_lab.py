import datetime as dt
import numpy as np
import pandas as pd


def _bars(days=8):
    rows=[]
    start=dt.date(2026,1,5)
    for d in range(days):
        day=start+dt.timedelta(days=d)
        if day.weekday()>=5: continue
        price=24000+d*10
        t=dt.datetime.combine(day,dt.time(9,15))
        while t.time() <= dt.time(15,10):
            # deterministic but non-zero returns, larger near open/close
            bump=0.00025 + (0.00015 if t.time()<dt.time(10,0) or t.time()>dt.time(14,30) else 0)
            price*=np.exp(bump*((len(rows)%3)-1))
            rows.append({'timestamp':t,'close':price})
            t += dt.timedelta(minutes=5)
    return pd.DataFrame(rows)


def _vix():
    dates=pd.bdate_range('2026-01-02',periods=10)
    return pd.DataFrame({'date':dates.date,'vix':[12+i*.2 for i in range(len(dates))]})


def test_dataset_uses_strict_prior_day_vix_and_fixed_windows():
    from app.v121_rv_lab import build_remaining_session_dataset
    ds=build_remaining_session_dataset(_bars(),_vix())
    assert not ds.empty
    first=ds.iloc[0]
    assert first['morning_variance']>0 and first['remaining_variance']>0
    vix=_vix().sort_values('date').reset_index(drop=True)
    row=vix[vix['date']==first['date']].index[0]
    assert first['prior_vix']==vix.iloc[row-1]['vix']
    assert first['feature_end']=='10:30' and first['target_end']=='15:10'


def test_dataset_excludes_incomplete_target_sessions():
    from app.v121_rv_lab import build_remaining_session_dataset
    bars=_bars()
    day=bars['timestamp'].dt.date.iloc[-1]
    bars=bars[~((bars['timestamp'].dt.date==day)&(bars['timestamp'].dt.time>dt.time(12,0)))]
    ds=build_remaining_session_dataset(bars,_vix())
    assert day not in set(ds['date'])


def test_loglinear_model_and_rolling_oos_are_positive_and_finite():
    from app.v121_rv_lab import fit_loglinear_model, rolling_oos_forecast, forecast_metrics
    rng=np.random.default_rng(7)
    n=40
    m=np.exp(rng.normal(-10,.4,n)); v=np.exp(rng.normal(-8,.2,n))
    y=np.exp(-1 + .5*np.log(m) + .3*np.log(v) + rng.normal(0,.05,n))
    ds=pd.DataFrame({'date':pd.bdate_range('2025-01-01',periods=n).date,'morning_variance':m,'prior_vix_variance':v,'remaining_variance':y})
    model=fit_loglinear_model(ds.iloc[:20])
    assert len(model['coef'])==3
    pred=rolling_oos_forecast(ds,min_train=15)
    assert len(pred)==n-15 and (pred['prediction']>0).all()
    met=forecast_metrics(pred)
    assert np.isfinite(met['mse']) and np.isfinite(met['qlike'])
    assert met['n']==n-15


def test_intraday_variance_curve_sums_to_one():
    from app.v121_rv_lab import intraday_variance_curve
    curve=intraday_variance_curve(_bars())
    assert abs(curve['variance_share'].sum()-1.0)<1e-9
    assert {'time','mean_sq_return','variance_share','observations'} <= set(curve.columns)
