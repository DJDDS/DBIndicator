from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'app/templates/backtest.html'
BUILD = '2026-09-02-INSTITUTIONAL-V9.5.3-TRIAL15-CLOSED-CONTRACT-STRUCTURE'


def test_v953_ui_surfaces_closed_trial15_and_separate_contract_structure_research():
    text = HTML.read_text(encoding='utf-8')
    assert BUILD in text
    assert 'V9.5.3 Contract Structure Feature Research' in text
    assert 'cannot rescue Trial 15' in text
    assert 'cannot unlock Trial 16' in text
    assert 'RESEARCH / SHADOW ONLY' in text
    assert 'Trial 16 LOCKED' in text


def test_v953_ui_keeps_final_locked_language():
    text = HTML.read_text(encoding='utf-8').lower()
    assert 'final 20%' in text
    assert 'locked' in text
