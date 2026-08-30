from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_backtest_ui_exposes_v9_build_and_keeps_legacy_v6_audit_locked():
    html = (ROOT / 'app/templates/backtest.html').read_text()
    build_id = '2026-08-30-INSTITUTIONAL-V9.1.2-STREAMING-BACKTEST'
    assert build_id in html
    assert build_id in (ROOT / 'app/early_research.py').read_text()
    assert build_id in (ROOT / 'RESEARCH_BUILD.txt').read_text()
    assert 'V6 Institutional Edge Lab' in html
    assert 'Final 20% locked' in html
    assert 'Path-aware Exit Lab' in html


def test_dashboard_retires_v6_production_cards_and_uses_v9_console():
    html = (ROOT / 'app/templates/index.html').read_text()
    assert 'V6 Intraday Entry' not in html
    assert 'V6 Swing 1-2D' not in html
    assert 'V9.1 Goal-Focused Scanner' in html
    assert 'Bullish Models' in html and 'Bearish Model' in html


def test_settings_explains_oi_is_sponsorship_not_mandatory():
    html = (ROOT / 'app/templates/settings.html').read_text()
    assert 'OI is sponsorship' in html
    assert 'not a universal hard gate' in html
