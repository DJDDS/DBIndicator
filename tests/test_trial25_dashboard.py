from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_dashboard_context_and_api_expose_only_safe_trial25_summary():
    source = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert 'trial25_shadow=state.get("trial25_shadow") or {}' in source
    assert '"trial25_shadow": state.get("trial25_shadow") or {}' in source


def test_dashboard_has_stage_d_panel_and_live_renderer_without_efficacy_fields():
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="trial25-stage-d"' in html
    assert 'TRIAL 25 · EARNINGS VOLATILITY · STAGE D' in html
    assert 'id="trial25-completed"' in html
    assert 'id="trial25-frozen-universe"' in html
    assert 'id="trial25-current-overlap"' in html
    assert 'id="trial25-new-fno"' in html
    assert 'id="trial25-fno-missing"' in html
    assert 'id="trial25-stale-old-trade"' in html
    assert 'function renderTrial25Shadow' in html
    assert 'renderTrial25Shadow(state.trial25_shadow);' in html

    start = html.index('id="trial25-stage-d"')
    end = html.index('id="v121-index-recorder"', start)
    panel = html[start:end].lower()
    for forbidden in (
        'trial25-pnl', 'trial25-return', 'trial25-win-rate',
        'trial25-profit-factor', 'trial25-mean-return', 'trial25-t-stat',
    ):
        assert forbidden not in panel
