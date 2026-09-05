from pathlib import Path


def test_v111_backtest_ui_is_primary_development_lab_and_trial24_is_read_only():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V11.1 Development &amp; Feasibility Lab" in text
    assert "DEVELOPMENT ONLY &mdash; NO TRIAL 25 YET" in text
    assert "2010-01 through 2023-05" in text
    assert "FINAL 31 MONTHS UNREAD" in text
    assert "Residual momentum + volatility de-risking" in text
    assert "Liquid price momentum + identical volatility de-risking" in text
    assert "/api/v111/development/start" in text
    assert "/api/v111/development/status" in text
    assert "Trial 24 Historical Record" in text
    assert 'id="v11-run-btn" disabled' in text
    assert "startJob('/api/v11/trial24/start'" not in text


def test_v111_web_routes_and_page_state_are_wired():
    text = Path("app/web.py").read_text(encoding="utf-8")
    assert '"/api/v111/development/start"' in text
    assert '"/api/v111/development/status"' in text
    assert "backtest.start_v111_development_lab()" in text
    assert "backtest.get_v111_development_state()" in text
    assert "v111_state=backtest.get_v111_development_state()" in text


def test_v111_ui_copy_keeps_measured_cost_stress_cost_power_and_production_lock_visible():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "Measured-cost net" in text
    assert "Stress-cost net" in text
    assert "Joint power" in text
    assert "FUTSTK execution coverage" in text
    assert "production activation <strong>NO</strong>" in text


def test_v111_ui_reports_frozen_candidate_targets_stress_net_and_required_validation_months():
    text = Path("app/templates/backtest.html").read_text(encoding="utf-8")
    assert "12.49%" in text
    assert "19%" in text
    assert "m.stress_net_monthly_mean" in text
    assert "required_periods_for_minimum_primary_power" in text
