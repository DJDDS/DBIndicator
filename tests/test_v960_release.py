from pathlib import Path
from app import v96_trial17, v95_daily_evidence, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]
BUILD = '2026-09-02-INSTITUTIONAL-V9.6.2-TRIAL17-PROMOTION-CONTROLS'
CURRENT = '2026-09-03-INSTITUTIONAL-V9.9.2-TRIAL20-LOG-RV-INTEGRITY-CLOSURE'


def test_v960_release_markers_and_production_safety():
    assert v96_trial17.BUILD_ID == BUILD
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip() == CURRENT
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip() == CURRENT
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    assert v96_trial17.trial17_spec()['total_oi_z_min'] == 1.5
    assert v96_trial17.trial18_spec()['locked'] is True
    assert v95_daily_evidence.trial15_spec()['final_20_locked'] is True


def test_v960_changelog_documents_independent_window_and_no_retuning():
    text=(ROOT/'V9_6_CHANGELOG.md').read_text(encoding='utf-8')
    assert '2021-09-01' in text and '2023-09-01' in text
    assert '1.5' in text
    assert 'Trial 18' in text and 'locked' in text.lower()
    assert 'ACTIVE_PLAYBOOKS' in text
