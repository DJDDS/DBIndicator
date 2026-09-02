import numpy as np
import pandas as pd
import pytest

from app import v95_daily_evidence as v95


def _daily_price(start='2024-01-01', periods=180, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=periods)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, periods)))
    open_ = np.r_[close[0], close[:-1]]
    span = np.maximum(0.5, np.abs(rng.normal(1.2, 0.25, periods)))
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    return pd.DataFrame({'open':open_, 'high':high, 'low':low, 'close':close, 'volume':1000}, index=idx)


def _oi(idx, seed=9):
    rng = np.random.default_rng(seed)
    chg = rng.normal(0.002, 0.02, len(idx))
    return pd.Series(1_000_000 * np.exp(np.cumsum(chg)), index=idx)


def test_v950_specs_are_research_only_and_trial16_locked():
    t15 = v95.trial15_spec()
    assert v95.BUILD_ID == '2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE'
    assert t15['trial_number'] == 15
    assert t15['primary_horizon'] == '1D'
    assert t15['secondary_2D_cannot_rescue_1D'] is True
    assert t15['unexpected_oi_z_min'] == pytest.approx(1.5)
    assert t15['research_only'] is True
    t16 = v95.trial16_spec()
    assert t16['trial_number'] == 16
    assert t16['locked'] is True
    assert t16['auto_run'] is False


def test_v950_point_in_time_features_do_not_use_future_oi_or_volatility():
    price = _daily_price(periods=90)
    oi = _oi(price.index)
    a = v95.build_symbol_daily_frame(price, oi)
    oi2 = oi.copy()
    oi2.iloc[-1] *= 20
    price2 = price.copy()
    price2.loc[price2.index[-1], ['high','low','close']] *= [2.0, 0.5, 1.5]
    b = v95.build_symbol_daily_frame(price2, oi2)
    # A future/latest-row mutation cannot alter prior signal-date features.
    common = a.index[:-2]
    pd.testing.assert_series_equal(a.loc[common, 'raw_oi_z'], b.loc[common, 'raw_oi_z'])
    pd.testing.assert_series_equal(a.loc[common, 'realized_vol20_prev'], b.loc[common, 'realized_vol20_prev'])
    pd.testing.assert_series_equal(a.loc[common, 'atr14_prev'], b.loc[common, 'atr14_prev'])


def test_v950_expiry_regime_switch_is_explicit():
    idx = pd.DatetimeIndex(['2025-08-28', '2025-09-01', '2025-09-02', '2025-09-29'])
    dte, derived, regime = v95.derived_days_to_expiry(idx)
    assert regime.loc[pd.Timestamp('2025-08-28')] == 'THURSDAY'
    assert regime.loc[pd.Timestamp('2025-09-01')] == 'TUESDAY'
    assert regime.loc[pd.Timestamp('2025-09-02')] == 'TUESDAY'
    assert (dte >= 0).all()
    assert derived.all()


def test_v950_expected_oi_model_is_frozen_from_development():
    price = _daily_price(periods=160)
    frame = v95.build_symbol_daily_frame(price, _oi(price.index))
    dev = frame.iloc[:100]
    model = v95.fit_expected_oi_model(dev)
    applied1 = v95.apply_expected_oi_model(frame, model)
    mutated = frame.copy()
    mutated.loc[mutated.index[120]:, 'oi_chg_pct'] += 100.0
    applied2 = v95.apply_expected_oi_model(mutated, model)
    assert model['fit_end'] <= str(dev.index.max().date())
    assert applied1.loc[frame.index[90], 'expected_oi_chg_pct'] == pytest.approx(
        applied2.loc[frame.index[90], 'expected_oi_chg_pct']
    )
    assert model['resid_std'] > 0


