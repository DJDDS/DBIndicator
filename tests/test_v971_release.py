from pathlib import Path
from app import v97_trial19
ROOT=Path(__file__).resolve().parents[1]
BUILD='2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE'

def test_v971_release_marker_and_trial19_remain_frozen():
    assert v97_trial19.BUILD_ID == BUILD
    assert str(v97_trial19.INDEPENDENT_START.date()) == '2018-09-01'
    assert str(v97_trial19.INDEPENDENT_END.date()) == '2021-08-31'
    assert v97_trial19.TOTAL_OI_Z_MIN == 1.5
    assert v97_trial19.MIN_MATCHED_LIFT == 1.10
    assert v97_trial19.T_STAT_HURDLE == 3.0
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip() == BUILD
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip() == BUILD

def test_v971_ui_discloses_mwpl_coverage_and_keeps_trial18_locked():
    text=(ROOT/'app/templates/backtest.html').read_text()
    assert 'V9.7.2 Trial 19 · Confound &amp; Integrity Closure' in text
    assert 'MWPL months' in text and 'dates' in text
    assert 'Trial 18 LOCKED' in text
