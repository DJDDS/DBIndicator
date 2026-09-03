from pathlib import Path


def test_v10_routes_and_ui_are_present_and_trial23_locked():
    web=Path('app/web.py').read_text()
    html=Path('app/templates/backtest.html').read_text()
    assert '/api/v10/start' in web
    assert '/api/v10/status' in web
    assert 'V10.0 Directional Edge Laboratory' in html
    assert 'Trial 21' in html and 'Trial 22' in html
    assert 'Trial 23 LOCKED' in html
    assert 'v10-run-btn' in html
    assert "'/api/v10/start'" in html


def test_backtest_page_injects_v10_state():
    web=Path('app/web.py').read_text()
    assert 'v10_state=backtest.get_v10_directional_state()' in web

def test_v10_ui_surfaces_failed_gates_and_2d_secondary_without_rescue():
    html=Path('app/templates/backtest.html').read_text()
    assert 'failed_gates' in html
    assert 'bull_2d' in html and 'bear_2d' in html
    assert '2D secondary' in html

def test_v10_ui_can_render_infinite_profit_factor_without_invalid_json():
    html=Path('app/templates/backtest.html').read_text()
    assert 'profit_factor_infinite' in html
