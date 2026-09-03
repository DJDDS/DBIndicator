import math

import numpy as np
import pandas as pd

from app import v95_daily_evidence as v95
from app import v97_trial19 as t19


def _price_frame(periods=20):
    idx = pd.bdate_range('2020-01-01', periods=periods)
    close = pd.Series([100, 101, 99, 102, 103, 101, 104, 105, 103, 106, 108, 107, 110, 109, 112, 111, 114, 113, 116, 115][:periods], index=idx, dtype=float)
    return pd.DataFrame({
        'open': close * 0.997,
        'high': close * 1.012,
        'low': close * 0.988,
        'close': close,
    }, index=idx)


def test_v974_daily_frame_adds_pre_signal_realized_vol5_without_lookahead():
    price = _price_frame(20)
    oi = pd.Series(np.linspace(1_000_000, 1_400_000, len(price)), index=price.index)
    frame = v95.build_symbol_daily_frame(price, oi)
    assert 'realized_vol5_prev' in frame

    d = price.index[12]
    logret = np.log(price['close'] / price['close'].shift(1))
    expected = (logret.rolling(5, min_periods=4).std(ddof=1) * math.sqrt(252.0)).shift(1).loc[d]
    assert frame.loc[d, 'realized_vol5_prev'] == pytest.approx(expected)

    changed = price.copy()
    changed.loc[price.index[13]:, ['high', 'low', 'close']] *= 3.0
    frame2 = v95.build_symbol_daily_frame(changed, oi)
    assert frame2.loc[d, 'realized_vol5_prev'] == pytest.approx(frame.loc[d, 'realized_vol5_prev'])


def test_v974_daily_frame_adds_two_complete_pre_signal_session_moves():
    price = _price_frame(20)
    oi = pd.Series(np.linspace(1_000_000, 1_400_000, len(price)), index=price.index)
    frame = v95.build_symbol_daily_frame(price, oi)
    assert {'movement_prev1_atr', 'movement_prev2_atr'}.issubset(frame.columns)
    d = price.index[12]
    # Trial-19 event is formed at the close of d. The two completed sessions
    # before it are encoded by the already horizon-scaled next-session series
    # originating at d-2 and d-3 respectively.
    assert frame.loc[d, 'movement_prev1_atr'] == pytest.approx(frame['movement_1d_atr'].shift(2).loc[d])
    assert frame.loc[d, 'movement_prev2_atr'] == pytest.approx(frame['movement_1d_atr'].shift(3).loc[d])


def _confound_frame():
    rows = []
    for day in pd.to_datetime(['2020-01-02', '2020-01-03']):
        for i in range(20):
            rows.append({
                'date': day,
                'symbol': f'S{i:02d}',
                'nse_near_dte': 8,
                'dte_bucket': '6-10',
                'extreme_oi_event': i in (16, 18),
                'movement_1d_atr': 1.4 if i in (16, 18) else 1.0,
                'movement_prev1_atr': 1.02 if i in (16, 18) else 1.0,
                'movement_prev2_atr': 1.01 if i in (16, 18) else 1.0,
                'realized_vol5_prev': float(i + 1),
            })
    return pd.DataFrame(rows)


def test_v974_volatility_confound_matching_uses_same_day_dte_and_prior_vol_quintile():
    df = _confound_frame()
    events = df[df['extreme_oi_event']].copy()
    out = t19.same_day_dte_vol_matched_report(events, df, 'movement_1d_atr', reps=40)
    assert out['event_count'] == 4
    assert out['baseline_count'] > 0
    assert out['matched_group_columns'] == ['date', 'dte_bucket', 'rv5_bucket']
    assert out['lift'] > 1.0
    assert out['ci95_low'] is not None


def test_v974_volatility_confound_never_uses_event_rows_as_controls():
    df = _confound_frame()
    events = df[df['extreme_oi_event']].copy()
    controls = t19._matched_controls_with_vol(events, df)
    assert not controls['extreme_oi_event'].any()
    assert set(controls['date']) <= set(events['date'])
    assert set(controls['dte_bucket']) == {'6-10'}


def test_v974_frozen_same_day_dte_report_does_not_require_prior_volatility():
    df = _confound_frame().drop(columns=['realized_vol5_prev'])
    ev = df[df['extreme_oi_event']]
    out = t19.same_day_dte_matched_report(ev, df, 'movement_1d_atr', reps=20)
    assert out['event_count'] == 4
    assert out['lift'] is not None


