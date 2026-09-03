from pathlib import Path
from app import v99_volume_gate

BUILD='2026-09-03-INSTITUTIONAL-V9.9.0-TRIAL20-OOS-VOLUME-GATE'


def test_v990_build_marker_and_spec_are_frozen():
    assert v99_volume_gate.BUILD_ID == BUILD
    assert Path('RESEARCH_BUILD.txt').read_text().strip() == BUILD
    assert Path('PRODUCTION_BUILD.txt').read_text().strip() == BUILD
    assert v99_volume_gate.trial20_spec()['volume_threshold'] is None


def test_v990_backtest_ui_exposes_decisive_oos_gate_and_keeps_oi_diagnostics():
    text = Path('app/templates/backtest.html').read_text()
    assert 'V9.9 / Trial 20 · Abnormal FUTSTK Volume OOS Gate' in text
    assert 'HAR + abnormal FUTSTK volume' in text
    assert 'QLIKE' in text and 'Clark–West' in text and '1.645' in text
    assert '/api/v99/start' in text and '/api/v99/status' in text
    assert 'OI remains diagnostic' in text
    assert 'Trial 18 LOCKED' in text
