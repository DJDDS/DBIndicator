from pathlib import Path

from app.oi_view import event_driven_early_radar

ROOT = Path(__file__).resolve().parents[1]


def _scout(symbol="ABC", direction="Bullish", **extra):
    row = {
        "symbol": symbol,
        "direction": direction,
        "early_state": "FORMING",
        "early_eligible": True,
        "scout_eligible": True,
        "oi_acceleration": 0.6,
        "oi_accelerating_now": True,
        "participation_accelerating_now": True,
        "move_since_first_scout_atr": 0.1,
        "first_scout_age_min": 6,
        "maturity": "EARLY",
        "trigger_level": 101.0,
    }
    row.update(extra)
    return row


def _base(*rows):
    bulls = [r for r in rows if r.get("direction") == "Bullish"]
    bears = [r for r in rows if r.get("direction") == "Bearish"]
    return {
        "scout_bullish": bulls,
        "scout_bearish": bears,
        "bullish": [],
        "bearish": [],
        "counts": {},
    }


def test_pressure_shift_requires_event_not_composite_score():
    radar = _base(_scout(score=99.0, pressure=99.0))
    out = event_driven_early_radar(radar, {"candidates": []})
    assert out["rows"][0]["event_state"] == "PRESSURE_SHIFT"
    assert "score" not in out["rows"][0]
    assert out["rows"][0]["participation_shift"] is True
    assert out["rows"][0]["oi_accelerating"] is True


def test_high_legacy_score_without_fresh_participation_remains_scout():
    radar = _base(_scout(score=99.0, pressure=99.0, participation_accelerating_now=False))
    out = event_driven_early_radar(radar, {"candidates": []})
    assert out["rows"][0]["event_state"] == "SCOUT"


def test_live_three_minute_ready_overrides_static_scout():
    radar = _base(_scout())
    tactical = {
        "candidates": [{
            "symbol": "ABC", "direction": "Bullish", "state": "READY",
            "setup": "MICRO_BREAKOUT", "trigger": 101.2, "invalidation": 99.8,
            "rvol_3m": 1.6, "relative_3m_vs_nifty_pct": 0.2,
            "depth": {"support_fraction": 0.62},
            "basis": {"basis_change_60s_pct_points": 0.02},
        }]
    }
    row = event_driven_early_radar(radar, tactical)["rows"][0]
    assert row["event_state"] == "READY"
    assert row["trigger"] == 101.2
    assert row["invalidation"] == 99.8
    assert row["microstructure_shift"] is True


def test_tradeable_becomes_break_accepted_not_a_higher_score():
    radar = _base(_scout())
    tactical = {
        "candidates": [{
            "symbol": "ABC", "direction": "Bullish", "state": "TRADEABLE",
            "setup": "MICRO_BREAKOUT", "trigger": 101.2, "invalidation": 99.8,
            "live_price": 101.35, "tradeable": True,
            "option_route": {"contract": {"symbol": "ABC27OCT100CE", "dte": 35, "spread_pct": 1.0}},
        }]
    }
    row = event_driven_early_radar(radar, tactical)["rows"][0]
    assert row["event_state"] == "BREAK_ACCEPTED"
    assert row["option_contract"] == "ABC27OCT100CE"
    assert row["tradeable"] is True


def test_failure_state_is_explicit():
    radar = _base(_scout())
    tactical = {
        "candidates": [{
            "symbol": "ABC", "direction": "Bullish", "state": "TIME_EXIT",
            "reason": "setup failed its speed-class follow-through clock",
        }]
    }
    row = event_driven_early_radar(radar, tactical)["rows"][0]
    assert row["event_state"] == "FAILED"
    assert "follow-through" in row["reason"]


def test_visible_template_no_longer_uses_legacy_movement_score_section():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert "Event-Driven Early Detection" in text
    assert "no composite score" in text
    assert "Legacy Energy Building / Ignition scores are retained only in research logs" in text
    # The old exact visible section headings must be gone.
    assert "<h2>⚡ Production Early Radar" not in text
    assert ">Shadow Early Radar <" not in text
