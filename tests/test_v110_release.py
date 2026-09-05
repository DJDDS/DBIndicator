from pathlib import Path


def test_v11_backtest_api_exposes_feasibility_without_outcomes():
    from app import backtest
    out = backtest.get_v11_feasibility()
    assert out["outcome_data_read"] is False
    assert out["winner"] == "TRIAL24_RESIDUAL_MOMENTUM_REPLICATION"
    assert out["trial24_registered"] is True


def test_v11_template_preserves_trial24_as_read_only_historical_record():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.0.5 Trial 24 Historical Record" in text
    assert "Trial 24 Historical Record · read-only" in text
    assert "final 20%" in text
    assert 'id="v11-run-btn" disabled' in text


def test_v11_release_marker_is_current_research_build():
    marker = Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip()
    assert marker == "2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE"
