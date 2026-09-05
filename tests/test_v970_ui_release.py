from pathlib import Path
from app import backtest, v97_trial19, v9_playbooks

ROOT=Path(__file__).resolve().parents[1]
BUILD='2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE'
CURRENT='2026-09-05-INSTITUTIONAL-V12.0.1-PERSISTENT-OPTION-RECORDER-HEALTH'


def test_v970_build_and_safety_markers():
    assert v97_trial19.BUILD_ID==BUILD
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip()==CURRENT
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip()=='2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
    assert v9_playbooks.ACTIVE_PLAYBOOKS==()
    assert v97_trial19.trial19_spec()['total_oi_z_min']==1.5
    assert v97_trial19.trial18_spec()['locked'] is True


def test_v970_backtest_ui_is_primary_and_keeps_v962_visible():
    text=(ROOT/'app/templates/backtest.html').read_text()
    assert 'V9.7.2 Trial 19' in text
    assert '2018-09-01' in text or '1 Sep 2018' in text
    assert 'same trading day' in text.lower()
    assert 'same DTE' in text or 'same-DTE' in text
    assert 'extreme_oi_event' in text
    assert 'Trial 18 LOCKED' in text
    assert 'V9.6.2 Trial 17' in text
    assert 'v97-run-btn' in text
    assert '/api/v97/status' in text


def test_v970_backtest_state_accessor_does_not_break_route_context():
    state=backtest.get_v97_trial19_state()
    assert state['mode']=='v97_trial19'
