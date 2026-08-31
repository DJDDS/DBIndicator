import datetime as dt

import sys
import types

if "kiteconnect" not in sys.modules:
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = type("KiteConnect", (), {})
    mod.KiteTicker = type("KiteTicker", (), {})
    sys.modules["kiteconnect"] = mod

from app import background, v9_playbooks


def row(symbol, direction="Bullish", source="Opening Range", score=80):
    return {
        "symbol": symbol,
        "breakout_direction": direction,
        "v8_direction": direction,
        "fresh_breakout": True,
        "breakout_source": source,
        "breakout_extension_atr": 0.4,
        "v8_participation": float(score),
        "v8_relative": 75.0,
        "v8_derivatives": 75.0,
        "v8_structure": 80.0,
        "v8_oi_state": "Long Buildup" if direction == "Bullish" else "Fresh Short Buildup",
        "close_position_pct": 88.0 if direction == "Bullish" else 12.0,
        "basis_acceleration": 0.0,
        "vwap_side_agrees": True,
        "vwap_distance_atr": 0.2,
        "price_chg_60m_pct": 1.0 if direction == "Bullish" else -1.0,
        "oi_chg_60m_pct": 5.0,
        "tod_rvol": 2.0,
    }


def test_v9_live_keeps_unvalidated_bull_playbook_in_shadow_not_operational(monkeypatch):
    rows = [row("BULL1"), row("BULL2"), row("BULL3"), row("BULL4")]
    monkeypatch.setattr(background.news, "get_news_for_symbol", lambda symbol, limit=3: [])
    background._apply_v9_playbooks(rows, now=dt.datetime(2026, 8, 30, 10, 0))
    intraday, _swing = background._apply_v9_operational_shortlists(rows)
    assert intraday == []
    assert any(p.get("playbook") == v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION for p in rows[0]["v9_playbooks"])
    assert rows[0]["v9_intraday_playbook"] is None


def test_v9_live_uses_real_cached_catalyst_headline(monkeypatch):
    rows = [row("NEWS", source="Recent Range")]
    monkeypatch.setattr(background.news, "get_news_for_symbol", lambda symbol, limit=3: [{
        "title": "NEWS wins major order worth Rs 1500 crore",
        "published_at": "2026-08-30T09:30:00+05:30",
        "sentiment_score": 0.8,
    }])
    background._apply_v9_playbooks(rows, now=dt.datetime.fromisoformat("2026-08-30T10:00:00+05:30"))
    plays = rows[0]["v9_playbooks"]
    assert any(p["playbook"] == v9_playbooks.BULL_CATALYST_CONTINUATION for p in plays)
    assert rows[0]["v9_intraday_playbook"] is None  # live/shadow until validated


def test_v9_operational_shortlists_do_not_promote_research_or_rejected_models(monkeypatch):
    bull = row("BULL")
    bear = row("BEAR", direction="Bearish", source="Recent Range")
    monkeypatch.setattr(background.news, "get_news_for_symbol", lambda symbol, limit=3: [])
    background._apply_v9_playbooks([bull, bear], now=dt.datetime(2026, 8, 30, 10, 0))
    intraday, swing = background._apply_v9_operational_shortlists([bull, bear])
    assert intraday == []
    assert swing == []


def test_live_indicator_payload_exposes_failed_breakout_confirmation():
    from pathlib import Path
    text = Path("app/indicators.py").read_text(encoding="utf-8")
    assert '"failed_breakout_direction": failed_breakout_direction' in text
    assert '"failed_breakout_vwap_reject": failed_breakout_vwap_reject' in text


def test_v9_refreshes_real_news_only_for_strong_bullish_attention(monkeypatch):
    called = []
    monkeypatch.setattr(background.news, "fetch_news_for_symbols", lambda symbols: called.extend(symbols) or {})
    rows = [
        row("HOT", "Bullish", "Recent Range", score=85),
        row("COLD", "Bullish", "Recent Range", score=50),
        row("BEARNEWS", "Bearish", "Recent Range", score=90),
    ]
    background._refresh_v9_catalyst_news(rows)
    assert "HOT" in called
    assert "COLD" not in called
    assert "BEARNEWS" not in called
