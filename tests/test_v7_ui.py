from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_backtest_ui_has_one_frozen_v7_final_test_and_predeclared_thresholds():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert 'V7 Frozen Final Test' in text
    assert 'RR_LONG_CATALYST60_15M_NEXTBAR_1D' in text
    assert 'PF ≥ 1.20' in text
    assert 'Avg net ≥ +0.15%' in text
    assert 'N ≥ 80' in text
    assert '3 of 4 chronological blocks positive' in text


def test_build_marker_is_v7_frozen():
    build_id = '2026-08-29-INSTITUTIONAL-V7-FROZEN'
    assert build_id in (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert build_id in (ROOT / 'RESEARCH_BUILD.txt').read_text()


def test_backtest_ui_renders_v7_verdict_and_chronological_blocks_from_result():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert 'r.v7_frozen' in text
    assert "verdict.verdict" in text
    assert 'chronological_blocks' in text
    assert 'Protocol mismatch' in text


def test_v7_has_dedicated_one_click_frozen_protocol_runner():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert 'id="er-v7-run-btn"' in text
    assert "timeframe:'15minute', days:'180'" in text
    assert 'Run Frozen V7 Final Test' in text
