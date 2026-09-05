from app.v12_trade_console import build_trade_console


def _radar_row(symbol="AAA", score=72, **kw):
    row = {
        "symbol": symbol,
        "direction": "Bullish",
        "score": score,
        "status": "HIGH ATTENTION" if score >= 70 else "BUILDING",
        "chase_guard": "OK",
        "vwap_agrees": True,
        "reasons": ["Relative leader", "High participation"],
        "price_chg_pct": 1.2,
        "oi_30m_chg_pct": 2.1,
        "tod_rvol": 1.4,
        "relative": 80,
        "participation": 78,
    }
    row.update(kw)
    return row


def _result(symbol="AAA", **kw):
    row = {
        "symbol": symbol,
        "close": 100.0,
        "vwap": 99.5,
        "atr": 2.0,
        "fut_price_near": 100.2,
        "fut_spread_bps": 8.0,
        "option_contract": None,
        "option_spread_pct": None,
        "option_intelligence": {},
    }
    row.update(kw)
    return row


def _console(radar_rows, results, swing=None):
    radar = {"bullish": radar_rows, "bearish": [], "label": "RESEARCH / SHADOW"}
    swing = swing or {"1D": {"bullish": [], "bearish": []}, "2D": {"bullish": [], "bearish": []}}
    return build_trade_console(radar, swing, results)


def test_low_score_is_observe_and_never_claims_validation():
    out = _console([_radar_row(score=49)], [_result()])
    row = out["intraday"][0]
    assert row["trade_state"] == "OBSERVE"
    assert row["not_validated"] is True
    assert row["validation_label"] == "NOT VALIDATED"


def test_mid_score_is_watch_even_with_liquidity():
    row = _console([_radar_row(score=64)], [_result()])["intraday"][0]
    assert row["trade_state"] == "WATCH"
    assert row["execution_route"] == "FUTURES"


def test_high_score_with_structure_but_no_executable_route_is_setup():
    result = _result(fut_price_near=None, fut_spread_bps=None)
    row = _console([_radar_row(score=78)], [result])["intraday"][0]
    assert row["trade_state"] == "SETUP"
    assert row["execution_route"] == "WAIT"
    assert row["trigger_reference"] == 99.5


def test_futures_liquidity_promotes_setup_to_executable_candidate():
    row = _console([_radar_row(score=78)], [_result(fut_spread_bps=11.5)])["intraday"][0]
    assert row["trade_state"] == "EXECUTABLE"
    assert row["execution_route"] == "FUTURES"
    assert row["display_state"] == "EXECUTABLE CANDIDATE · NOT VALIDATED"


def test_two_sided_tight_option_can_be_executable_route():
    intel = {"contract": {"symbol": "AAA26SEP100CE", "bid": 5.0, "ask": 5.15, "spread_pct": 2.956}}
    result = _result(fut_price_near=None, fut_spread_bps=None, option_contract="AAA26SEP100CE", option_spread_pct=2.956, option_intelligence=intel)
    row = _console([_radar_row(score=80)], [result])["intraday"][0]
    assert row["trade_state"] == "EXECUTABLE"
    assert row["execution_route"] == "OPTION"


def test_both_routes_are_reported_when_both_are_executable():
    intel = {"contract": {"symbol": "AAA26SEP100CE", "bid": 5.0, "ask": 5.1, "spread_pct": 1.98}}
    result = _result(option_contract="AAA26SEP100CE", option_spread_pct=1.98, option_intelligence=intel)
    row = _console([_radar_row(score=82)], [result])["intraday"][0]
    assert row["trade_state"] == "EXECUTABLE"
    assert row["execution_route"] == "BOTH"


def test_extended_high_score_is_downgraded_to_watch():
    row = _console([_radar_row(score=90, chase_guard="EXTENDED")], [_result()])["intraday"][0]
    assert row["trade_state"] == "WATCH"
    assert "do not chase" in row["operational_reason"].lower()


def test_missing_structural_reference_cannot_be_setup():
    result = _result(vwap=None, breakout_level=None, retained_breakout_level=None)
    row = _console([_radar_row(score=90)], [result])["intraday"][0]
    assert row["trade_state"] == "WATCH"


def test_console_returns_at_most_five_ranked_intraday_candidates():
    radar = [_radar_row(symbol=f"S{i}", score=90-i) for i in range(8)]
    results = [_result(symbol=f"S{i}") for i in range(8)]
    out = _console(radar, results)
    assert len(out["intraday"]) == 5
    assert [r["symbol"] for r in out["intraday"]] == ["S0", "S1", "S2", "S3", "S4"]


def test_swing_rows_are_enriched_without_becoming_validated():
    base = _radar_row(symbol="AAA", score=81)
    swing = {
        "1D": {"bullish": [{**base, "horizon_score": 83, "horizon_reason": "Ignition"}], "bearish": []},
        "2D": {"bullish": [], "bearish": []},
    }
    out = _console([base], [_result()], swing=swing)
    assert out["swing"]["1D"][0]["trade_state"] == "EXECUTABLE"
    assert out["swing"]["1D"][0]["not_validated"] is True