def test_v950_final_20_is_masked_and_2d_cannot_rescue_failed_1d():
    # Construct 100 trading dates; anomaly rows in validation have no 1D lift
    # but very large 2D moves. The report must still fail the primary horizon,
    # and the final rows must never appear in an outcome statistic.
    dates = pd.bdate_range('2024-01-01', periods=100)
    rows = []
    for sym in ['AAA','BBB','CCC','DDD']:
        f = pd.DataFrame(index=dates)
        f['unexpected_oi_z'] = 0.0
        f['movement_1d_atr'] = 1.0
        f['movement_2d_atr'] = 1.0
        f['realized_vol20_prev'] = 0.2
        f['atr_pct_prev'] = 0.02
        f['ban_flag'] = False
        f['mwpl_pct'] = 50.0
        f['eligible'] = True
        # validation is 60:80; create anomalies with 1D ~= baseline but 2D huge
        if sym in ('AAA','BBB'):
            f.iloc[60:80, f.columns.get_loc('unexpected_oi_z')] = 2.0
            f.iloc[60:80, f.columns.get_loc('movement_2d_atr')] = 5.0
        # final sentinels must never leak
        f.iloc[80:, f.columns.get_loc('unexpected_oi_z')] = 3.0
        f.iloc[80:, f.columns.get_loc('movement_1d_atr')] = 99.0
        rows.append((sym, f))
    report = v95.evaluate_trial15(dict(rows), controls={
        'mwpl_available': True,
        'historical_membership_available': True,
        'lot_size_normalization_available': True,
        'atm_iv_available': False,
    }, bootstrap_reps=100)
    assert report['final_test']['locked'] is True
    assert 'outcomes' not in report['final_test']
    assert report['validation']['1D']['lift'] == pytest.approx(1.0)
    assert report['validation']['2D']['lift'] > 1.0
    assert report['status'] != 'PASS_VALIDATION'
    assert report['primary_pass'] is False


def test_v950_missing_integrity_controls_fail_closed():
    dates = pd.bdate_range('2024-01-01', periods=120)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = 2.0
    f['movement_1d_atr'] = 2.0
    f['movement_2d_atr'] = 2.0
    f['realized_vol20_prev'] = 0.2
    f['atr_pct_prev'] = 0.02
    f['eligible'] = True
    report = v95.evaluate_trial15({'AAA':f}, controls={
        'mwpl_available': False,
        'historical_membership_available': False,
        'lot_size_normalization_available': False,
        'atm_iv_available': False,
    }, bootstrap_reps=50)
    assert report['status'] == 'INCONCLUSIVE_SAMPLE'
    assert 'MISSING_MWPL_CONTROL' in report['inconclusive_reasons']
    assert 'SURVIVORSHIP_BIAS' in report['inconclusive_reasons']
    assert 'OI_NORMALIZATION_UNAVAILABLE' in report['inconclusive_reasons']
    assert report['controls']['atm_iv_control'] == 'UNAVAILABLE_NOT_FABRICATED'


def test_v950_cluster_robust_ols_detects_independent_signal():
    rng = np.random.default_rng(11)
    days = np.repeat(pd.bdate_range('2024-01-01', periods=80).to_numpy(), 5)
    z = rng.normal(size=len(days))
    vol = rng.normal(0.2, 0.03, len(days))
    y = 1.0 + 0.30*z + 1.2*vol + rng.normal(0, 0.18, len(days))
    out = v95.cluster_robust_ols(
        y,
        pd.DataFrame({'unexpected_oi_z':z, 'realized_vol20_prev':vol}),
        pd.Series(days),
    )
    assert out['n'] == len(days)
    assert out['clusters'] == 80
    assert out['coef']['unexpected_oi_z'] > 0.2
    assert out['t']['unexpected_oi_z'] > 3.0


def test_v950_day_cluster_bootstrap_is_deterministic():
    days = pd.bdate_range('2024-01-01', periods=30)
    ev = pd.DataFrame({'date':days, 'movement_1d_atr':np.repeat(1.3, len(days))})
    base = pd.DataFrame({'date':days, 'movement_1d_atr':np.repeat(1.0, len(days))})
    a = v95.day_cluster_bootstrap_lift(ev, base, 'movement_1d_atr', reps=200, seed=950)
    b = v95.day_cluster_bootstrap_lift(ev, base, 'movement_1d_atr', reps=200, seed=950)
    assert a == b
    assert a['lift'] == pytest.approx(1.3)
    assert a['ci95_low'] == pytest.approx(1.3)


