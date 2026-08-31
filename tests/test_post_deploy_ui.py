from pathlib import Path


def test_15minute_research_can_request_one_year():
    from app import backtest
    lo, hi, default = backtest.backtest_day_bounds('15minute')
    assert lo <= 30
    assert hi >= 365
    assert default >= 30


def test_backtest_template_defines_early_research_ui_controller():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'function updateEarlyResearchUI(state)' in text
    assert "document.getElementById('er-progress-fill')" in text
    assert "document.getElementById('er-error')" in text


def test_settings_template_shows_live_and_research_symbol_counts():
    text = Path('app/templates/settings.html').read_text(encoding='utf-8')
    assert 'id="live-fno-count"' in text
    assert 'id="research-watchlist-count"' in text
    assert 'id="last-scan-count"' in text


def test_dashboard_template_polls_live_scan_state():
    text = Path('app/templates/index.html').read_text(encoding='utf-8')
    assert "fetch('/api/dashboard-state')" in text
    assert 'id="live-scan-count"' in text
    assert 'id="radar-count"' in text
    assert 'id="intraday-count"' in text
    assert 'id="swing-count"' in text
    assert 'function pollDashboardState()' in text


def test_web_exposes_dashboard_state_endpoint():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert '@app.route("/api/dashboard-state")' in text
    assert 'def api_dashboard_state()' in text

def test_backtest_template_updates_day_bounds_when_timeframe_changes():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'const BACKTEST_DAY_BOUNDS' in text
    assert "document.getElementById('scope-timeframe').addEventListener('change'" in text
    assert 'applyDayBounds' in text

def test_backtest_template_exposes_research_build_marker():
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'RESEARCH_BUILD_ID' in text
    assert 'Research build' in text


def test_aggregate_research_includes_build_id():
    from app.early_research import aggregate_research
    result = aggregate_research([])
    assert result['research_build_id'] == '2026-08-31-INSTITUTIONAL-V9.2.8-BACKTEST-INTEGRITY-SHADOW-RADAR'


def test_dashboard_scan_health_exposes_attempted_valid_and_error_counts():
    from app.v9_playbooks import scan_health_counts
    got = scan_health_counts([{'symbol': 'A'}, {'symbol': 'B', 'error': 'x'}, {'symbol': 'C'}])
    assert got == {'attempted': 3, 'valid': 2, 'errors': 1}
    text = Path('app/templates/index.html').read_text(encoding='utf-8')
    assert 'id="live-valid-count"' in text
    assert 'id="live-error-count"' in text
    assert 'Attempted' in text and 'Valid' in text and 'Errors' in text
