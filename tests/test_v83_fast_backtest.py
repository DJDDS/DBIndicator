from pathlib import Path

from app import early_research

ROOT = Path(__file__).resolve().parents[1]


def _event(i, direction="Bullish"):
    return {
        "symbol": f"S{i}",
        "signal_time": f"2026-05-{1 + (i % 20):02d} 10:15:00",
        "entry_time": f"2026-05-{1 + (i % 20):02d} 10:30:00",
        "direction": direction,
        "breakout_source": "Recent Range",
        "breakout_extension_atr": 0.4,
        "high": 101.0,
        "low": 99.0,
        "close": 100.8 if direction == "Bullish" else 99.2,
        "v8_alpha": 90 if direction == "Bullish" else 75,
        "v8_participation": 90,
        "v8_structure": 88,
        "v8_relative": 85,
        "v8_derivatives": 82,
        "v81_bear_pressure": 91 if direction == "Bearish" else None,
        "intraday_returns": {"30m": 0.1, "1h": 0.12, "2h": 0.15, "eod": 0.18},
        "swing_returns": {"1D": 0.22, "2D": 0.25},
        "mfe_atr": {"1D": 1.8},
        "mae_atr": {"1D": 0.8},
        "oi_status": "Confirmed",
    }


def test_fast_v8_aggregate_only_builds_v8_report_and_skips_legacy_labs(monkeypatch):
    # If the fast path accidentally touches the expensive legacy V6 lab, fail loudly.
    monkeypatch.setattr(early_research, "v6_edge_report", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy V6 lab called")))
    replays = [{"ignition_events": [_event(i, "Bullish" if i % 2 == 0 else "Bearish") for i in range(160)]}]

    result = early_research.aggregate_v8_research_fast(
        replays,
        holdout_pct=30.0,
        run_context={"setup_timeframe": "15minute", "days": 180},
    )

    assert result["fast_v8"] is True
    assert "v8_dual" in result
    assert "v6_edge_lab" not in result
    assert "sensitivity" not in result
    assert "interactions" not in result
    assert "recent_range_edge_lab" not in result
    assert "promotion_benchmark" not in result
    assert "excursions" not in result


def test_primary_v8_button_requests_fast_mode_and_ui_has_staged_progress():
    template = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    body = template[template.index("document.getElementById('er-v8-run-btn')"):]
    assert "mode:'v8_fast'" in body.replace(" ", "")
    assert "p.stage" in template
    assert "p.overall_pct" in template
    assert "Building cross-sectional ranks" in template
    assert "Validating Bull/Bear Top-K" in template


def test_legacy_diagnostic_button_explicitly_requests_full_mode():
    template = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    body = template[template.index("document.getElementById('er-run-btn')"):]
    assert "mode:'legacy'" in body.replace(" ", "")


def test_fast_result_hides_legacy_research_sections_in_ui():
    template = (ROOT / "app" / "templates" / "backtest.html").read_text(encoding="utf-8")
    assert "er-legacy-research" in template
    assert "r.fast_v8" in template
