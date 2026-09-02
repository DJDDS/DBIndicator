import numpy as np
import pandas as pd


def _frame(boost=1.35, event_every=4, start='2021-05-03', periods=620):
    dates = pd.bdate_range(start, periods=periods)
    f = pd.DataFrame(index=dates)
    base = 1_000_000 + np.arange(periods) * 1000.0
    # Create recurring discrete total-OI jumps large enough for rolling z events.
    total = base.copy()
    for i in range(90, periods, event_every):
        total[i:] *= 1.04
    f['nse_total_oi'] = total
    f['nse_near_oi'] = total * 0.65
    f['nse_next_oi'] = total * 0.25
    f['nse_far_oi'] = total * 0.10
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['realized_vol20_prev'] = 0.25 + (np.arange(periods) % 20) / 1000
    f['atr_pct_prev'] = 0.02
    f['nse_near_dte'] = 20 - (np.arange(periods) % 20)
    f['days_to_expiry'] = f['nse_near_dte']
    f['eligible'] = True
    f['fno_member_pti'] = True
    # Mark movement on event dates after z is computed by evaluator; these recurring jumps
    # align to large positive daily OI changes.
    for i in range(90, periods, event_every):
        f.iloc[i, f.columns.get_loc('movement_1d_atr')] = boost
        f.iloc[i, f.columns.get_loc('movement_2d_atr')] = 1.20
    return f


def test_trial17_spec_is_frozen_and_uses_older_nonoverlapping_window():
    from app import v96_trial17 as t17
    spec = t17.trial17_spec()
    assert spec['trial_number'] == 17
    assert spec['total_oi_z_min'] == 1.5
    assert spec['independent_start'] == '2021-09-01'
    assert spec['independent_end'] == '2023-09-01'
    assert spec['primary_horizon'] == '1D'
    assert spec['secondary_2D_cannot_rescue_1D'] is True
    assert spec['directional_prediction'] is False
    assert spec['prior_locked_finals_untouched'] is True


def test_trial17_evaluator_never_uses_dates_after_independent_end():
    from app import v96_trial17 as t17
    f = _frame()
    # Make post-window rows absurdly strong; they must not change Trial 17.
    f.loc[f.index > pd.Timestamp('2023-09-01'), 'movement_1d_atr'] = 99.0
    out = t17.evaluate_trial17({'AAA': f}, controls={'historical_membership_available': True, 'lot_size_normalization_available': True}, bootstrap_reps=30)
    assert out['evidence_window']['end'] == '2023-09-01'
    assert out['trial18']['locked'] is True
    assert out['prior_locked_finals_untouched'] is True
    assert out['validation']['1D']['avg_move_atr'] < 5


def test_trial17_efficiency_failure_outranks_missing_mwpl():
    from app import v96_trial17 as t17
    f = _frame(boost=1.0)
    out = t17.evaluate_trial17({'AAA': f}, controls={'historical_membership_available': True, 'lot_size_normalization_available': True, 'mwpl_available': False}, bootstrap_reps=20)
    assert out['status'].startswith('FAIL_')
    assert out['status'] != 'INCONCLUSIVE_MISSING_MWPL_CONTROL'
    assert out['trial18']['locked'] is True


def test_trial17_reports_dte_and_symbol_concentration_diagnostics():
    from app import v96_trial17 as t17
    frames = {'AAA': _frame(), 'BBB': _frame(boost=1.25, event_every=5)}
    out = t17.evaluate_trial17(frames, controls={'historical_membership_available': True, 'lot_size_normalization_available': True}, bootstrap_reps=20)
    assert set(out['dte_buckets']) == {'0-5','6-10','11-20','21+'}
    assert 'top5_symbol_event_share' in out['concentration']
    assert out['research_only'] is True
    assert out['production_activation'] is False
