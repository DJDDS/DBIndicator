from pathlib import Path

from app import v8_dual


def _row(symbol, direction, alpha, state, swing_alpha=None, swing_state=None):
    return {
        "symbol": symbol,
        "v8_direction": direction,
        "v8_alpha": alpha,
        "v8_state": state,
        "v8_structure": 91,
        "v8_participation": 93,
        "v8_relative": 89,
        "v8_derivatives": 87,
        "v8_oi_state": "Long Buildup" if direction == "Bullish" else "Fresh Short Buildup",
        "breakout_extension_atr": 0.35,
        "v8_reasons": ["Recent-Range escape", "Strong price acceptance"],
        "v8_swing_alpha": swing_alpha,
        "v8_swing_state": swing_state,
        "close": 100.0,
        "tod_rvol": 2.1,
        "oi_chg_60m_pct": 2.5,
    }


def test_v8_dashboard_payload_has_separate_bull_bear_and_intraday_swing_views():
    state = {
        "results": [
            _row("BULL", "Bullish", 94, "TRADE CANDIDATE", 90, "TRADE CANDIDATE"),
            _row("BEAR", "Bearish", 96, "TRADE CANDIDATE", 88, "WATCH"),
            _row("WATCH", "Bullish", 78, "WATCH", 76, "WATCH"),
        ],
        "last_scan": "2026-08-29T15:00:00+05:30",
        "last_error": None,
        "index_direction": "Bullish",
        "index_chg_pct": 0.4,
    }
    payload = v8_dual.dashboard_payload(state)
    assert payload["intraday"]["bullish"][0]["symbol"] == "BULL"
    assert payload["intraday"]["bearish"][0]["symbol"] == "BEAR"
    assert payload["swing"]["bullish"][0]["symbol"] == "BULL"
    assert payload["counts"]["intraday_trade"] == 2
    assert payload["counts"]["swing_trade"] == 1
    assert payload["last_scan"] == state["last_scan"]


def test_web_exposes_v8_dashboard_endpoint():
    text = Path("app/web.py").read_text(encoding="utf-8")
    assert '@app.route("/api/v8-dashboard")' in text
    assert 'def api_v8_dashboard()' in text
    assert 'v8_dual.dashboard_payload' in text
