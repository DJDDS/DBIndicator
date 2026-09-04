from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = '2026-09-04-INSTITUTIONAL-V11.0.2-IIMA-MF-SCHEMA-HOTFIX'


def test_v991_release_markers_and_changelog():
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text().strip() == BUILD
    assert (ROOT / 'PRODUCTION_BUILD.txt').read_text().strip() == '2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
    text = (ROOT / 'V9_9_CHANGELOG.md').read_text(encoding='utf-8')
    assert '# V9.9.1' in text
    assert 'two-way clustered covariance' in text
    assert 'V9.6/V9.8' in text