def test_v974_pre_signal_persistence_is_diagnostic_not_trial19_rewrite():
    df = _confound_frame()
    events = df[df['extreme_oi_event']].copy()
    out = t19.pre_signal_persistence_report(events, df, reps=30)
    assert set(out) >= {'t_minus_1', 't_minus_2', 'warning'}
    assert out['warning'] is False


# pytest imported last to keep helper code import-light for app module loading.
import pytest


def _efficacy_pass_integrity_fail():
    return {
        'status': 'INCONCLUSIVE_INTEGRITY',
        'gates': {
            'sample_ok': True,
            'matched_lift_ok': True,
            'binary_event_t_ok': True,
            'tail_ok': True,
            'stability_ok': True,
            'integrity_ok': False,
        },
    }


def test_v974_earnings_confound_runs_when_efficacy_passes_even_if_integrity_is_inconclusive(monkeypatch):
    df = _confound_frame().copy()
    df['trial19_eligible'] = True
    monkeypatch.setattr(t19, '_stack', lambda frames: df.copy())
    emap = {'_meta': {'symbol_coverage': 1.0}}
    out = t19.evaluate_earnings_promotion({'ignored': pd.DataFrame()}, frozen_result=_efficacy_pass_integrity_fail(), earnings_map=emap, bootstrap_reps=30)
    assert out['status'] == 'PASS_EARNINGS_PROMOTION'
    assert out['earnings_symbol_coverage'] == 1.0
    assert out['trial18_eligible'] is False  # combined gate decides later
    assert out['confound_pass'] is True


def test_v974_earnings_confound_stays_locked_when_frozen_efficacy_failed(monkeypatch):
    failed = _efficacy_pass_integrity_fail()
    failed['gates']['binary_event_t_ok'] = False
    out = t19.evaluate_earnings_promotion({}, frozen_result=failed, earnings_map={'_meta': {'symbol_coverage': 1.0}}, bootstrap_reps=10)
    assert out['status'] == 'LOCKED_TRIAL19_EFFICACY_NOT_PASSED'
    assert out['confound_pass'] is False


def test_v974_trial18_eligibility_accepts_full_mwpl_or_preregistered_bound():
    frozen = _efficacy_pass_integrity_fail()
    vol = {'pass': True}
    earn = {'confound_pass': True}
    core = {'historical_membership_available': True, 'historical_cash_price_available': True, 'lot_size_normalization_available': True, 'mwpl_available': False}
    bounded = t19.evaluate_trial18_eligibility(frozen_result=frozen, volatility_control=vol, earnings_control=earn, integrity_controls=core, recent_mwpl_bound={'non_load_bearing': True})
    assert bounded['trial18_eligible'] is True
    assert bounded['status'] == 'ELIGIBLE_FOR_PREREGISTRATION'
    applied = dict(core); applied['mwpl_available'] = True
    direct = t19.evaluate_trial18_eligibility(frozen_result=frozen, volatility_control=vol, earnings_control=earn, integrity_controls=applied, recent_mwpl_bound=None)
    assert direct['trial18_eligible'] is True


def test_v974_trial18_eligibility_stays_locked_if_vol_or_earnings_fail():
    frozen = _efficacy_pass_integrity_fail()
    core = {'historical_membership_available': True, 'historical_cash_price_available': True, 'lot_size_normalization_available': True, 'mwpl_available': True}
    out = t19.evaluate_trial18_eligibility(frozen_result=frozen, volatility_control={'pass': False}, earnings_control={'confound_pass': True}, integrity_controls=core, recent_mwpl_bound=None)
    assert out['trial18_eligible'] is False
    assert 'VOLATILITY_CONFOUND' in out['reasons']


def test_v974_earnings_confound_accepts_nonempty_datetimeindex_without_boolean_coercion(monkeypatch):
    df = _confound_frame().copy()
    df['trial19_eligible'] = True
    monkeypatch.setattr(t19, '_stack', lambda frames: df.copy())
    emap = {
        'S16': pd.DatetimeIndex([pd.Timestamp('2020-01-03')]),
        '_meta': {'symbol_coverage': 1.0, 'loaded_symbols': ['S16']},
    }
    out = t19.evaluate_earnings_promotion(
        {'ignored': pd.DataFrame()},
        frozen_result=_efficacy_pass_integrity_fail(),
        earnings_map=emap,
        bootstrap_reps=20,
    )
    assert out['earnings_symbol_coverage'] == 1.0
    assert out['status'] in {'PASS_EARNINGS_PROMOTION', 'FAIL_EARNINGS_PROMOTION'}
