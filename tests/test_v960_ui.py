from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'app/templates/backtest.html'
BUILD = '2026-09-02-INSTITUTIONAL-V9.6.0-TRIAL17-INDEPENDENT-TOTAL-OI'


def test_v960_ui_exposes_trial17_as_primary_independent_validation():
    text = HTML.read_text(encoding='utf-8')
    assert BUILD in text
    assert 'V9.6 Trial 17' in text
    assert '1 Sep 2021' in text and '1 Sep 2023' in text
    assert 'total OI z' in text and '1.5' in text
    assert 'Trial 18 LOCKED' in text
    assert 'RESEARCH / SHADOW ONLY' in text
    assert '/api/v96/start' in text and '/api/v96/status' in text


def test_v960_ui_keeps_v95_and_prior_finals_locked():
    text = HTML.read_text(encoding='utf-8')
    assert 'V9.5.3 Contract Structure Feature Research' in text
    assert 'final 20%' in text.lower() and 'locked' in text.lower()
