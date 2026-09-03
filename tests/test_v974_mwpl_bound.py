import pandas as pd

from app import v97_trial19 as t19


def test_v974_mwpl_bound_thresholds_are_preregistered():
    assert t19.mwpl_bound_non_load_bearing(0.05, 0.02) is True
    assert t19.mwpl_bound_non_load_bearing(0.0501, 0.02) is False
    assert t19.mwpl_bound_non_load_bearing(0.05, 0.0201) is False


def _bound_frame():
    rows = []
    dates = pd.bdate_range('2022-01-03', periods=10)
    for di, day in enumerate(dates):
        for i in range(10):
            event = i == 0
            rows.append({
                'date': day,
                'symbol': f'S{i:02d}',
                'nse_near_dte': 8,
                'dte_bucket': '6-10',
                'extreme_oi_event': event,
                'trial19_eligible': True,
                'movement_1d_atr': 1.12 if event else 1.0,
                'ban_flag': bool(event and di == 0),
                'mwpl_pct': 96.0 if event and di == 0 else 50.0,
            })
    return pd.DataFrame(rows)


def test_v974_mwpl_bound_reports_overlap_and_clean_lift_delta():
    out = t19.evaluate_mwpl_bound(_bound_frame(), bootstrap_reps=30)
    assert out['event_count'] == 10
    assert out['risk_event_count'] == 1
    assert out['event_overlap_fraction'] == 0.1
    assert out['all_1D']['lift'] is not None
    assert out['clean_1D']['lift'] is not None
    assert out['absolute_lift_delta'] < 0.02
    assert out['non_load_bearing'] is False  # overlap is 10%, above 5% bar
