from pathlib import Path

from app import oi_view, v9_playbooks

ROOT = Path(__file__).resolve().parents[1]


def _short(symbol, price=-1.0, oi_day=6.0, oi30=2.0, vol=1.2, **extra):
    row = {
        "symbol": symbol,
        "oi": 100,
        "oi_structure": "Short Buildup",
        "price_chg_today_pct": price,
        "oi_day_chg_pct": oi_day,
        "oi_chg_30m_pct": oi30,
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
    assert radar["bearish"][0]["status"] in {"HIGH ATTENTION", "BUILDING", "EARLY"}
    assert "Short Buildup" in radar["bearish"][0]["reasons"]
    assert radar["counts"]["bearish"] == 2


def test_live_opportunity_radar_keeps_research_radar_separate_from_validated_trades():
    rows = [_short("BEAR_A")]
    state = {"results": rows}

    production = v9_playbooks.dashboard_payload(state)
    radar = oi_view.live_opportunity_radar(rows)

    assert production["counts"]["intraday_trade"] == 0
    assert production["intraday"]["bearish"] == []
    assert radar["bearish"][0]["symbol"] == "BEAR_A"
    assert radar["label"] == "RESEARCH / SHADOW"
    assert radar["is_trade_signal"] is False


def test_live_opportunity_radar_penalizes_chasing_but_does_not_hide_the_stock():
    clean = _short("CLEAN", price=-1.2, oi_day=7.0, oi30=2.5, vol=1.4,
                   breakout_extension_atr=0.8)
    chased = _short("CHASED", price=-1.2, oi_day=7.0, oi30=2.5, vol=1.4,
                    breakout_extension_atr=1.8)

    radar = oi_view.live_opportunity_radar([chased, clean], limit=5)
    by_symbol = {r["symbol"]: r for r in radar["bearish"]}

    assert by_symbol["CLEAN"]["score"] > by_symbol["CHASED"]["score"]
    assert by_symbol["CHASED"]["chase_guard"] == "EXTENDED"
    assert any("1.25 ATR" in reason for reason in by_symbol["CHASED"]["reasons"])


def test_dashboard_template_has_live_opportunity_radar_and_clear_validated_separation():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")

    assert 'id="live-opportunity-radar"' in text
    assert 'id="lor-bullish"' in text
    assert 'id="lor-bearish"' in text
    assert "RESEARCH / SHADOW" in text
    assert "Validated Production Models" in text
    assert "function renderLiveOpportunityRadar" in text


def test_web_api_exposes_live_opportunity_radar():
    text = (ROOT / "app/web.py").read_text(encoding="utf-8")

    assert "live_opportunity_radar" in text
    assert 'payload["opportunity_radar"]' in text
    assert '"opportunity_radar": live_opportunity_radar(' in text
    assert 'market_breadth=state.get("breadth")' in text


def test_live_opportunity_radar_uses_4h_as_context_not_a_veto():
    agrees = _short("AGREES", htf_direction="Bearish")
    conflicts = _short("CONFLICTS", htf_direction="Bullish")

    radar = oi_view.live_opportunity_radar([conflicts, agrees], limit=5)
    by_symbol = {r["symbol"]: r for r in radar["bearish"]}

    assert by_symbol["AGREES"]["score"] > by_symbol["CONFLICTS"]["score"]
    assert "CONFLICTS" in by_symbol
    assert any("4H context" in reason for reason in by_symbol["AGREES"]["reasons"])
