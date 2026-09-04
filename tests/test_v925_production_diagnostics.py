from pathlib import Path

from app import oi_view, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]


def test_symbol_scan_health_preserves_last_success_across_current_failure():
    previous = {
        "ABC": {"last_success": "2026-08-31T10:45:00+05:30", "last_error": None},
    }
    rows = [
        {"symbol": "ABC", "error": "no candle data returned", "error_stage": "candle_fetch"},
        {"symbol": "XYZ", "close": 100.0},
    ]

    health = v9_playbooks.update_symbol_scan_health(previous, rows, "2026-08-31T11:05:25+05:30")

    assert health["ABC"]["last_success"] == "2026-08-31T10:45:00+05:30"
    assert health["ABC"]["last_error"] == "2026-08-31T11:05:25+05:30"
    assert health["ABC"]["error_stage"] == "candle_fetch"
    assert health["XYZ"]["last_success"] == "2026-08-31T11:05:25+05:30"
    assert health["XYZ"]["last_error"] is None


def test_scan_failure_details_exposes_symbol_stage_error_and_last_success():
    rows = [
        {"symbol": "ABC", "error": "symbol not found on NSE", "error_stage": "instrument_lookup"},
        {"symbol": "XYZ", "close": 100.0},
    ]
    symbol_health = {
        "ABC": {
            "last_success": "2026-08-30T15:27:00+05:30",
            "last_error": "2026-08-31T11:05:25+05:30",
            "error_stage": "instrument_lookup",
            "error": "symbol not found on NSE",
        }
    }
    failures = v9_playbooks.scan_failure_details(rows, symbol_health)
    assert failures == [{
        "symbol": "ABC",
        "stage": "instrument_lookup",
        "error": "symbol not found on NSE",
        "last_success": "2026-08-30T15:27:00+05:30",
    }]


def test_live_market_state_summarizes_oi_breadth_and_rankings():
    rows = [
        {"symbol": "LONG1", "oi": 100, "oi_structure": "Long Buildup", "price_chg_today_pct": 1.2,
         "oi_day_chg_pct": 6.0, "oi_chg_30m_pct": 2.0, "vol_multiple": 1.8,
         "oi_accel_label": "Strong acceleration", "oi_chg_60m_pct": 2.5},
        {"symbol": "SHORT1", "oi": 100, "oi_structure": "Short Buildup", "price_chg_today_pct": -1.4,
         "oi_day_chg_pct": 8.0, "oi_chg_30m_pct": 3.0, "vol_multiple": 2.1,
         "oi_accel_label": "Moderate acceleration", "oi_chg_60m_pct": 3.5},
        {"symbol": "SHORT2", "oi": 100, "oi_structure": "Short Buildup", "price_chg_today_pct": -0.8,
         "oi_day_chg_pct": 5.0, "oi_chg_30m_pct": 1.5, "vol_multiple": 1.5,
         "oi_accel_label": "Stable", "oi_chg_60m_pct": 1.0},
        {"symbol": "SHORT3", "oi": 100, "oi_structure": "Short Buildup", "price_chg_today_pct": -0.5,
         "oi_day_chg_pct": 4.0, "oi_chg_30m_pct": 1.0, "vol_multiple": 0.7,
         "oi_accel_label": "Stable", "oi_chg_60m_pct": 0.5},
    ]
    summary = oi_view.live_market_state(rows, top_n=3)

    assert summary["breadth"]["long_buildup"] == 1
    assert summary["breadth"]["short_buildup"] == 3
    assert summary["bias"] == "Bearish"
    assert summary["oi_expansion"][0]["symbol"] == "SHORT1"
    assert summary["price_oi_confirmation"][0]["symbol"] == "SHORT1"
    assert summary["volume_oi"][0]["symbol"] == "SHORT1"
    assert summary["acceleration"]["strong"] == 1
    assert summary["acceleration"]["moderate"] == 1
    assert summary["acceleration"]["ready_60m"] == 4


def test_settings_and_dashboard_expose_valid_counts_failures_and_live_market_state():
    settings = (ROOT / "app/templates/settings.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    web = (ROOT / "app/web.py").read_text(encoding="utf-8")

    assert "Valid live symbols" in settings
    assert 'id="valid-live-scan-count"' in settings
    assert "scan_failures" in web
    assert 'id="scan-failure-panel"' in dashboard
    assert 'id="live-market-state"' in dashboard
    assert "market_state" in web


def test_backtest_copy_matches_current_research_architecture_and_build_is_bumped():
    text = (ROOT / "app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V9.1 focuses" not in text
    assert "V10.2 Research Integrity &amp; Feasibility Repair is the primary research architecture" in text
    assert "V9.4 remains visible as the completed measurement/audit path" in text
    assert "V9.2 remains a manual diagnostic only" in text
    assert "V9.5.3" in text
    assert "V9.4.0" in text


def test_scan_watchlist_tags_failure_stage(monkeypatch):
    from app import scanner
    import pandas as pd

    monkeypatch.setattr(scanner, "_load_instrument_map", lambda kite: {"BROKEN": 123})
    monkeypatch.setattr(scanner, "fetch_oi_map", lambda kite, universe: {})
    monkeypatch.setattr(scanner, "fetch_candles", lambda kite, token, timeframe: pd.DataFrame({
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]
    }))
    monkeypatch.setattr(scanner, "compute_signal", lambda df, timeframe, now=None: (_ for _ in ()).throw(RuntimeError("bad signal")))

    rows = scanner.scan_watchlist(object(), timeframe="15minute", symbols=["MISSING", "BROKEN"])

    assert rows[0]["error_stage"] == "instrument_lookup"
    assert rows[1]["error_stage"] == "signal_compute"


def test_dashboard_payload_includes_current_scan_failures():
    state = {
        "results": [{"symbol": "BAD", "error": "boom", "error_stage": "signal_compute"}],
        "scan_symbol_health": {"BAD": {"last_success": "2026-08-31T10:00:00+05:30"}},
    }
    payload = v9_playbooks.dashboard_payload(state)
    assert payload["scan_failures"] == [{
        "symbol": "BAD", "stage": "signal_compute", "error": "boom",
        "last_success": "2026-08-31T10:00:00+05:30",
    }]


def test_background_state_tracks_symbol_scan_health():
    import sys
    import types
    if "kiteconnect" not in sys.modules:
        mod = types.ModuleType("kiteconnect")
        mod.KiteConnect = type("KiteConnect", (), {})
        mod.KiteTicker = type("KiteTicker", (), {})
        sys.modules["kiteconnect"] = mod
    from app import background
    assert "scan_symbol_health" in background._state


def test_dashboard_js_renders_scan_failures_and_market_state():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert "function renderScanFailures" in text
    assert "function renderLiveMarketState" in text
    assert "renderScanFailures(state.scan_failures" in text
    assert "renderLiveMarketState(state.market_state" in text


def test_live_market_state_is_directional_when_only_one_fresh_positioning_side_exists():
    only_short = [{"symbol": "S", "oi": 1, "oi_structure": "Short Buildup"}]
    only_long = [{"symbol": "L", "oi": 1, "oi_structure": "Long Buildup"}]
    assert oi_view.live_market_state(only_short)["bias"] == "Bearish"
    assert oi_view.live_market_state(only_long)["bias"] == "Bullish"
