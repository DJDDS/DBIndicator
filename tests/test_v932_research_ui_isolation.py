from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'app/templates/backtest.html'


def _text():
    return HTML.read_text(encoding='utf-8')


def test_v932_has_separate_progress_containers_for_each_research_mode():
    text = _text()
    for prefix in ('v93', 'v92', 'v4h'):
        assert f'id="{prefix}-progress"' in text
        assert f'id="{prefix}-progress-fill"' in text
        assert f'id="{prefix}-progress-label"' in text
        assert f'id="{prefix}-error"' in text
        assert f'id="{prefix}-form-hint"' in text
    assert 'id="er-progress"' not in text
    assert 'id="er-progress-fill"' not in text
    assert 'id="er-progress-label"' not in text


def test_v932_v93_results_live_inside_v93_card_and_not_v92_card():
    text = _text()
    v93_card_start = text.index('<h2>1 &middot; V9.4 Measurement Repair + Magnitude Lab</h2>')
    v92_card_start = text.index('<h2>2 &middot; V9.2 Diagnostic Reset</h2>')
    v93_results = text.index('id="er-v93-results"')
    assert v93_card_start < v93_results < v92_card_start


def test_v932_v92_copy_is_manual_legacy_diagnostic_not_primary():
    text = _text()
    v92_start = text.index('<h2>2 &middot; V9.2 Diagnostic Reset</h2>')
    v4h_start = text.index('<h2>3 &middot; 4H Diagnostic</h2>')
    copy = text[v92_start:v4h_start]
    assert 'manual legacy diagnostic' in copy.lower()
    assert 'primary V9.2 research path' not in copy


def test_v932_js_routes_running_state_to_mode_specific_progress_container():
    text = _text()
    assert "v93_lab: 'v93'" in text
    assert "v91_fast: 'v92'" in text
    assert "legacy_4h: 'v4h'" in text
    assert "const activePrefix = modePrefixes[researchMode]" in text
    assert "document.getElementById(activePrefix + '-progress')" in text


def test_v932_4h_has_its_own_card_and_status_area():
    text = _text()
    assert '<h2>3 &middot; 4H Diagnostic</h2>' in text
    assert 'id="er-run-btn"' in text
    assert 'completed 4H setup candles' in text
