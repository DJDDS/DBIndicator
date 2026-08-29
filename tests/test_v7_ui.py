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


def _function_body(text: str, name: str) -> str:
    marker = f'function {name}('
    start = text.index(marker)
    brace = text.index('{', start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[brace + 1:i]
    raise AssertionError(f'unclosed function {name}')


def test_v7_button_controller_does_not_crash_backtest_initialization():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    backtest_ui = _function_body(text, 'updateUI')
    assert 'v7RunBtn' not in backtest_ui, (
        'updateUI must not reference the V7 early-research button; an undeclared '
        'v7RunBtn aborts the page script before the V7 click handler is registered.'
    )


def test_early_research_controller_manages_both_research_buttons():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    early_ui = _function_body(text, 'updateEarlyResearchUI')
    assert 'v7RunBtn.disabled = true' in early_ui
    assert "v7RunBtn.disabled = {{ 'true' if not logged_in else 'false' }}" in early_ui
