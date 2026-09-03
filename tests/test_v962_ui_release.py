from pathlib import Path
from app import v96_trial17, v9_playbooks

ROOT=Path(__file__).resolve().parents[1]
BUILD='2026-09-02-INSTITUTIONAL-V9.6.2-TRIAL17-PROMOTION-CONTROLS'
CURRENT='2026-09-02-INSTITUTIONAL-V9.7.2-TRIAL19-CONFOUND-INTEGRITY-CLOSURE'


def test_v962_release_marker_and_frozen_safety():
    assert v96_trial17.BUILD_ID == BUILD
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip()==CURRENT
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip()==CURRENT
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    spec=v96_trial17.trial17_spec()
    assert spec['total_oi_z_min']==1.5
    assert spec['independent_start']=='2021-09-01' and spec['independent_end']=='2023-09-01'


def test_v962_backtest_ui_separates_frozen_trial17_from_promotion_gate():
    text=(ROOT/'app/templates/backtest.html').read_text(encoding='utf-8')
    assert BUILD in text
    assert 'Trial 18 Promotion Gate' in text
    assert 'Earnings ±5 sessions' in text
    assert 'Same-day matched' in text
    assert 'India VIX' in text and 'NIFTY realized vol' in text
    assert 'Two-way clustered' in text
    assert 'DTE-matched' in text
    assert 'ATM IV' in text and 'NOT FABRICATED' in text
    assert 'ELIGIBLE FOR PREREGISTRATION' in text
    assert 'no TRADE/WATCH activation' in text


def test_v962_changelog_declares_no_retuning_and_promotion_only():
    text=(ROOT/'V9_6_2_CHANGELOG.md').read_text(encoding='utf-8')
    assert 'z >= 1.5' in text
    assert '2021-09-01' in text and '2023-09-01' in text
    assert 'promotion' in text.lower()
    assert 'earnings' in text.lower()
    assert 'same-day' in text.lower()
    assert 'two-way' in text.lower()
    assert 'ACTIVE_PLAYBOOKS = ()' in text
