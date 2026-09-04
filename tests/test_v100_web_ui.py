from pathlib import Path


def test_v10_routes_and_ui_are_present_and_trial23_locked():
    web=Path('app/web.py').read_text()
    html=Path('app/templates/backtest.html').read_text()
    assert '/api/v10/start' in web
    assert '/api/v10/status' in web
    assert 'V10.2.1 Provenance &amp; Statistical Integrity Lock' in html
    assert 'Trial 21' in html and 'Trial 22' in html
    assert 'Trial 23 CLOSED' in html
    assert 'v10-run-btn' in html
    assert "'/api/v10/start'" not in html
    assert 'Alpha reread disabled' in html


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


def test_v1001_background_worker_wires_archive_progress_into_v10_state():
    source=Path('app/backtest.py').read_text()
    assert 'input_progress_cb=_input_progress' in source
    assert 'def _input_progress(stage_index,stage_total,stage,overall_pct,done,total,item):' in source
    assert '"done":int(done)' in source and '"total":int(total)' in source

def test_v102_ui_reports_weighting_basis_density_and_feasibility_gate():
    html=Path('app/templates/backtest.html').read_text()
    assert 'Event gross' in html and 'Event net' in html
    assert 'Day gross' in html and 'Day net' in html
    assert 'Events/day' in html
    assert 'Naive t' in html and 'Day t' in html
    assert 'Pre-Trial Feasibility Gate' in html
    assert 'Trial 23 CLOSED' in html
