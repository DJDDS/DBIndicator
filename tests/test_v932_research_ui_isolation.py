from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _html():
    return (ROOT / 'app/templates/backtest.html').read_text(encoding='utf-8')


def _web():
    return (ROOT / 'app/web.py').read_text(encoding='utf-8')


def test_v932_each_research_job_has_its_own_progress_channel():
    text = _html()
    for prefix in ('er-v93', 'er-v92', 'er-4h'):
        assert f'id="{prefix}-progress"' in text
        assert f'id="{prefix}-progress-fill"' in text
        assert f'id="{prefix}-progress-label"' in text
        assert f'id="{prefix}-error"' in text
        assert f'id="{prefix}-form-hint"' in text
    assert 'id="er-progress"' not in text
    assert 'id="er-progress-fill"' not in text
    assert 'id="er-progress-label"' not in text


def test_v932_v93_progress_is_not_nested_in_v92_card():
    text = _html()
    v93_start = text.index('<h2>1 &middot; V9.3 Component Edge Laboratory</h2>')
    v92_start = text.index('<h2>2 &middot; V9.2 Manual Diagnostic</h2>')
    fourh_start = text.index('<h2>3 &middot; 4H Diagnostic</h2>')
    assert v93_start < text.index('id="er-v93-progress"') < v92_start
    assert v92_start < text.index('id="er-v92-progress"') < fourh_start
    assert fourh_start < text.index('id="er-4h-progress"')


def test_v932_job_mode_routes_to_matching_progress_channel():
    text = _html()
    assert "v93_lab: 'er-v93'" in text
    assert "v91_fast: 'er-v92'" in text
    assert "legacy_4h: 'er-4h'" in text
    assert "legacy: 'er-4h'" in text
    assert 'activeResearchChannel' in text


def test_v932_v92_copy_is_manual_not_primary():
    text = _html()
    assert '<h2>2 &middot; V9.2 Manual Diagnostic</h2>' in text
    assert 'V9.2 is retained as a manual diagnostic only' in text
    assert 'This is the primary V9.2 research path' not in text


def test_v932_custom_backtest_ui_and_api_are_removed():
    text = _html()
    web = _web()
    assert 'Custom backtest' not in text
    assert 'id="bt-form"' not in text
    assert 'id="bt-run-btn"' not in text
    assert 'id="bt-results"' not in text
    assert '@app.route("/api/backtest/start"' not in web
    assert '@app.route("/api/backtest/status"' not in web


def test_v932_backtest_page_only_exposes_three_primary_research_actions():
    text = _html()
    assert 'Run V9.3 Anticipation Lab' in text
    assert 'Run V9.2 Diagnostic Reset' in text
    assert 'Run 4H Diagnostic' in text
    assert 'Run backtest' not in text
    assert 'Run gate sweep' not in text
    assert 'Run the overnight test' not in text
