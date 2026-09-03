import numpy as np
import pandas as pd

from app import backtest


def _fixtures():
    idx = pd.bdate_range('2014-06-02', '2018-09-07')
    turn = pd.Series(5_000_000.0 * np.exp(np.linspace(0, 0.2, len(idx))), index=idx)
    oi = pd.Series(1_000_000.0, index=idx)
    exp = pd.Series(idx + pd.Timedelta(days=20), index=idx)
    histories = {
        'AAA': {
            'membership': pd.Series(True, index=idx),
            'near_oi': oi,
            'total_oi': oi,
            'near_expiry': exp,
            'total_turnover_notional': turn,
        },
        '_meta': {'date_coverage': 1.0, 'source': 'TEST_NSE_FO'},
    }
    close = pd.Series(100.0 * np.exp(np.linspace(0, 0.3, len(idx))), index=idx)
    price = pd.DataFrame({'open': close * 0.999, 'high': close * 1.01, 'low': close * 0.99, 'close': close}, index=idx)
    cash = {'AAA': price, '_meta': {'date_coverage': 1.0, 'source': 'TEST_NSE_CM'}}
    earnings = {'AAA': [pd.Timestamp('2016-01-15')], '_meta': {'symbols_requested': 1, 'symbols_with_dates': 1, 'symbol_date_coverage': 1.0}}
    return histories, cash, earnings


def test_v990_runner_builds_volume_only_research_frames_and_keeps_trial18_locked(monkeypatch):
    histories, cash, earnings = _fixtures()
    seen = {}

    def fake_eval(frames, **kwargs):
        seen['frame'] = frames['AAA'].copy()
        seen['earnings'] = kwargs.get('earnings_map')
        return {'status': 'PASS_TRIAL20_VOLUME_OOS_GATE', 'pass': True, 'trial18_state': 'LOCKED', 'production_activation': False}

    monkeypatch.setattr(backtest.v99_volume_gate, 'evaluate_trial20', fake_eval)
    out = backtest.run_v99_trial20(None, symbols=['AAA'], integrity_data={
        'nse_history_by_symbol': histories,
        'nse_cash_by_symbol': cash,
        'earnings_map': earnings,
    })
    assert out['build'] == backtest.v99_volume_gate.BUILD_ID
    assert out['trial18_eligible'] is False
    assert out['oi_role'] == 'DIAGNOSTIC_ONLY'
    assert out['trial20_validation']['status'] == 'PASS_TRIAL20_VOLUME_OOS_GATE'
    assert 'futures_turnover_notional' in seen['frame']
    assert 'abnormal_futstk_volume' in seen['frame']
    assert seen['earnings'] is earnings
