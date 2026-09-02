from pathlib import Path

from app import v95_daily_evidence, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]
V95_BUILD = '2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE'
V94_BUILD = '2026-09-01-INSTITUTIONAL-V9.4.0-MEASUREMENT-TRIAL14'


def test_v950_release_markers_are_current_but_v94_audit_id_is_preserved():
    assert v95_daily_evidence.BUILD_ID == V95_BUILD
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text(encoding='utf-8').strip() == V95_BUILD
    assert (ROOT / 'PRODUCTION_BUILD.txt').read_text(encoding='utf-8').strip() == V95_BUILD
    html = (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8')
    assert V95_BUILD in html
    # The V9.4 completed audit path keeps its own immutable build identity.
    assert V94_BUILD in html
    assert V94_BUILD in (ROOT / 'app/early_research.py').read_text(encoding='utf-8')


def test_v950_never_activates_a_production_playbook_or_live_signal_path():
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    for rel in ('app/background.py', 'app/scanner.py', 'app/v9_playbooks.py'):
        text = (ROOT / rel).read_text(encoding='utf-8')
        assert 'v95_daily_evidence' not in text
        assert 'trial15' not in text.lower()
    assert v95_daily_evidence.trial15_spec()['research_only'] is True
    assert v95_daily_evidence.trial16_spec()['locked'] is True


def test_v950_docs_close_trial13_and_trial14_and_explain_trial16_lock():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    changelog = (ROOT / 'V9_5_CHANGELOG.md').read_text(encoding='utf-8')
    combined = readme + '\n' + changelog
    assert 'V9.5 Daily OI Evidence Lab' in combined
    assert 'Trial 13' in combined and 'closed' in combined.lower()
    assert 'Trial 14' in combined and 'failed' in combined.lower()
    assert 'Trial 16' in combined and 'LOCKED' in combined
    assert 'ACTIVE_PLAYBOOKS = ()' in combined
