from pathlib import Path
from app import v10_directional_edge as v10
from app import v9_playbooks
ROOT=Path(__file__).resolve().parents[1]


def test_v10_release_markers_and_locks():
    assert (ROOT/'RESEARCH_BUILD.txt').read_text().strip() == v10.BUILD_ID
    assert (ROOT/'PRODUCTION_BUILD.txt').read_text().strip() == v10.BUILD_ID
    assert v10.spec()['trial23'] == 'LOCKED_PENDING_TRIAL21_AND_22'
    assert v9_playbooks.ACTIVE_PLAYBOOKS == ()
    assert v10.spec()['production_activation'] is False
