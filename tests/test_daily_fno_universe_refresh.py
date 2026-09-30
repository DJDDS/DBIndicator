import datetime as dt

from app import scanner
from app.v123_market_stream import UniverseMomentumStreamService


class _FakeKite:
    def __init__(self):
        self.calls = []

    def instruments(self, exchange):
        self.calls.append(exchange)
        if exchange == "NSE":
            return [
                {"segment": "NSE", "tradingsymbol": "OLD", "instrument_token": 1},
                {"segment": "NSE", "tradingsymbol": "ANANDRATHI", "instrument_token": 2},
                {"segment": "NSE", "tradingsymbol": "ENRIN", "instrument_token": 3},
                {"segment": "NSE", "tradingsymbol": "UJJIVANSFB", "instrument_token": 4},
            ]
        if exchange == "NFO":
            return [
                {"instrument_type": "FUT", "name": "OLD"},
                {"instrument_type": "FUT", "name": "ANANDRATHI"},
                {"instrument_type": "FUT", "name": "ENRIN"},
                {"instrument_type": "FUT", "name": "UJJIVANSFB"},
                {"instrument_type": "FUT", "name": "NIFTY"},
            ]
        return []


def _service():
    return UniverseMomentumStreamService(
        publish_callback=lambda payload: None,
        metadata_provider=lambda: [],
        access_token_getter=lambda: None,
        kite_client_getter=lambda: None,
        api_key="x",
        sleep_fn=lambda seconds: None,
    )


def test_fno_universe_refreshes_stale_nse_cash_master(monkeypatch):
    monkeypatch.setattr(scanner, "_instrument_cache", {"OLD": 1})
    monkeypatch.setattr(scanner, "_instrument_cache_date", "2026-09-29")
    monkeypatch.setattr(scanner, "_fno_cache", {"date": "2026-09-29", "symbols": ["OLD"]})
    monkeypatch.setattr(scanner, "now_ist", lambda: dt.datetime(2026, 9, 30, 9, 15))

    kite = _FakeKite()
    symbols = scanner.get_fno_stock_list(kite)

    assert {"ANANDRATHI", "ENRIN", "UJJIVANSFB"}.issubset(set(symbols))
    assert scanner._instrument_cache_date == "2026-09-30"
    assert scanner._fno_cache["date"] == "2026-09-30"
    assert kite.calls.count("NSE") == 1
    assert kite.calls.count("NFO") == 1

    # Same-day calls should use the refreshed caches rather than refetching.
    again = scanner.get_fno_stock_list(kite)
    assert again == symbols
    assert kite.calls.count("NSE") == 1
    assert kite.calls.count("NFO") == 1


def test_v123_replaces_overnight_token_universe_once_per_day():
    svc = _service()
    svc._tokens = {"OLD": 1, "NIFTY 50": 99}
    svc._token_to_symbol = {1: "OLD", 99: "NIFTY 50"}
    svc._underlying_symbols = {"OLD"}
    svc._universe_date = "2026-09-29"
    svc._next_connect_at = dt.datetime(2026, 9, 30, 9, 30)

    calls = {"count": 0}

    def _resolve(_kite):
        calls["count"] += 1
        svc._underlying_symbols = {"OLD", "ANANDRATHI", "ENRIN", "UJJIVANSFB"}
        return {
            "OLD": 1,
            "ANANDRATHI": 2,
            "ENRIN": 3,
            "UJJIVANSFB": 4,
            "NIFTY 50": 99,
        }

    svc._resolve_universe = _resolve
    now = dt.datetime(2026, 9, 30, 9, 15)

    assert svc._ensure_daily_universe(object(), now) is True
    assert calls["count"] == 1
    assert svc._universe_date == "2026-09-30"
    assert {"ANANDRATHI", "ENRIN", "UJJIVANSFB"}.issubset(svc._tokens)
    assert svc._next_connect_at is None

    # Once today's universe is loaded, the hot loop must not rebuild it.
    assert svc._ensure_daily_universe(object(), now + dt.timedelta(minutes=1)) is False
    assert calls["count"] == 1