def test_v950_tail_and_block_stability_helpers():
    dates = pd.bdate_range('2024-01-01', periods=40)
    base = pd.DataFrame({'date':np.repeat(dates, 2), 'movement_1d_atr':1.0})
    ev = base.copy()
    ev['movement_1d_atr'] = 1.2
    robust = v95.top_days_removed_lift(ev, base, 'movement_1d_atr', top_n=3)
    blocks = v95.chronological_block_lifts(ev, base, 'movement_1d_atr', blocks=4)
    assert robust['lift'] > 1.0
    assert len(blocks) == 4
    assert all(x['lift'] > 1.0 for x in blocks)


def test_v950_primary_result_excludes_ban_or_95pct_mwpl_days_but_reports_them():
    dates = pd.bdate_range('2024-01-01', periods=120)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = 0.0
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['realized_vol20_prev'] = 0.2
    f['atr_pct_prev'] = 0.02
    f['days_to_expiry'] = 10.0
    f['ban_flag'] = False
    f['mwpl_pct'] = 50.0
    f['eligible'] = True
    # Validation 72:96. A few banned anomaly days have huge moves; clean days do not.
    f.iloc[72:76, f.columns.get_loc('unexpected_oi_z')] = 3.0
    f.iloc[72:76, f.columns.get_loc('movement_1d_atr')] = 10.0
    f.iloc[72:76, f.columns.get_loc('ban_flag')] = True
    f.iloc[72:76, f.columns.get_loc('mwpl_pct')] = 99.0
    f.iloc[76:84, f.columns.get_loc('unexpected_oi_z')] = 2.0
    report = v95.evaluate_trial15({'AAA':f}, controls={
        'mwpl_available': True, 'historical_membership_available': True,
        'lot_size_normalization_available': True, 'atm_iv_available': False,
    }, bootstrap_reps=50)
    # Primary clean population sees only the non-ban anomalies, which have no lift.
    assert report['validation']['1D']['lift'] == pytest.approx(1.0)
    assert report['ban_mwpl_analysis']['ban_or_95']['events'] == 4
    # The ban population is compared with its own ban-regime baseline, so the
    # mechanically high-move regime is not mislabelled as incremental OI edge.
    assert report['ban_mwpl_analysis']['ban_or_95']['lift_1D'] == pytest.approx(1.0)


def test_v950_regression_uses_atm_iv_when_honest_series_is_present():
    dates = pd.bdate_range('2024-01-01', periods=120)
    f = pd.DataFrame(index=dates)
    rng = np.random.default_rng(123)
    f['unexpected_oi_z'] = rng.normal(size=len(f))
    f['realized_vol20_prev'] = rng.normal(.2,.02,len(f))
    f['atr_pct_prev'] = .02
    f['days_to_expiry'] = 10.0
    f['atm_iv_pct_pti'] = rng.normal(30,2,len(f))
    f['movement_1d_atr'] = 1 + .2*f['unexpected_oi_z'] + .01*f['atm_iv_pct_pti'] + rng.normal(0,.1,len(f))
    f['movement_2d_atr'] = f['movement_1d_atr']
    f['ban_flag'] = False
    f['mwpl_pct'] = 50.0
    f['eligible'] = True
    report = v95.evaluate_trial15({'AAA':f}, controls={
        'mwpl_available': True, 'historical_membership_available': True,
        'lot_size_normalization_available': True, 'atm_iv_available': True,
    }, bootstrap_reps=20)
    assert report['controls']['atm_iv_control'] == 'APPLIED'
    assert 'atm_iv_pct_pti' in report['regression_1D']['coef']


def test_v950_cluster_bootstrap_resamples_all_validation_days_not_only_event_days():
    days = pd.bdate_range('2024-01-01', periods=10)
    base = pd.DataFrame({
        'date': np.repeat(days, 2),
        'movement_1d_atr': np.repeat([1.0] * 5 + [3.0] * 5, 2),
    })
    ev = pd.DataFrame({
        'date': days[:5],
        'movement_1d_atr': 1.5,
    })
    out = v95.day_cluster_bootstrap_lift(ev, base, 'movement_1d_atr', reps=1000, seed=950)
    assert out['lift'] == pytest.approx(0.75)
    assert out['clusters'] == 10
    # If non-event high-volatility days were incorrectly discarded, the CI would sit at 1.5x.
    assert out['ci95_high'] < 1.2


