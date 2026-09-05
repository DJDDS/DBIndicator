from pathlib import Path

WEB = Path(__file__).parents[1] / 'app' / 'web.py'


def test_dashboard_state_exposes_v12_surfaces():
    source = WEB.read_text(encoding='utf-8')
    for token in (
        '"v12_trade_console"', '"v12_option_recorder"', '"v12_feasibility"',
        '"v12_earnings"', '"v12_trial25_status"',
    ):
        assert token in source


def test_v12_export_routes_are_registered():
    source = WEB.read_text(encoding='utf-8')
    for route in (
        '/api/v12-option-state/export', '/api/v12-option-snapshots/export',
        '/api/v12-earnings-state/export', '/api/v12-earnings-ledger/export',
    ):
        assert route in source
