from pathlib import Path

from app import oi_view, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]


def _short(symbol, price=-1.0, oi_day=6.0, oi30=2.0, vol=1.2, **extra):
    row = {
        "symbol": symbol,
        "oi": 100,
        "close": 100.0,
        "atr": 2.0,
        "prior_low_20d": 99.5,
        "price_chg_60m_pct": -0.20,
        "oi_structure": "Short Buildup",
        "price_chg_today_pct": price,
        "oi_day_chg_pct": oi_day,
        "oi_chg_15m_pct": 0.65,
        "oi_chg_30m_pct": oi30,
        "oi_acceleration": 0.45,
        "tod_rvol": max(1.15, vol),
        "tod_rvol_accel": 0.25,
        "vol_rising": True,
        "vol_multiple": vol,
        "oi_accel_label": "Moderate acceleration",
        "v8_relative": 75.0,
        "v8_participation": 70.0,
        "v8_structure": 65.0,
        "vs_vwap": "Below",
    }
    row.update(extra)
    return row


def test_live_opportunity_radar_surfaces_bearish_oi_even_when_no_playbook_is_active():
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    rows = [
        _short("BEAR_A", price=-1.8, oi_day=9.0, oi30=3.5, vol=1.8,
               oi_accel_label="Strong acceleration", v8_relative=92.0, v8_participation=88.0),
        _short("BEAR_B", price=-0.6, oi_day=4.0, oi30=1.0, vol=0.7,
               oi_accel_label="Stable", v8_relative=62.0, v8_participation=55.0),
    ]

    radar = oi_view.live_opportunity_radar(rows, limit=5)

    assert [r["symbol"] for r in radar["bearish"]] == ["BEAR_A", "BEAR_B"]
    assert radar["bearish"][0]["score"] > radar["bearish"][1]["score"]
    assert radar["bearish"][0]["direction"] == "Bearish"
    assert radar["bearish"][0]["early_state"] in {"FORMING", "READY", "FRESH_BREAK"}
    assert radar["bearish"][0]["early_eligible"] is True
    assert radar["counts"]["bearish"] == 2


def test_live_opportunity_radar_keeps_research_radar_separate_from_validated_trades():
    rows = [_short("BEAR_A")]
    state = {"results": rows}

    production = v9_playbooks.dashboard_payload(state)
    radar = oi_view.live_opportunity_radar(rows)

    assert production["counts"]["intraday_trade"] == 0
    assert production["intraday"]["bearish"] == []
    assert radar["bearish"][0]["symbol"] == "BEAR_A"
    assert radar["label"] == "EARLY MOVE · RESEARCH / SHADOW"
    assert radar["is_trade_signal"] is False


def test_live_opportunity_radar_hard_excludes_chasing_from_early_panel():
    clean = _short("CLEAN", price=-1.2, oi_day=7.0, oi30=2.5, vol=1.4,
                   breakout_extension_atr=0.8)
    chased = _short("CHASED", price=-1.2, oi_day=7.0, oi30=2.5, vol=1.4,
                    breakout_extension_atr=1.8)

    radar = oi_view.live_opportunity_radar([chased, clean], limit=5)
    by_symbol = {r["symbol"]: r for r in radar["bearish"]}

    assert "CLEAN" in by_symbol
    assert "CHASED" not in by_symbol
    assert radar["counts"]["hidden_mature"] >= 1


def test_dashboard_template_has_live_opportunity_radar_and_clear_validated_separation():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")

    assert 'id="live-opportunity-radar"' in text
    assert 'id="lor-bullish"' in text
    assert 'id="lor-bearish"' in text
    assert "RESEARCH / SHADOW" in text
    assert "Validated Production Models" in text
    assert "function renderLiveOpportunityRadar" in text


def test_web_api_exposes_canonical_early_radar_with_live_tactical_overlay():
    text = (ROOT / "app/web.py").read_text(encoding="utf-8")

    assert "live_opportunity_radar" in text
    assert "overlay_tactical_radar" in text
    assert 'payload["opportunity_radar"]' in text
    assert 'state.get("opportunity_radar")' in text
    assert 'state.get("v122b_tactical")' in text


def test_live_opportunity_radar_uses_4h_as_context_not_a_veto():
    agrees = _short("AGREES", htf_direction="Bearish")
    conflicts = _short("CONFLICTS", htf_direction="Bullish")

    radar = oi_view.live_opportunity_radar([conflicts, agrees], limit=5)
    by_symbol = {r["symbol"]: r for r in radar["bearish"]}

    assert "AGREES" in by_symbol
    assert "CONFLICTS" in by_symbol
    assert by_symbol["AGREES"]["score"] > by_symbol["CONFLICTS"]["score"]
