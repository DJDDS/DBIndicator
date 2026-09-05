import datetime as dt
from pathlib import Path

import pytest


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def test_refresh_earnings_calendar_is_once_per_day_and_fail_closed(tmp_path, monkeypatch):
    from app import v12_live

    state_file = tmp_path / 'earnings_state.json'
    ledger_file = tmp_path / 'earnings_ledger.jsonl'
    calls = []

    def fake_fetch(session, symbols, start, end, timeout=25):
        calls.append((set(symbols), start, end))
        return {
            'status': 'OK',
            'events': [{'symbol': 'ABC', 'meeting_date': '2026-09-08', 'purpose': 'Financial Results', 'details': '', 'broadcast_at': None, 'source_fingerprint': 'x'}],
            'error': None,
        }

    monkeypatch.setattr(v12_live.v12_earnings_calendar, 'fetch_upcoming_earnings', fake_fetch)
    now = dt.datetime(2026, 9, 5, 9, 20, tzinfo=IST)
    first = v12_live.refresh_earnings_calendar(
        {'ABC', 'XYZ'}, now=now, state_file=state_file, ledger_file=ledger_file,
        session_factory=lambda: object(),
    )
    second = v12_live.refresh_earnings_calendar(
        {'ABC', 'XYZ'}, now=now.replace(hour=12), state_file=state_file, ledger_file=ledger_file,
        session_factory=lambda: object(),
    )
    assert first['status'] == 'OK'
    assert second['status'] == 'OK'
    assert len(calls) == 1


def test_process_live_scan_keeps_trade_console_when_recorder_fails(tmp_path, monkeypatch):
    from app import v12_live

    now = dt.datetime(2026, 9, 5, 9, 31, tzinfo=IST)
    radar = {'bullish': [{'symbol': 'ABC', 'score': 75, 'chase_guard': 'OK'}], 'bearish': []}
    swing = {'1D': {'bullish': [], 'bearish': []}, '2D': {'bullish': [], 'bearish': []}}
    results = [{'symbol': 'ABC', 'vwap': 100, 'fut_price_near': 101, 'fut_spread_bps': 8}]

    monkeypatch.setattr(v12_live.v12_earnings_calendar, 'upcoming_earnings_symbols', lambda state, today, days=7: [])
    monkeypatch.setattr(v12_live.v12_option_recorder, 'record_snapshot', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('quote down')))

    out = v12_live.process_live_scan(
        object(), results, radar, swing, now=now,
        option_snapshot_file=tmp_path/'snap.jsonl', option_state_file=tmp_path/'opt_state.json',
        earnings_state_file=tmp_path/'earn_state.json',
    )
    assert out['trade_console']['intraday'][0]['trade_state'] == 'EXECUTABLE'
    assert out['recorder']['status'] == 'ERROR'
    assert out['feasibility']['trial25_locked'] is True


def test_process_live_scan_reports_trial25_locked_before_ten_days(tmp_path, monkeypatch):
    from app import v12_live

    now = dt.datetime(2026, 9, 5, 13, 1, tzinfo=IST)
    monkeypatch.setattr(v12_live.v12_option_recorder, 'record_snapshot', lambda *a, **k: {'status': 'NOT_DUE'})
    out = v12_live.process_live_scan(
        object(), [], {'bullish': [], 'bearish': []}, {'1D': {}, '2D': {}}, now=now,
        option_snapshot_file=tmp_path/'snap.jsonl', option_state_file=tmp_path/'opt_state.json',
        earnings_state_file=tmp_path/'earn_state.json',
    )
    assert out['feasibility']['status'] == 'RECORDING — NO FEASIBILITY VERDICT'
    assert out['trial25_status'] == 'TRIAL 25 LOCKED — FORWARD INDIAN OPTION DATA REQUIRED.'


def test_post_cash_v12_capture_window_is_only_1530_to_1540_weekdays():
    from app import v12_live
    assert v12_live.post_cash_derivative_window(dt.datetime(2026, 9, 7, 15, 31, tzinfo=IST)) is True
    assert v12_live.post_cash_derivative_window(dt.datetime(2026, 9, 7, 15, 37, tzinfo=IST)) is True
    assert v12_live.post_cash_derivative_window(dt.datetime(2026, 9, 7, 15, 40, tzinfo=IST)) is True
    assert v12_live.post_cash_derivative_window(dt.datetime(2026, 9, 7, 15, 41, tzinfo=IST)) is False
    assert v12_live.post_cash_derivative_window(dt.datetime(2026, 9, 6, 15, 37, tzinfo=IST)) is False


def test_failed_earnings_refresh_is_throttled_for_one_hour(tmp_path, monkeypatch):
    from app import v12_live
    state_file = tmp_path/'earn_state.json'
    ledger_file = tmp_path/'earn_ledger.jsonl'
    calls = []
    def fail_fetch(session, symbols, start, end, timeout=25):
        calls.append(start)
        return {'status':'UNAVAILABLE','events':[],'error':'temporary outage'}
    monkeypatch.setattr(v12_live.v12_earnings_calendar, 'fetch_upcoming_earnings', fail_fetch)
    now = dt.datetime(2026,9,7,9,20,tzinfo=IST)
    a = v12_live.refresh_earnings_calendar({'ABC'}, now=now, state_file=state_file, ledger_file=ledger_file, session_factory=lambda: object())
    b = v12_live.refresh_earnings_calendar({'ABC'}, now=now+dt.timedelta(minutes=30), state_file=state_file, ledger_file=ledger_file, session_factory=lambda: object())
    c = v12_live.refresh_earnings_calendar({'ABC'}, now=now+dt.timedelta(minutes=61), state_file=state_file, ledger_file=ledger_file, session_factory=lambda: object())
    assert a['status'] == 'UNAVAILABLE' and b['status'] == 'UNAVAILABLE' and c['status'] == 'UNAVAILABLE'
    assert len(calls) == 2


def test_successful_earnings_refresh_clears_prior_error_on_disk(tmp_path, monkeypatch):
    from app import v12_live
    from app import v12_earnings_calendar as cal
    state_file = tmp_path/'earn_state.json'
    ledger_file = tmp_path/'earn_ledger.jsonl'
    cal._save_state(state_file, {'status':'UNAVAILABLE','events':{},'last_refresh_at':None,'last_attempt_at':'2026-09-07T08:00:00+05:30','error':'old'})
    monkeypatch.setattr(v12_live.v12_earnings_calendar, 'fetch_upcoming_earnings', lambda *a, **k: {'status':'OK','events':[],'error':None})
    now = dt.datetime(2026,9,7,10,0,tzinfo=IST)
    out = v12_live.refresh_earnings_calendar({'ABC'}, now=now, state_file=state_file, ledger_file=ledger_file, session_factory=lambda: object())
    persisted = cal._load_state(state_file)
    assert out['status'] == 'OK'
    assert persisted.get('error') is None
