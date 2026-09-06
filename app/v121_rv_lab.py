"""V12.1 development-only remaining-session realised-variance research.

This is a new target. It is not labelled validated and cannot promote Trial 25.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

_EPS = 1e-12


def _clock(value: str) -> dt.time:
    h, m = [int(x) for x in str(value).split(':')[:2]]
    return dt.time(h, m)


def _prepared_bars(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if 'timestamp' not in df or 'close' not in df:
        raise ValueError('nifty_5m requires timestamp and close columns')
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df = df.dropna(subset=['timestamp','close']).sort_values('timestamp')
    df['date'] = df['timestamp'].dt.date
    df['time'] = df['timestamp'].dt.time
    df['log_close'] = np.log(df['close'].where(df['close'] > 0))
    df['log_return'] = df.groupby('date')['log_close'].diff()
    df['sq_return'] = df['log_return'] ** 2
    return df


def build_remaining_session_dataset(nifty_5m: pd.DataFrame, vix_daily: pd.DataFrame, *, feature_end='10:30', target_end='15:10') -> pd.DataFrame:
    """Build fixed 09:15→10:30 feature and 10:30→15:10 target rows.

    India VIX is lagged by one *available VIX trading observation* before
    joining, so the current day's VIX can never leak into the feature set.
    """
    bars = _prepared_bars(nifty_5m)
    f_end, t_end = _clock(feature_end), _clock(target_end)
    vix = vix_daily.copy()
    if 'date' not in vix or 'vix' not in vix:
        raise ValueError('vix_daily requires date and vix columns')
    vix['date'] = pd.to_datetime(vix['date']).dt.date
    vix['vix'] = pd.to_numeric(vix['vix'], errors='coerce')
    vix = vix.dropna(subset=['date','vix']).sort_values('date').drop_duplicates('date', keep='last')
    vix['prior_vix'] = vix['vix'].shift(1)
    prior_map = dict(zip(vix['date'], vix['prior_vix']))

    rows = []
    for day, grp in bars.groupby('date', sort=True):
        morning = grp[(grp['time'] > dt.time(9,15)) & (grp['time'] <= f_end)]['sq_return'].dropna()
        target = grp[(grp['time'] > f_end) & (grp['time'] <= t_end)]['sq_return'].dropna()
        # A complete 5-minute session has 15 feature returns and 56 target
        # returns for these fixed windows. Fail closed on materially incomplete
        # sessions rather than changing the window to fit available data.
        if len(morning) < 12 or len(target) < 50:
            continue
        pv = prior_map.get(day)
        if pv is None or not np.isfinite(float(pv)) or float(pv) <= 0:
            continue
        pv = float(pv)
        rows.append({
            'date': day,
            'morning_variance': float(morning.sum()),
            'remaining_variance': float(target.sum()),
            'prior_vix': pv,
            'prior_vix_variance': float((pv / 100.0) ** 2 / 252.0),
            'feature_end': feature_end,
            'target_end': target_end,
            'morning_bars': int(len(morning)),
            'target_bars': int(len(target)),
        })
    return pd.DataFrame(rows)


def fit_loglinear_model(train_df: pd.DataFrame) -> dict:
    req = ['morning_variance','prior_vix_variance','remaining_variance']
    df = train_df.dropna(subset=req).copy()
    df = df[(df[req] > 0).all(axis=1)]
    if len(df) < 3:
        raise ValueError('at least 3 positive observations required')
    X = np.column_stack([
        np.ones(len(df)),
        np.log(df['morning_variance'].to_numpy(float) + _EPS),
        np.log(df['prior_vix_variance'].to_numpy(float) + _EPS),
    ])
    y = np.log(df['remaining_variance'].to_numpy(float) + _EPS)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    return {'coef': coef.tolist(), 'n': int(len(df)), 'resid_std': float(np.std(resid, ddof=min(1, len(resid)-1)))}


def _predict(model: dict, row) -> float:
    c = np.asarray(model['coef'], dtype=float)
    x = np.array([1.0, math.log(float(row['morning_variance']) + _EPS), math.log(float(row['prior_vix_variance']) + _EPS)])
    return float(max(_EPS, math.exp(float(x @ c))))


def rolling_oos_forecast(dataset: pd.DataFrame, *, min_train=120) -> pd.DataFrame:
    ds = dataset.sort_values('date').reset_index(drop=True).copy()
    out=[]
    min_train=max(3,int(min_train))
    for i in range(min_train, len(ds)):
        train=ds.iloc[:i]
        row=ds.iloc[i]
        model=fit_loglinear_model(train)
        pred=_predict(model,row)
        baseline=float(train['remaining_variance'].mean())
        out.append({'date':row['date'],'actual':float(row['remaining_variance']),'prediction':pred,'baseline_mean':max(_EPS,baseline),'train_n':i})
    return pd.DataFrame(out)


def forecast_metrics(predictions: pd.DataFrame) -> dict:
    if predictions is None or predictions.empty:
        return {'n':0,'mse':None,'qlike':None,'baseline_mse':None,'baseline_qlike':None}
    y=np.maximum(predictions['actual'].to_numpy(float),_EPS)
    p=np.maximum(predictions['prediction'].to_numpy(float),_EPS)
    b=np.maximum(predictions['baseline_mean'].to_numpy(float),_EPS)
    q=lambda f: np.mean(np.log(f)+y/f)
    return {
        'n':int(len(y)),
        'mse':float(np.mean((y-p)**2)),
        'qlike':float(q(p)),
        'baseline_mse':float(np.mean((y-b)**2)),
        'baseline_qlike':float(q(b)),
    }


def intraday_variance_curve(nifty_5m: pd.DataFrame) -> pd.DataFrame:
    bars=_prepared_bars(nifty_5m).dropna(subset=['sq_return']).copy()
    if bars.empty:
        return pd.DataFrame(columns=['time','mean_sq_return','variance_share','observations'])
    g=bars.groupby('time')['sq_return'].agg(['mean','count']).reset_index().rename(columns={'mean':'mean_sq_return','count':'observations'})
    total=float(g['mean_sq_return'].sum())
    g['variance_share']=g['mean_sq_return']/total if total>0 else 0.0
    return g[['time','mean_sq_return','variance_share','observations']]
