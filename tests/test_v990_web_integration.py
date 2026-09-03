from pathlib import Path


def test_v990_web_route_passes_state_and_exposes_start_status_endpoints():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert 'v99_state=backtest.get_v99_trial20_state()' in text
    assert '@app.route("/api/v99/start", methods=["POST"])' in text
    assert 'backtest.start_v99_trial20(kite, symbols=symbols)' in text
    assert '@app.route("/api/v99/status")' in text
    assert 'backtest.get_v99_trial20_state()' in text
