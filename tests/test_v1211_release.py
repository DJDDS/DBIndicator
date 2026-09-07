from pathlib import Path


def test_v1211_hotfix_release_contract_preserves_research_identity():
    changelog = Path('V12_1_1_CHANGELOG.md')
    assert changelog.exists()
    text = changelog.read_text(encoding='utf-8')
    assert 'V12.1.1' in text
    assert 'quote_age_seconds' in text
    assert 'threaded=True' in text
    assert 'Trial 25 remains LOCKED' in text
    assert 'No strategy thresholds changed' in text
    assert Path('RESEARCH_BUILD.txt').read_text(encoding='utf-8').strip() == (
        '2026-09-06-INSTITUTIONAL-V12.1-INDEX-VOLATILITY-RECORDER-FEASIBILITY-LAB'
    )
