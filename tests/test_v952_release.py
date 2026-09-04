from pathlib import Path

from app import backtest, v95_daily_evidence

ROOT = Path(__file__).resolve().parents[1]
BUILD = '2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE'


def test_v952_release_markers_and_checkpoint_schemas_are_nse_specific():
    assert v95_daily_evidence.BUILD_ID == BUILD
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text().strip() == '2026-09-04-INSTITUTIONAL-V11.0.3-IIMA-NUMERIC-FORMAT-INTEGRITY-HOTFIX'
    assert (ROOT / 'PRODUCTION_BUILD.txt').read_text().strip() == '2026-09-04-INSTITUTIONAL-V10.2.1-PROVENANCE-STATISTICAL-INTEGRITY-LOCK'
    assert backtest._V95_RESUME_SCHEMA.startswith('v952-nse-')
    assert backtest._V95_RUN_SCHEMA.startswith('v952-nse-')


def test_v952_backtest_ui_discloses_official_nse_history_and_actual_contract_structure():
    text = (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8')
    assert BUILD in text
    assert 'official NSE' in text
    assert 'near / next / far' in text
    assert 'actual contract expiries' in text
    assert 'Kite OI' in text and 'not used' in text


def test_v952_default_research_state_has_four_visible_stages():
    state = backtest._default_v95_daily_state()
    assert state['progress']['stage_total'] == 4
