from pathlib import Path
from app import v98_incremental_oi
from app import v97_trial19
from app.v9_playbooks import ACTIVE_PLAYBOOKS

BUILD='2026-09-03-INSTITUTIONAL-V9.8.0-INCREMENTAL-OI-VALIDATION'
CURRENT='2026-09-04-INSTITUTIONAL-V11.0.1-IIMA-FACTOR-SCHEMA-HOTFIX'


def test_v980_release_marker_preserves_frozen_trial19():
    assert v98_incremental_oi.BUILD_ID == BUILD
    assert v97_trial19.TOTAL_OI_Z_MIN == 1.5
    assert str(v97_trial19.INDEPENDENT_START.date()) == '2018-09-01'
    assert str(v97_trial19.INDEPENDENT_END.date()) == '2021-08-31'
    assert ACTIVE_PLAYBOOKS == ()


def test_v980_backtest_ui_surfaces_four_high_risk_tests_and_trial18_lock():
    text=Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert BUILD in text
    assert 'V9.8 Incremental OI Validation' in text
    assert 'Yang–Zhang' in text or 'Yang-Zhang' in text
    assert 'Full HAR' in text
    assert 'Volume horse race' in text
    assert 'Earnings join audit' in text
    assert 'Trial 18 LOCKED' in text
    assert 'v98_validation' in text


def test_v980_build_markers_are_current():
    assert Path('RESEARCH_BUILD.txt').read_text().strip()==CURRENT
    assert Path('PRODUCTION_BUILD.txt').read_text().strip()=='2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
