import numpy as np
import pandas as pd
import pytest

from app import v99_volume_gate as v99


def _turnover_frame(n=360):
    idx = pd.bdate_range('2014-01-02', periods=n)
    rng = np.random.default_rng(990)
    trend = np.linspace(0.0, 0.5, n)
    dow = idx.dayofweek.to_numpy()
    log_turn = 14.0 + trend + 0.08 * (dow == 1) - 0.05 * (dow == 4) + rng.normal(0, 0.06, n)
    return pd.DataFrame({'futures_turnover_notional': np.exp(log_turn)}, index=idx)


def test_v990_abnormal_turnover_is_point_in_time_and_future_changes_do_not_leak():
    frame = _turnover_frame()
    out = v99.build_abnormal_turnover(frame)
    d = frame.index[300]
    assert np.isfinite(out.loc[d, 'abnormal_futstk_volume'])

    changed = frame.copy()
    changed.loc[frame.index[301]:, 'futures_turnover_notional'] *= 1000.0
    out2 = v99.build_abnormal_turnover(changed)
    assert out2.loc[d, 'abnormal_futstk_volume'] == pytest.approx(out.loc[d, 'abnormal_futstk_volume'])


def test_v990_current_turnover_spike_is_visible_without_entering_its_own_scale():
    frame = _turnover_frame()
    spike_day = frame.index[300]
    base = v99.build_abnormal_turnover(frame)
    spiked = frame.copy()
    spiked.loc[spike_day, 'futures_turnover_notional'] *= 20.0
    out = v99.build_abnormal_turnover(spiked)
    assert out.loc[spike_day, 'abnormal_futstk_volume'] > base.loc[spike_day, 'abnormal_futstk_volume'] + 3.0


def _forecast_frame(*, coef=0.8, seed=1, symbol='AAA', n=700):
    idx = pd.bdate_range('2014-01-02', periods=n)
    rng = np.random.default_rng(seed)
    h1 = 0.5 + rng.uniform(0.0, 0.4, n)
    h5 = pd.Series(h1).rolling(5, min_periods=1).mean().to_numpy()
    h22 = pd.Series(h1).rolling(22, min_periods=1).mean().to_numpy()
    av = rng.normal(0, 1, n)
    noise = rng.normal(0, 0.03, n)
    y = 0.15 + 0.30*h1 + 0.25*h5 + 0.20*h22 + coef*0.08*av + noise
    y = np.clip(y, 0.02, None)
    return pd.DataFrame({
        'date': idx,
        'symbol': symbol,
        'har_daily_var': h1,
        'har_weekly_var': h5,
        'har_monthly_var': h22,
        'abnormal_futstk_volume': av,
        'next_yz_var': y,
        'next_gk_var': y * (1.0 + rng.normal(0, 0.015, n)),
        'days_to_expiry': (np.arange(n) % 28) + 1,
        'fno_member_pti': True,
    }).set_index(idx)


def test_v990_oos_gate_passes_when_volume_has_stable_incremental_forecast_value(monkeypatch):
    frames = {f'S{i}': _forecast_frame(coef=1.0, seed=100+i, symbol=f'S{i}') for i in range(12)}
    monkeypatch.setattr(v99, 'INDEPENDENT_START', pd.Timestamp('2015-06-01'))
    monkeypatch.setattr(v99, 'INDEPENDENT_END', pd.Timestamp('2016-08-31'))
    out = v99.evaluate_trial20(frames, earnings_map=None, min_train_obs=160, refit_every=20, require_earnings=False)
    assert out['status'] == 'PASS_TRIAL20_VOLUME_OOS_GATE'
    assert out['pass'] is True
    assert out['primary_oos']['mse']['augmented'] < out['primary_oos']['mse']['har']
    assert out['primary_oos']['qlike']['augmented'] < out['primary_oos']['qlike']['har']
    assert out['primary_oos']['clark_west']['t'] > v99.CLARK_WEST_HURDLE
    assert out['primary_oos']['oos_r2'] > 0
    assert out['trial18_state'] == 'LOCKED'
    assert out['production_activation'] is False


def test_v990_oos_gate_fails_without_incremental_volume_information(monkeypatch):
    frames = {f'S{i}': _forecast_frame(coef=0.0, seed=300+i, symbol=f'S{i}') for i in range(12)}
    monkeypatch.setattr(v99, 'INDEPENDENT_START', pd.Timestamp('2015-06-01'))
    monkeypatch.setattr(v99, 'INDEPENDENT_END', pd.Timestamp('2016-08-31'))
    out = v99.evaluate_trial20(frames, earnings_map=None, min_train_obs=160, refit_every=20, require_earnings=False)
    assert out['pass'] is False
    assert out['status'].startswith('FAIL_')
    assert out['trial18_state'] == 'LOCKED'


def test_v990_spec_has_no_optimized_volume_threshold_and_keeps_oi_diagnostic_only():
    spec = v99.trial20_spec()
    assert spec['feature'] == 'abnormal total FUTSTK notional turnover'
    assert spec['volume_threshold'] is None
    assert spec['oi_role'] == 'DIAGNOSTIC_ONLY'
    assert spec['trial18_locked'] is True
