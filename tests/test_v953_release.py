from pathlib import Path
from app import v95_daily_evidence, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]
BUILD = '2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE'


def test_v953_release_markers_and_production_safety():
    assert v95_daily_evidence.BUILD_ID == BUILD
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text().strip() == '2026-09-03-INSTITUTIONAL-V10.0.0-DIRECTIONAL-EDGE-LAB'
    assert (ROOT / 'PRODUCTION_BUILD.txt').read_text().strip() == '2026-09-03-INSTITUTIONAL-V10.0.0-DIRECTIONAL-EDGE-LAB'
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    assert v95_daily_evidence.trial16_spec()['locked'] is True


def test_v953_changelog_documents_trial15_closure_without_unlocking_final():
    text = (ROOT / 'V9_5_3_CHANGELOG.md').read_text(encoding='utf-8')
    assert 'Trial 15' in text and 'closed' in text.lower()
    assert 'final 20%' in text.lower() and 'locked' in text.lower()
    assert 'MWPL' in text
    assert 'contract structure' in text.lower()
