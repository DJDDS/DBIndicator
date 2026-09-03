from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = '2026-09-03-INSTITUTIONAL-V9.9.2-TRIAL20-LOG-RV-INTEGRITY-CLOSURE'


def test_v991_release_markers_and_changelog():
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text().strip() == BUILD
    assert (ROOT / 'PRODUCTION_BUILD.txt').read_text().strip() == BUILD
    text = (ROOT / 'V9_9_CHANGELOG.md').read_text(encoding='utf-8')
    assert '# V9.9.1' in text
    assert 'two-way clustered covariance' in text
    assert 'V9.6/V9.8' in text
