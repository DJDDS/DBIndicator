from pathlib import Path

from app import v97_trial19
from app.v9_playbooks import ACTIVE_PLAYBOOKS

BUILD = '2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE'


def test_v974_release_marker_and_frozen_trial19_constants():
    assert v97_trial19.BUILD_ID == BUILD
    assert str(v97_trial19.INDEPENDENT_START.date()) == '2018-09-01'
    assert str(v97_trial19.INDEPENDENT_END.date()) == '2021-08-31'
    assert v97_trial19.TOTAL_OI_Z_MIN == 1.5
    assert v97_trial19.MIN_MATCHED_LIFT == 1.10
    assert v97_trial19.T_STAT_HURDLE == 3.0
    assert ACTIVE_PLAYBOOKS == ()


def test_v974_backtest_ui_surfaces_confound_controls_and_retires_discovery_projection():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert BUILD in text
    assert 'V9.7.2 Trial 19 · Confound &amp; Integrity Closure' in text
    assert 'prior 5-day realised-volatility' in text
    assert 't-1 / t-2' in text
    assert 'Earnings ±5 sessions' in text
    assert 'replicated planning effect ~1.13×' in text
    assert '1.22× discovery estimate is retired for economic projection' in text
    assert 'recent_mwpl_bound' in text
    assert 'confound_controls' in text
