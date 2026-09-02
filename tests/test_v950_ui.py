from pathlib import Path

from app import backtest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'app/templates/backtest.html'
WEB = ROOT / 'app/web.py'


def _html():
    return HTML.read_text(encoding='utf-8')


def _web():
    return WEB.read_text(encoding='utf-8')


def test_v950_backtest_has_isolated_daily_oi_evidence_card_and_v94_is_preserved():
    text = _html()
    assert 'V9.5 Daily OI Evidence Lab' in text
    assert 'Unexpected Daily OI' in text
    assert '3-year daily' in text or '3+ year daily' in text
    assert 'Trial 15' in text
    assert 'Trial 16' in text and 'LOCKED' in text
    assert 'final 20%' in text.lower()
    assert 'RESEARCH / SHADOW ONLY' in text
    assert 'Run V9.5 Daily OI Evidence Lab' in text
    # V9.4 remains visible as the completed audit path rather than being replaced.
    assert 'V9.4 Measurement Repair + Magnitude Lab' in text
    assert 'Run V9.4 Measurement Lab' in text


def test_v950_ui_calls_separate_start_and_status_endpoints():
    text = _html()
    web = _web()
    assert "fetch('/api/v95/status')" in text
    assert "startJob('/api/v95/start'" in text
    assert 'initialV95State' in text
    assert 'updateV95UI' in text
    assert '@app.route("/api/v95/start", methods=["POST"])' in web
    assert '@app.route("/api/v95/status")' in web
    assert 'backtest.start_v95_daily_oi_evidence' in web
    assert 'backtest.get_v95_daily_oi_state' in web


def test_v950_state_is_separate_from_v94_and_research_only():
    state = backtest.get_v95_daily_oi_state()
    assert state['status'] in {'idle', 'running', 'done', 'error'}
    assert 'progress' in state
    assert state['mode'] == 'v95_daily'
    assert state['research_only'] is True
    # V9.4 has its own legacy state object and remains available.
    assert isinstance(backtest.get_early_research_state(), dict)


def test_v950_page_header_marks_v95_as_current_research_architecture():
    text = _html()
    assert '<strong>Research build:</strong> 2026-09-02-INSTITUTIONAL-V9.5.2-NSE-DAILY-OI-EVIDENCE' in text
    assert '<strong>V9.5 Daily OI Evidence Lab is the primary research architecture.</strong>' in text


def test_v950_results_surface_audit_diagnostics_and_discovery_overlap_guard():
    text = _html()
    assert 'V9.4 discovery-window guard' in text
    assert 'Raw positive OI z' in text
    assert 'Unexpected negative OI z' in text
    assert 'Prior OI level z' in text
