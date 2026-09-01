from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v7_final_is_retired_from_primary_backtest_ui():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert 'V7 Frozen Final Test' not in text
    assert 'er-v7-run-btn' not in text
    assert 'r.v7_frozen' not in text
    assert 'V9.2 Goal-Focused' in text


def test_current_build_marker_is_v9():
    build_id = '2026-09-01-INSTITUTIONAL-V9.3.3-V93-INPUT-PROGRESS'
    assert build_id in (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    assert build_id in (ROOT / 'RESEARCH_BUILD.txt').read_text()


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



def test_early_research_controller_has_no_retired_v7_reference():
    text = (ROOT / 'app' / 'templates' / 'backtest.html').read_text()
    early_ui = _function_body(text, 'updateEarlyResearchUI')
    assert 'v7RunBtn' not in early_ui
