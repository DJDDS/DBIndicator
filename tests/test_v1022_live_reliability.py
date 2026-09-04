from pathlib import Path
import datetime as dt
import sys
import types

import pytest

if "kiteconnect" not in sys.modules:
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = type("KiteConnect", (), {})
    sys.modules["kiteconnect"] = mod

from app import background, scanner


def test_settings_page_does_not_call_live_kite_on_get():
    text = Path("app/web.py").read_text(encoding="utf-8")
    block = text.split('def settings_page():', 1)[1].split('@app.route("/settings/load-fno-list"', 1)[0]
    assert "scanner.get_fno_stock_list(kite)" not in block
    assert 'last_fno_symbols' in block


def test_resolve_live_fno_universe_falls_back_to_last_known_good(monkeypatch):
    with background._state_lock:
        old = dict(background._state)
        background._state["last_fno_symbols"] = ["AAA", "BBB"]
    try:
        monkeypatch.setattr(scanner, "get_fno_stock_list", lambda kite: (_ for _ in ()).throw(TimeoutError("Kite timed out")))
        symbols, source = background._resolve_live_fno_symbols(object())
        assert symbols == ["AAA", "BBB"]
        assert source == "LAST_KNOWN_GOOD"
    finally:
        with background._state_lock:
            background._state.clear(); background._state.update(old)


def test_scan_attempt_state_separates_attempt_from_success_and_uses_bounded_backoff(monkeypatch):
    fixed = dt.datetime(2026, 9, 4, 10, 0, 0)
    monkeypatch.setattr(background, "now_ist", lambda: fixed)
    with background._state_lock:
        old = dict(background._state)
    try:
        background._record_scan_attempt_start()
        state = background.get_state()
        assert state["last_scan_attempt"] == "2026-09-04T10:00:00"
        assert state["scan_status"] == "RUNNING"
        assert state["last_scan"] != state["last_scan_attempt"]

        delay1 = background._record_scan_attempt_failure("Read timed out")
        state = background.get_state()
        assert state["last_scan_attempt_status"] == "FAILED"
        assert state["scan_status"] == "RETRYING"
        assert state["consecutive_scan_failures"] == 1
        assert state["last_scan_attempt_error"] == "Read timed out"
        assert 5 <= delay1 <= 60

        delay2 = background._record_scan_attempt_failure("Read timed out")
        assert delay2 >= delay1
        assert delay2 <= 60

        background._record_scan_attempt_success("2026-09-04T10:00:30")
        state = background.get_state()
        assert state["last_scan"] == "2026-09-04T10:00:30"
        assert state["last_scan_attempt_status"] == "SUCCESS"
        assert state["scan_status"] == "RUNNING"
        assert state["consecutive_scan_failures"] == 0
    finally:
        with background._state_lock:
            background._state.clear(); background._state.update(old)


def test_scan_watchlist_continues_price_scan_when_oi_master_times_out(monkeypatch):
    monkeypatch.setattr(scanner, "_load_instrument_map", lambda kite: {"AAA": 123})
    monkeypatch.setattr(scanner, "fetch_oi_map", lambda kite, symbols: (_ for _ in ()).throw(TimeoutError("NFO timeout")))
    frame = scanner.pd.DataFrame({
        "open": [100.0] * 80, "high": [101.0] * 80, "low": [99.0] * 80,
        "close": [100.0 + i * 0.01 for i in range(80)], "volume": [1000] * 80,
    }, index=scanner.pd.date_range("2026-09-01 09:15", periods=80, freq="15min"))
    monkeypatch.setattr(scanner, "fetch_candles", lambda *a, **k: frame)
    monkeypatch.setattr(scanner, "compute_signal", lambda *a, **k: {"direction": "Bullish", "close": 100.79})
    out = scanner.scan_watchlist(object(), timeframe="15minute", symbols=["AAA"])
    assert len(out) == 1
    assert out[0]["symbol"] == "AAA"
    assert "error" not in out[0]
    assert out[0]["oi"] is None


def test_cached_cash_tokens_can_seed_scanner_after_restart():
    original = dict(scanner._instrument_cache)
    try:
        scanner._instrument_cache.clear()
        scanner.seed_nse_instrument_cache({"AAA": 123, "BBB": 456})
        assert scanner.cached_nse_instrument_tokens(["AAA", "CCC"]) == {"AAA": 123}
    finally:
        scanner._instrument_cache.clear(); scanner._instrument_cache.update(original)


def test_dashboard_state_api_exposes_attempt_success_and_scanner_status():
    text = Path("app/web.py").read_text(encoding="utf-8")
    block = text.split('def api_dashboard_state():', 1)[1].split('@app.route', 1)[0]
    assert '"last_scan_attempt"' in block
    assert '"scan_status"' in block
    assert '"next_scan_due"' in block


def test_dashboard_template_shows_attempt_and_success_separately():
    text = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert 'last-attempt-text' in text
    assert 'last-scan-text' in text
    assert 'scan-status-text' in text



def test_fetch_candles_retries_once_when_kite_returns_empty_payload(monkeypatch):
    calls = {"n": 0}
    rows = [{
        "date": "2026-09-04T09:15:00+05:30", "open": 100.0, "high": 101.0,
        "low": 99.0, "close": 100.5, "volume": 1000,
    }]
    def fake_fetch(*args, **kwargs):
        calls["n"] += 1
        return [] if calls["n"] == 1 else rows
    monkeypatch.setattr(scanner, "_fetch_historical_chunked", fake_fetch)
    monkeypatch.setattr(scanner.time, "sleep", lambda _x: None)
    df = scanner.fetch_candles(object(), 123, "15minute")
    assert calls["n"] == 2
    assert not df.empty


def test_live_reliability_build_marker_is_visible_without_changing_research_build():
    assert background.LIVE_RELIABILITY_BUILD_ID == "2026-09-04-INSTITUTIONAL-V10.2.2-LIVE-RELIABILITY-HOTFIX"
    text = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "live-build-text" in text
