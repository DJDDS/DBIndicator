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
    assert 'v9_playbooks.dashboard_payload' in text


def test_v8_dashboard_payload_exposes_option_intelligence_fields():
    from app import v8_dual
    row = {
        'symbol':'ABC','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE','v8_decision_score':93,
        'v8_alpha':93,'v8_structure':90,'v8_participation':92,'v8_relative':91,'v8_derivatives':80,
        'option_action':'OPTION BUYER EDGE','option_edge':'HIGH','option_buyer_score':82,
        'option_contract':'ABCSEP100CE','option_iv_pct':28.4,'option_spread_pct':1.2,'option_delta':0.52,
        'option_theta_day':-0.14,'option_iv_rv_ratio':0.94,'option_dte':12,'option_straddle_move_pct':3.2,
    }
    payload = v8_dual.dashboard_payload({'results':[row]})
    got = payload['intraday']['bullish'][0]
    assert got['option_action'] == 'OPTION BUYER EDGE'
    assert got['option_contract'] == 'ABCSEP100CE'
    assert got['option_buyer_score'] == 82.0
    assert got['option_iv_rv_ratio'] == 0.94


def test_v8_dashboard_api_includes_forward_option_validation_stats():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert 'derivative_intelligence.get_shadow_stats()' in text
    assert 'payload["option_forward"]' in text


def test_web_exposes_option_shadow_export_route():
    text = Path('app/web.py').read_text(encoding='utf-8')
    assert '@app.route("/api/option-shadow/export")' in text
    assert 'def api_option_shadow_export()' in text


def test_v8_swing_payload_uses_swing_specific_option_contract_and_dte():
    row = {
        'symbol':'ABC','v8_direction':'Bullish','v8_state':'TRADE CANDIDATE','v8_decision_score':93,
        'v8_alpha':93,'v8_structure':90,'v8_participation':92,'v8_relative':91,'v8_derivatives':80,
        'v8_swing_alpha':88,'v8_swing_state':'TRADE CANDIDATE',
        'option_contract':'ABCNEAR100CE','option_dte':1,'option_action':'OPTION BUYER EDGE',
        'option_swing_contract':'ABCFAR100CE','option_swing_dte':10,'option_swing_action':'OPTION BUYER EDGE',
        'option_swing_edge':'HIGH','option_swing_buyer_score':81,'option_swing_iv_rv_ratio':1.05,
        'option_swing_spread_pct':1.1,'option_swing_straddle_move_pct':4.0,
    }
    payload = v8_dual.dashboard_payload({'results':[row]})
    got = payload['swing']['bullish'][0]
    assert got['option_contract'] == 'ABCFAR100CE'
    assert got['option_dte'] == 10
