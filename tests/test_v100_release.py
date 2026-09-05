from pathlib import Path
from app import v10_directional_edge as v10
from app import v9_playbooks
ROOT=Path(__file__).resolve().parents[1]


def test_v10_release_markers_and_locks():
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip() == '2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE'
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip() == v10.BUILD_ID
    assert v10.spec()['trial23'] == 'CLOSED_COMPONENT_TRIALS_FAILED_NOT_EVALUATED'
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    assert v10.spec()['production_activation'] is False
