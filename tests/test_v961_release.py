from pathlib import Path
from app import v96_trial17, v9_playbooks

ROOT=Path(__file__).resolve().parents[1]
BUILD='2026-09-02-INSTITUTIONAL-V9.6.2-TRIAL17-PROMOTION-CONTROLS'
CURRENT='2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE'

def test_v961_release_markers_and_safety():
    assert v96_trial17.BUILD_ID == BUILD
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip() == CURRENT
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip() == '2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    assert v96_trial17.trial17_spec()['total_oi_z_min'] == 1.5
    assert v96_trial17.trial18_spec()['locked'] is True

def test_v961_ui_reports_new_integrity_controls():
    text=(ROOT/'app/templates/backtest.html').read_text(encoding='utf-8')
    assert BUILD in text
    assert 'historical cash' in text.lower()
    assert 'V9.6.2 Trial 17' in text
    assert '/api/v96/start' in text and '/api/v96/status' in text

def test_v961_changelog_freezes_trial17_and_keeps_trial18_locked():
    text=(ROOT/'V9_6_1_CHANGELOG.md').read_text(encoding='utf-8')
    assert 'z >= 1.5' in text
    assert '2021-09-01' in text and '2023-09-01' in text
    assert 'Trial 18 remains locked' in text
    assert 'ACTIVE_PLAYBOOKS = ()' in text
