import numpy as np
import pandas as pd
import pytest

from app import v96_trial17 as v96
from app import v99_volume_gate as v99


def _fixture(n_dates=24, n_symbols=12):
    rng = np.random.default_rng(99)
    dates = pd.bdate_range('2016-01-04', periods=n_dates)
    rows = []
    for d in dates:
        for j in range(n_symbols):
            rows.append({
                'date': d,
                'symbol': f'S{j:03d}',
                'dte_bucket': ['0-5', '6-10', '11-20', '21+'][j % 4],
                'har_daily_var': abs(rng.normal(0.00020, 0.00003)),
                'har_weekly_var': abs(rng.normal(0.00018, 0.00003)),
                'har_monthly_var': abs(rng.normal(0.00016, 0.00003)),
                'abnormal_futstk_volume': rng.normal(),
            })
    frame = pd.DataFrame(rows)
    frame['next_yz_var'] = (
        0.25 * frame['har_daily_var']
        + 0.35 * frame['har_weekly_var']
        + 0.20 * frame['har_monthly_var']
        + 0.00001 * frame['abnormal_futstk_volume']
        + rng.normal(0, 0.00001, len(frame))
    ).clip(lower=1e-8)
    return frame


def _slow_reference(frame):
    cols = ['har_daily_var', 'har_weekly_var', 'har_monthly_var', 'abnormal_futstk_volume']
    use = frame[['date', 'dte_bucket', 'symbol', 'next_yz_var'] + cols].copy()
    use['next_yz_var'] = pd.to_numeric(use['next_yz_var']) * v99.VAR_SCALE
    for c in cols[:3]:
        use[c] = pd.to_numeric(use[c]) * v99.VAR_SCALE
    g = use.groupby(['date', 'dte_bucket'], observed=True)
    y = use['next_yz_var'] - g['next_yz_var'].transform('mean')
    X = pd.DataFrame(index=use.index)
    for c in cols:
        X[c] = use[c] - g[c].transform('mean')
    return v96.two_way_cluster_robust_ols(y, X, use['date'], use['symbol'])


def test_fast_two_way_cluster_matches_frozen_reference():
    frame = _fixture()
    expected = _slow_reference(frame)
    actual = v99._same_day_dte_regression(frame, 'next_yz_var')

    assert actual['n'] == expected['n']
    assert actual['date_clusters'] == expected['date_clusters']
    assert actual['symbol_clusters'] == expected['symbol_clusters']
    for section in ('coef', 'se', 't'):
        assert actual[section].keys() == expected[section].keys()
        for key in expected[section]:
            assert actual[section][key] == pytest.approx(expected[section][key], rel=1e-10, abs=1e-10)


def test_v99_same_day_dte_does_not_call_slow_frozen_cluster(monkeypatch):
    frame = _fixture()

    def _boom(*args, **kwargs):
        raise AssertionError('slow frozen V9.6 cluster routine must not be used by V9.9')

    monkeypatch.setattr(v96, 'two_way_cluster_robust_ols', _boom)
    result = v99._same_day_dte_regression(frame, 'next_yz_var')

    assert result['n'] == len(frame)
    assert 'abnormal_futstk_volume' in result['t']
