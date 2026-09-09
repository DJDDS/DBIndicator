from pathlib import Path


def test_v1212_hotfix_release_contract_preserves_research_identity():
    changelog = Path('V12_1_2_CHANGELOG.md')
    assert changelog.exists()
    text = changelog.read_text(encoding='utf-8')
    assert 'V12.1.2' in text
    assert 'session rollover' in text.lower()
    assert 'callFromThread' in text
    assert 'STALE' in text
    assert 'Trial 25 remains LOCKED' in text
    assert 'No strategy thresholds changed' in text
    assert Path('RESEARCH_BUILD.txt').read_text(encoding='utf-8').strip() == (
        '2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB'
    )
