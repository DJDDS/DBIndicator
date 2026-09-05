from pathlib import Path

BUILD = "2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE"


def test_v120_research_build_and_changelog_are_locked():
    assert Path("RESEARCH_BUILD.txt").read_text(encoding="utf-8").strip() == BUILD
    text = Path("V12_CHANGELOG.md").read_text(encoding="utf-8")
    assert "Trial 25 LOCKED" in text
    assert "final 31 Trial-24 months unread" in text
    assert "NOT VALIDATED" in text
    assert "09:30" in text and "13:00" in text and "15:10" in text and "15:37" in text


def test_v120_config_has_runtime_files_and_safe_operational_defaults():
    from app import config
    assert config.V12_OPTION_SNAPSHOT_FILE == "v12_option_snapshots.jsonl"
    assert config.V12_OPTION_STATE_FILE == "v12_option_state.json"
    assert config.V12_EARNINGS_LEDGER_FILE == "v12_earnings_ledger.jsonl"
    assert config.V12_EARNINGS_STATE_FILE == "v12_earnings_state.json"
    assert config.V12_SNAPSHOT_GRACE_MINUTES == 7
    assert config.V12_DEEP_SYMBOL_LIMIT == 40


def test_v120_env_example_documents_new_runtime_paths_without_secrets():
    text = Path(".env.example").read_text(encoding="utf-8")
    for key in (
        "V12_OPTION_SNAPSHOT_FILE",
        "V12_OPTION_STATE_FILE",
        "V12_EARNINGS_LEDGER_FILE",
        "V12_EARNINGS_STATE_FILE",
    ):
        assert key in text


def test_v120_does_not_claim_trial25_runner_or_production_activation():
    text = Path("V12_CHANGELOG.md").read_text(encoding="utf-8")
    assert "No Trial-25 runner" in text
    assert "production activation remains NO" in text


def test_v120_closes_v111_rerun_and_backtest_labels_it_read_only():
    backtest_source = Path('app/backtest.py').read_text(encoding='utf-8')
    html = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'V11.1 MONTHLY BRANCH CLOSED IN V12' in backtest_source
    assert 'id="v111-run-btn" disabled' in html
    assert 'V11.1 Development Lab · CLOSED / READ-ONLY' in html
    assert '2026-09-05-INSTITUTIONAL-V12.0-OPTION-EDGE-RECORDER-TRADE-CONSOLE' in html
