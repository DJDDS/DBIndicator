from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v91_backtest_page_is_goal_focused_and_has_separate_final_button():
    text = (ROOT / "app/templates/backtest.html").read_text(encoding="utf-8")
    assert "V9.1 Goal-Focused Scanner" in text
    assert "Bull Institutional Accumulation" in text
    assert 'id="er-v91-run-btn"' in text
    assert 'id="er-v91-bear-final-btn"' in text
    assert "Run V9.1 Goal-Focused Backtest" in text
    assert "Run Frozen Bear FSB Final Test" in text
    assert "mode:'v91_fast'" in text
    assert "mode:'v91_bear_final'" in text


def test_v91_web_accepts_goal_and_final_modes():
    text = (ROOT / "app/web.py").read_text(encoding="utf-8")
    assert '"v91_fast"' in text
    assert '"v91_bear_final"' in text
