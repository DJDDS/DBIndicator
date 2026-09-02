import pandas as pd
from app import v95_daily_evidence as v95


def _weak_frame():
    dates = pd.bdate_range('2023-01-02', periods=320)
    f = pd.DataFrame(index=dates)
    f['unexpected_oi_z'] = 0.0
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['realized_vol20_prev'] = 0.2
    f['atr_pct_prev'] = 0.02
    f['days_to_expiry'] = 10.0
    f['eligible'] = True
    # validation 60:80% = rows 192:256. Give enough events but no lift.
    f.iloc[192:256, f.columns.get_loc('unexpected_oi_z')] = 2.0
    return f


def test_v953_primary_failure_precedes_missing_mwpl_control():
    report = v95.evaluate_trial15({s: _weak_frame() for s in ['AAA','BBB','CCC','DDD','EEE']}, controls={
        'mwpl_available': False,
        'historical_membership_available': True,
        'lot_size_normalization_available': True,
        'atm_iv_available': False,
    }, bootstrap_reps=20)
    assert report['status'] == 'FAIL_NO_INDEPENDENT_LIFT'
    assert report['trial15_closed'] is True
    assert report['final_test']['locked'] is True
    assert report['trial16']['locked'] is True
    assert 'MISSING_MWPL_CONTROL' in report['inconclusive_reasons']


def test_v953_missing_control_only_blocks_a_feature_that_passes_efficacy(monkeypatch):
    metrics = {
        'sample_ok': True, 'lift_ok': True, 'vol_ok': True,
        'tail_ok': True, 'stability_ok': True,
    }
    status, closed = v95.trial15_terminal_status(metrics, ['MISSING_MWPL_CONTROL'])
    assert status == 'INCONCLUSIVE_MISSING_MWPL_CONTROL'
    assert closed is False
