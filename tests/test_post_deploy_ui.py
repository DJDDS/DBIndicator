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
    assert 'id="v93-progress-fill"' in text
    assert 'id="v92-progress-fill"' in text
    assert 'id="v4h-progress-fill"' in text
    assert "document.getElementById(activePrefix + '-error')" in text


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
    assert result['research_build_id'] == '2026-09-01-INSTITUTIONAL-V9.4.0-MEASUREMENT-TRIAL14'


def test_dashboard_scan_health_exposes_attempted_valid_and_error_counts():
    from app.v9_playbooks import scan_health_counts
    got = scan_health_counts([{'symbol': 'A'}, {'symbol': 'B', 'error': 'x'}, {'symbol': 'C'}])
    assert got == {'attempted': 3, 'valid': 2, 'errors': 1}
    text = Path('app/templates/index.html').read_text(encoding='utf-8')
    assert 'id="live-valid-count"' in text
    assert 'id="live-error-count"' in text
    assert 'Attempted' in text and 'Valid' in text and 'Errors' in text



def test_dashboard_template_compiles_in_flask_jinja_environment():
    from app import web
    template = web.app.jinja_env.get_template("index.html")
    assert template is not None



def test_v123_focus_dashboard_is_professional_dynamic_workspace():
    text = Path('app/templates/index.html').read_text(encoding='utf-8')
    assert 'DBIndicator · NSE F&amp;O Command Center' in text
    assert 'id="v123-kpi-active"' in text
    assert 'id="v123-kpi-continuation"' in text
    assert 'id="v123-kpi-shadow"' in text
    assert 'Shadow continuation mathematics' in text
    assert 'Fresh-entry gate' in text
    assert '/api/v123-continuation-shadow/export' in text
    assert 'renderV123Focus(state.v123_focus_desk, state.v123_market_observer, state.v122b_tactical)' in text


def test_web_exposes_continuation_shadow_export():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert '@app.route("/api/v123-continuation-shadow/export")' in text
    assert 'v123_continuation_shadow.jsonl' in text



def test_focus_dashboard_shows_dynamic_underlying_risk_plan():
    text = Path('app/templates/index.html').read_text(encoding='utf-8')
    assert 'Dynamic SL / Target Plan' in text
    assert 'underlying authoritative · option levels indicative' in text
    assert 'Target 1' in text
    assert 'Target 2 / runner' in text
    assert 'local delta+gamma' in text
    assert 'time_stop_minutes_if_no_followthrough' in text
