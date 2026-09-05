import datetime as dt
import sys
import types

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _import_background():
    if "kiteconnect" not in sys.modules:
        fake = types.ModuleType("kiteconnect")
        class KiteConnect:
            pass
        fake.KiteConnect = KiteConnect
        sys.modules["kiteconnect"] = fake
    from app import background
    return background


def test_background_v12_helper_refreshes_calendar_and_processes_scan(monkeypatch, tmp_path):
    background = _import_background()
    calls = []
    monkeypatch.setattr(background.config, 'V12_EARNINGS_STATE_FILE', str(tmp_path/'earn_state.json'))
    monkeypatch.setattr(background.config, 'V12_EARNINGS_LEDGER_FILE', str(tmp_path/'earn_ledger.jsonl'))
    monkeypatch.setattr(background.config, 'V12_OPTION_SNAPSHOT_FILE', str(tmp_path/'snap.jsonl'))
    monkeypatch.setattr(background.config, 'V12_OPTION_STATE_FILE', str(tmp_path/'opt_state.json'))
    monkeypatch.setattr(background.v12_live, 'refresh_earnings_calendar', lambda symbols, **kw: calls.append(('calendar', set(symbols))) or {'status': 'OK'})
    monkeypatch.setattr(background.v12_live, 'process_live_scan', lambda kite, results, radar, swing, **kw: calls.append(('scan', len(results))) or {'trade_console': {'intraday': []}, 'recorder': {'status': 'NOT_DUE'}, 'feasibility': {'trial25_locked': True}, 'earnings': {}, 'trial25_status': 'LOCKED'})

    out = background._run_v12_live(object(), [{'symbol':'ABC'}], {'bullish': [], 'bearish': []}, {'1D': {}, '2D': {}}, ['ABC'], now=dt.datetime(2026,9,5,9,31,tzinfo=IST))
    assert out['feasibility']['trial25_locked'] is True
    assert calls == [('calendar', {'ABC'}), ('scan', 1)]


def test_background_state_has_v12_surfaces():
    background = _import_background()
    state = background.get_state()
    assert 'v12_trade_console' in state
    assert 'v12_option_recorder' in state
    assert 'v12_feasibility' in state
    assert 'v12_earnings' in state
    assert 'v12_trial25_status' in state


def test_background_source_contains_lightweight_post_cas_v12_path():
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'app' / 'background.py').read_text(encoding='utf-8')
    assert 'v12_live.post_cash_derivative_window' in source
    assert 'V12-POST-CAS' in source
