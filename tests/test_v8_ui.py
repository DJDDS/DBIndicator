from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_has_v9_professional_playbook_console():
    text = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="v8-decision-console"' in text
    assert 'id="v8-bull-leaders"' in text
    assert 'id="v8-bear-leaders"' in text
    assert 'id="v8-dashboard-status"' in text
    assert 'id="v8-view-intraday"' in text
    assert 'id="v8-view-swing"' in text
    assert 'Bull Institutional Accumulation' in text
    assert 'Bear Fresh Short Buildup' in text


def test_dashboard_polls_v8_json_and_rerenders_without_page_reload():
    text = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "fetch('/api/v8-dashboard'" in text
    assert 'function pollV8Dashboard()' in text
    assert 'function renderV8Leaders(' in text
    assert 'setInterval(pollV8Dashboard' in text
    assert 'window.location.reload' not in text[text.index('function pollV8Dashboard()'):]


def test_dashboard_v8_cards_show_evidence_components_and_trade_state():
    text = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    for token in ('Structure', 'Participation', 'Relative', 'Derivatives', 'OI state', 'TRADE CANDIDATE', 'WATCH'):
        assert token in text


def test_backtest_template_has_v9_playbook_result_section():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert 'V9.2 Goal-Focused Evidence' in text
    assert 'v91_goal' in text
    assert 'Bull Institutional Accumulation' in text
    assert 'Bear Fresh Short Buildup' in text
    assert 'Bull Pullback/Reclaim' not in text
    assert 'Bear Failed Breakout' not in text
    assert 'Bear VWAP Retest Failure' not in text


def test_backtest_v81_renderer_reads_primary_variant_shape_and_blocks():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert "const primary = side.primary_variants || {};" in text
    assert 'validation_blocks' in text


def test_v9_has_dedicated_one_click_15m_backtest_runner():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert 'id="er-v91-run-btn"' in text
    assert 'Run V9.2 Diagnostic Reset' in text
    assert "timeframe:'15minute'" in text
    body = text[text.index("document.getElementById('er-v91-run-btn')"):]
    assert "startJob('/api/early-research/start'" in body


def test_v9_build_marker_is_current_research_build():
    current = '2026-09-02-INSTITUTIONAL-V9.6.0-TRIAL17-INDEPENDENT-TOTAL-OI'
    legacy_v94 = '2026-09-01-INSTITUTIONAL-V9.4.0-MEASUREMENT-TRIAL14'
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text(encoding='utf-8').strip() == current
    assert legacy_v94 in (ROOT / 'app' / 'early_research.py').read_text(encoding='utf-8')
    html = (ROOT / 'app' / 'templates' / 'backtest.html').read_text(encoding='utf-8')
    assert current in html and legacy_v94 in html


def test_dashboard_v82_cards_surface_option_expression_intelligence():
    text = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    for token in ('Derivative Intelligence', 'OPTION BUYER EDGE', 'IV/RV', 'ATM move', 'Spread', 'DTE'):
        assert token in text


def test_dashboard_shows_live_forward_option_validation_metric():
    text = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
    assert 'Option FW' in text
    assert 'Long-vol FW 1D' in text
    assert 'option_forward' in text
