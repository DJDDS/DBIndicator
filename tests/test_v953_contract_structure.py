import numpy as np
import pandas as pd
from app import v953_contract_structure as cs


def test_v953_contract_state_masks_are_mutually_explanatory():
    z = pd.DataFrame({
        'near_z': [2.0, -2.0, 0.2, -0.4],
        'next_z': [0.1, 2.0, 0.3, -0.1],
        'total_z': [1.7, 0.2, 2.1, -2.2],
    })
    masks = cs.classify_from_z(z)
    assert masks['fresh_near_creation'].tolist() == [True, False, False, False]
    assert masks['rollover_dominant'].tolist() == [False, True, False, False]
    assert masks['fresh_total_expansion'].tolist() == [True, False, True, False]
    assert masks['abnormal_unwind'].tolist() == [False, False, False, True]


def test_v953_contract_structure_report_is_research_only_and_never_reads_final():
    dates = pd.bdate_range('2023-01-02', periods=160)
    f = pd.DataFrame(index=dates)
    base = np.linspace(1000, 1200, len(dates))
    f['nse_near_oi'] = base
    f['nse_next_oi'] = base * 0.4
    f['nse_far_oi'] = base * 0.1
    f['nse_total_oi'] = f['nse_near_oi'] + f['nse_next_oi'] + f['nse_far_oi']
    f['movement_1d_atr'] = 1.0
    f['movement_2d_atr'] = 1.0
    f['eligible'] = True
    # Force a development/validation shock without touching final semantics.
    for i in range(100, 118, 3):
        f.iloc[i:, f.columns.get_loc('nse_near_oi')] *= 1.08
        f.iloc[i:, f.columns.get_loc('nse_total_oi')] = f.iloc[i:]['nse_near_oi'] + f.iloc[i:]['nse_next_oi'] + f.iloc[i:]['nse_far_oi']
        f.iloc[i, f.columns.get_loc('movement_1d_atr')] = 1.5
    out = cs.evaluate_contract_structure({'AAA': f}, bootstrap_reps=20)
    assert out['research_only'] is True
    assert out['final_20_locked'] is True
    assert out['trial_number'] is None
    assert set(out['features']) == {'fresh_near_creation','rollover_dominant','fresh_total_expansion','abnormal_unwind'}
