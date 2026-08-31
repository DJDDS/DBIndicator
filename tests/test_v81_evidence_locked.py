from pathlib import Path

from app import early_research, v8_dual

ROOT = Path(__file__).resolve().parents[1]


def _event(i, direction, *, source="Recent Range", alpha=82, participation=88, relative=84,
           derivatives=86, bear_pressure=None, ret2h=0.1, ret1d=0.2, signal_time=None):
    return {
        "symbol": f"S{i}",
        "signal_time": signal_time or f"2026-01-{1 + i // 8:02d} 10:15:00",
        "entry_time": signal_time or f"2026-01-{1 + i // 8:02d} 10:30:00",
        "direction": direction,
        "breakout_source": source,
        "breakout_extension_atr": 0.4,
        "high": 101.0,
        "low": 99.0,
        "close": 100.8 if direction == "Bullish" else 99.2,
        "v8_alpha": alpha,
        "v8_participation": participation,
        "v8_structure": 80,
        "v8_relative": relative,
        "v8_derivatives": derivatives,
        "v81_bear_pressure": bear_pressure,
        "intraday_returns": {"2h": ret2h},
        "swing_returns": {"1D": ret1d},
        "mfe_atr": {"1D": 1.8},
        "mae_atr": {"1D": 0.9},
    }


def test_bear_pressure_uses_downside_participation_relative_derivatives_and_clv_not_structure():
    row = {
        "direction": "Bearish",
        "high": 110.0,
        "low": 100.0,
        "close": 100.2,
        "v8_participation": 92,
        "v8_relative": 90,
        "v8_derivatives": 94,
        "v8_structure": 10,
    }
    out = v8_dual.bear_pressure_score(row)
    assert out >= 90


def test_top_k_selection_is_point_in_time_and_does_not_use_trade_alpha_85_cutoff():
    rows = [
        _event(1, "Bullish", alpha=84, participation=90, signal_time="2026-01-10 10:15:00"),
        _event(2, "Bullish", alpha=82, participation=90, signal_time="2026-01-10 10:15:00"),
        _event(3, "Bullish", alpha=79, participation=90, signal_time="2026-01-10 10:15:00"),
        _event(4, "Bullish", alpha=75, participation=90, signal_time="2026-01-10 10:15:00"),
    ]
    selected = v8_dual.select_top_k(rows, score_field="v8_alpha", k=3, direction="Bullish",
                                    participation_floor=70, score_floor=70,
                                    allowed_sources={"Recent Range"})
    assert [r["symbol"] for r in selected] == ["S1", "S2", "S3"]


def test_bear_top_k_can_use_non_recent_range_breakouts_and_bear_pressure():
    rows = [
        _event(1, "Bearish", source="Opening Range", bear_pressure=94, signal_time="2026-01-10 10:15:00"),
        _event(2, "Bearish", source="Compression", bear_pressure=90, signal_time="2026-01-10 10:15:00"),
        _event(3, "Bearish", source="Recent Range", bear_pressure=88, signal_time="2026-01-10 10:15:00"),
        _event(4, "Bearish", source="Recent Range", bear_pressure=65, signal_time="2026-01-10 10:15:00"),
    ]
    selected = v8_dual.select_top_k(rows, score_field="v81_bear_pressure", k=3, direction="Bearish",
                                    participation_floor=70, score_floor=70, allowed_sources=None)
    assert [r["symbol"] for r in selected] == ["S1", "S2", "S3"]


def test_v81_report_has_predeclared_top_k_breadth_and_blocks_for_each_variant():
    events = []
    for day in range(80):
        t = f"2026-02-{1 + (day % 20):02d} {9 + (day % 4)}:15:00"
        for j, alpha in enumerate((92, 86, 82, 76, 72)):
            events.append(_event(day * 10 + j, "Bullish", alpha=alpha, participation=90,
                                 ret1d=0.25 if j < 3 else -0.05, signal_time=t))
        for j, bp in enumerate((94, 89, 84, 78, 72)):
            events.append(_event(5000 + day * 10 + j, "Bearish", source="Opening Range" if j == 0 else "Recent Range",
                                 alpha=60, participation=90, bear_pressure=bp,
                                 ret1d=0.20 if j < 3 else -0.04, signal_time=t))
    report = early_research.v8_dual_report(events)
    assert set(report["bullish"]["primary_variants"]) == {"top1", "top3", "top5"}
    assert set(report["bearish"]["primary_variants"]) == {"pressure_top1", "pressure_top3", "pressure_top5"}
    for side in ("bullish", "bearish"):
        for variant in report[side]["primary_variants"].values():
            assert len(variant["1D"]["validation_blocks"]) == 4
            assert len(variant["2h"]["validation_blocks"]) == 4
    assert report["protocol"]["selection"] == "point-in-time Top-K, predefined K=1/3/5"


def test_v7_is_retired_from_normal_v81_research_and_backtest_ui():
    early = (ROOT / "app" / "early_research.py").read_text(encoding="utf-8")
    template = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert 'result["v7_frozen"]' not in early
    assert 'id="er-v7-run-btn"' not in template
    assert 'V7 Frozen Final Test' not in template


def test_main_dashboard_no_longer_contains_v6_production_cards():
    template = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "V6 Intraday Entry" not in template
    assert "V6 Swing 1-2D" not in template
    assert "Swing remains long-only" not in template
    assert "V9.2 Live F&amp;O Monitor" in template


def test_live_operational_shortlists_are_driven_by_v81_trade_states_not_v6():
    import sys, types
    if "kiteconnect" not in sys.modules:
        mod = types.ModuleType("kiteconnect")
        mod.KiteConnect = type("KiteConnect", (), {})
        mod.KiteTicker = type("KiteTicker", (), {})
        sys.modules["kiteconnect"] = mod
    from app import background
    rows = [
        {"symbol": "BULL", "v8_direction": "Bullish", "v8_state": "TRADE CANDIDATE", "v8_decision_score": 92, "v8_alpha": 92, "v8_swing_state": "WATCH"},
        {"symbol": "BEAR", "v8_direction": "Bearish", "v8_state": "TRADE CANDIDATE", "v8_decision_score": 95, "v81_bear_pressure": 95, "v8_swing_state": "TRADE CANDIDATE", "v8_swing_alpha": 88},
        {"symbol": "WATCH", "v8_direction": "Bullish", "v8_state": "WATCH", "v8_decision_score": 78, "v8_alpha": 78, "v8_swing_state": "WATCH"},
    ]
    intraday, swing = background._apply_v81_operational_shortlists(rows)
    assert [r["symbol"] for r in intraday] == ["BEAR", "BULL"]
    assert [r["symbol"] for r in swing] == ["BEAR"]
    assert rows[0]["movement_stage"] == "V8.1 Bull Top-3"
    assert rows[1]["movement_stage"] == "V8.1 Bear Pressure Top-3"
    assert rows[2]["shortlist_rank"] is None


def test_v81_one_click_runner_locks_primary_research_to_180_days():
    template = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8").replace(" ", "")
    body = template[template.index("document.getElementById('er-v91-run-btn')"):]
    assert "document.getElementById('scope-days').value='180'" in body
    assert "timeframe:'15minute',days:'180'" in body
