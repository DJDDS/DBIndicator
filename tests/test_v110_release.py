from pathlib import Path


def test_v11_backtest_api_exposes_feasibility_without_outcomes():
    from app import backtest
    out = backtest.get_v11_feasibility()
    assert out["outcome_data_read"] is False
    assert out["winner"] == "TRIAL24_RESIDUAL_MOMENTUM_REPLICATION"
    assert out["trial24_registered"] is True


def test_v11_template_has_new_trial_button_and_locked_final_copy():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.0 Feasibility Competition" in text
    assert "Run Trial 24 Residual Momentum Replication" in text
    assert "final 20%" in text
    assert "/api/v11/trial24/start" in text


def test_v11_release_marker_is_current_research_build():
    marker = Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip()
    assert marker == "2026-09-04-INSTITUTIONAL-V11.0-FEASIBILITY-TRIAL24-PREREGISTRATION"
