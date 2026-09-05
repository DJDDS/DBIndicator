from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / 'app' / 'templates' / 'index.html'


def test_v12_trade_console_and_recorder_cards_exist():
    html = TEMPLATE.read_text(encoding='utf-8')
    for token in (
        'id="v12-trade-console"', 'id="v12-intraday-list"', 'id="v12-swing-1d-list"',
        'id="v12-option-recorder"', 'id="v12-feasibility-status"', 'id="v12-trial25-status"',
        'EXECUTABLE CANDIDATE · NOT VALIDATED',
    ):
        assert token in html


def test_v12_ui_is_live_polled_from_dashboard_state():
    html = TEMPLATE.read_text(encoding='utf-8')
    assert 'function renderV12TradeConsole' in html
    assert 'function renderV12Recorder' in html
    assert 'renderV12TradeConsole(state.v12_trade_console);' in html
    assert 'renderV12Recorder(state.v12_option_recorder, state.v12_feasibility, state.v12_earnings, state.v12_trial25_status);' in html


def test_v12_export_links_are_visible():
    html = TEMPLATE.read_text(encoding='utf-8')
    for route in (
        '/api/v12-option-snapshots/export', '/api/v12-option-state/export',
        '/api/v12-earnings-ledger/export', '/api/v12-earnings-state/export',
    ):
        assert route in html
