from pathlib import Path

BUILD = "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB"


def test_v111_historical_module_identity_remains_frozen_under_newer_release_marker():
    from app import v111_development, v111_lab
    assert Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip().startswith("2026-09-05-INSTITUTIONAL-V12.0-")
    assert v111_development.BUILD_ID == BUILD
    assert v111_lab.BUILD_ID == BUILD


def test_v111_changelog_records_no_trial25_no_final_no_production_contract():
    text = Path("V11_1_CHANGELOG.md").read_text(encoding="utf-8")
    assert "not Trial 25" in text
    assert "final 31 Trial-24 months unread" in text
    assert "80% prospective power" in text
    assert "production_activation = false" in text
    assert "cash P&L as futures P&L" in text


def test_historical_trial24_build_identity_remains_frozen_for_record_reproducibility():
    from app import v11_research
    assert v11_research.BUILD_ID == "2026-09-04-INSTITUTIONAL-V11.0.5-STRICT-REQUIRED-WINDOW-FACTOR-CONTRACT"
    assert v11_research.trial24_spec()["production_activation"] is False


def test_v111_changelog_records_both_external_volatility_targets_without_a_target_sweep():
    text = Path("V11_1_CHANGELOG.md").read_text(encoding="utf-8")
    assert "12.49%" in text
    assert "19%" in text
    assert "no volatility-target sweep" in text.lower()