def test_v950_mwpl_population_lift_uses_its_own_population_baseline():
    dates = pd.bdate_range('2024-01-01', periods=120)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = 0.0
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['realized_vol20_prev'] = 0.2
    f['atr_pct_prev'] = 0.02
    f['days_to_expiry'] = 10.0
    f['ban_flag'] = False
    f['mwpl_pct'] = 50.0
    f['eligible'] = True
    # Validation is 72:96. Make high-MWPL population intrinsically high-move,
    # but its anomaly rows have no additional lift within that population.
    f.iloc[72:84, f.columns.get_loc('mwpl_pct')] = 90.0
    f.iloc[72:84, f.columns.get_loc('movement_1d_atr')] = 3.0
    f.iloc[72:78, f.columns.get_loc('unexpected_oi_z')] = 2.0
    report = v95.evaluate_trial15({'AAA': f}, controls={
        'mwpl_available': True, 'historical_membership_available': True,
        'lot_size_normalization_available': True, 'atm_iv_available': False,
    }, bootstrap_reps=20)
    assert report['ban_mwpl_analysis']['high_mwpl_preban']['lift_1D'] == pytest.approx(1.0)


def test_v950_reports_raw_vs_unexpected_and_negative_shock_diagnostics_without_changing_primary():
    dates = pd.bdate_range('2024-01-01', periods=120)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = 0.0
    f['raw_oi_z'] = 0.0
    f['oi_level_z_prev'] = 0.0
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['realized_vol20_prev'] = 0.2
    f['atr_pct_prev'] = 0.02
    f['days_to_expiry'] = 10.0
    f['ban_flag'] = False
    f['mwpl_pct'] = 50.0
    f['eligible'] = True
    # Validation 72:96: positive unexpected shocks expand; raw-only shocks do not.
    f.iloc[72:78, f.columns.get_loc('unexpected_oi_z')] = 2.0
    f.iloc[72:78, f.columns.get_loc('movement_1d_atr')] = 2.0
    f.iloc[78:84, f.columns.get_loc('raw_oi_z')] = 2.0
    # Negative residual shock diagnostic.
    f.iloc[84:90, f.columns.get_loc('unexpected_oi_z')] = -2.0
    f.iloc[84:90, f.columns.get_loc('movement_1d_atr')] = 0.5
    report = v95.evaluate_trial15({'AAA': f}, controls={
        'mwpl_available': True, 'historical_membership_available': True,
        'lot_size_normalization_available': True, 'atm_iv_available': False,
    }, bootstrap_reps=20)
    diag = report['diagnostics']
    assert diag['raw_positive_oi_z']['1D']['event_count'] == 6
    assert diag['unexpected_negative_oi_z']['1D']['event_count'] == 6
    assert diag['unexpected_negative_oi_z']['1D']['lift'] < 1.0
    assert report['validation']['1D']['event_count'] == 6


def test_v950_regression_decomposes_oi_level_and_unexpected_shock_when_available():
    rng = np.random.default_rng(950)
    dates = pd.bdate_range('2024-01-01', periods=140)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = rng.normal(size=len(f))
    f['raw_oi_z'] = f['unexpected_oi_z']
    f['oi_level_z_prev'] = rng.normal(size=len(f))
    f['realized_vol20_prev'] = rng.normal(.2, .02, len(f))
    f['atr_pct_prev'] = .02
    f['days_to_expiry'] = 10.0
    f['movement_1d_atr'] = 1 + .3*f['unexpected_oi_z'] - .15*f['oi_level_z_prev'] + rng.normal(0,.1,len(f))
    f['movement_2d_atr'] = f['movement_1d_atr']
    f['ban_flag'] = False
    f['mwpl_pct'] = 50.0
    f['eligible'] = True
    report = v95.evaluate_trial15({'AAA': f}, controls={
        'mwpl_available': True, 'historical_membership_available': True,
        'lot_size_normalization_available': True, 'atm_iv_available': False,
    }, bootstrap_reps=20)
    assert 'oi_level_z_prev' in report['regression_1D']['coef']
