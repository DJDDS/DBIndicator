from pathlib import Path

from app import v9_playbooks

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_is_v9_professional_playbook_console():
    text = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert "V9.2 Live F&amp;O Monitor" in text
    assert "Bull Institutional Accumulation" in text
    assert "Bear Fresh Short Buildup" in text
    assert "Bull Pullback/Reclaim" not in text
    assert "Bear Failed Breakout" not in text
    assert "Bear VWAP Retest Failure" not in text
    assert "Derivative Intelligence" in text


def test_backtest_has_dedicated_v9_one_click_runner_and_playbook_report():
    text = (ROOT / "app/templates/backtest.html").read_text(encoding="utf-8")
    assert 'id="er-v91-run-btn"' in text
    assert "Run V9.2 Diagnostic Reset" in text
    assert "v91_goal" in text
    assert "Bull Institutional Accumulation" in text
    assert "Bear Fresh Short Buildup" in text
    assert "mode:'v91_fast'" in text
    assert "timeframe:'15minute'" in text
    assert "days:'180'" in text


def test_web_accepts_v9_fast_and_v9_dashboard_payload():
    text = (ROOT / "app/web.py").read_text(encoding="utf-8")
    assert '"v9_fast"' in text
    assert "v9_playbooks.dashboard_payload" in text


def test_v9_dashboard_does_not_surface_shadow_playbook_as_production_candidate():
    row = {
        "symbol": "ABC", "v8_direction": "Bullish",
        "v9_intraday_playbook": v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION,
        "v9_intraday_score": 91, "v9_intraday_state": "TRADE CANDIDATE",
        "v9_intraday_reasons": ["Long buildup"],
        "v8_participation": 93, "v8_relative": 88, "v8_derivatives": 75,
        "v8_oi_state": "Long Buildup", "breakout_extension_atr": 0.4,
        "option_action": "OPTION BUYER EDGE", "option_edge": "HIGH",
        "option_contract": "ABCSEP100CE", "option_iv_rv_ratio": 0.95,
        "option_spread_pct": 1.2, "option_dte": 8, "option_straddle_move_pct": 2.5,
    }
    payload = v9_playbooks.dashboard_payload({"results": [row]})
    assert payload["intraday"]["bullish"] == []
    assert v9_playbooks.BULL_INSTITUTIONAL_ACCUMULATION in payload["shadow_playbooks"]
    assert payload["production_status"] == "NO VALIDATED PRODUCTION PLAYBOOK"



def test_v9_backtest_block_summary_template_literal_is_valid_javascript(tmp_path):
    import re
    import shutil
    import subprocess
    import pytest
    from jinja2 import Environment, FileSystemLoader

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for rendered-JavaScript syntax regression check")

    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"))
    html = env.get_template("backtest.html").render(
        logged_in=True, watchlist_count=211,
        valid_timeframes=["15minute", "4hour"], default_timeframe="15minute",
        bt_days_default=180, bt_days_min=5, bt_days_max=365,
        param_defs=[], filter_defs=[], default_params=[], default_required=1,
        backtest_day_bounds={"15minute": [5, 365, 90], "4hour": [5, 365, 180]},
        state={"status": "idle"}, early_research_state={"status": "idle", "progress": {}},
        ablation_state={"status": "idle"},
    )
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    js_path = tmp_path / "backtest-rendered.js"
    js_path.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
