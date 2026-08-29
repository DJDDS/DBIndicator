from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_has_v8_professional_dual_engine_console():
    text = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="v8-decision-console"' in text
    assert 'id="v8-bull-leaders"' in text
    assert 'id="v8-bear-leaders"' in text
    assert 'id="v8-dashboard-status"' in text
    assert 'id="v8-view-intraday"' in text
    assert 'id="v8-view-swing"' in text
    assert 'Bull Alpha' in text
    assert 'Bear Alpha' in text


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


def test_backtest_template_has_v8_dual_engine_result_section():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert 'V8 Dual Alpha' in text
    assert 'v8_dual' in text
    assert 'Bullish Engine' in text
    assert 'Bearish Engine' in text
    assert 'Full Consensus' in text
    assert 'Structure only' in text
    assert 'Participation only' in text
    assert 'Relative only' in text
    assert 'Derivatives only' in text


def test_backtest_v8_renderer_reads_report_variant_shape_directly():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert "const variant = side[key] || {};" in text
    assert "(side.variants || {})[key]" not in text


def test_v8_has_dedicated_one_click_15m_backtest_runner():
    text = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert 'id="er-v8-run-btn"' in text
    assert 'Run V8 Dual Alpha Backtest' in text
    assert "timeframe:'15minute'" in text
    body = text[text.index("document.getElementById('er-v8-run-btn')"):]
    assert "startJob('/api/early-research/start'" in body


def test_v8_build_marker_is_current_research_build():
    build_id = '2026-08-29-INSTITUTIONAL-V8-DUAL-ALPHA'
    assert (ROOT / 'RESEARCH_BUILD.txt').read_text(encoding='utf-8').strip() == build_id
    assert build_id in (ROOT / 'app' / 'early_research.py').read_text(encoding='utf-8')
    assert build_id in (ROOT / 'app' / 'templates' / 'backtest.html').read_text(encoding='utf-8')
