import pandas as pd

from app import index_option_stage2_history


def _bars():
    days = []
    for day in ("2026-02-27", "2026-03-02"):
        idx = pd.date_range(f"{day} 09:15", periods=375, freq="min", tz="Asia/Kolkata")
        frame = pd.DataFrame(
            {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0},
            index=idx,
        )
        # Persistent bullish expansion after 10:00 gives immediate/double/acceptance signals.
        mask = frame.index >= pd.Timestamp(f"{day} 10:00", tz="Asia/Kolkata")
        frame.loc[mask, ["open", "high", "low", "close"]] = [100.5, 100.7, 100.4, 100.6]
        days.append(frame)
    return pd.concat(days).sort_index()


def test_stage2_checkpoint_resume_skips_completed_families(monkeypatch, tmp_path):
    monkeypatch.setattr(index_option_stage2_history, "OPENING_RANGES", (15, 30))
    monkeypatch.setattr(index_option_stage2_history, "TRIGGER_WINDOWS", (1,))

    first_progress = []
    first = index_option_stage2_history.run_stage2_from_bars(
        _bars(),
        output_dir=tmp_path,
        progress_callback=first_progress.append,
    )

    checkpoints = sorted((tmp_path / "checkpoints").glob("or*_trig*.csv"))
    assert len(checkpoints) == 2
    assert all(item["resumed"] is False for item in first_progress)
    assert first["manifest"]["checkpointing"]["families_completed"] == 2
    assert first["manifest"]["checkpointing"]["families_expected"] == 2

    second_progress = []
    second = index_option_stage2_history.run_stage2_from_bars(
        _bars(),
        output_dir=tmp_path,
        progress_callback=second_progress.append,
    )

    assert len(second_progress) == 2
    assert all(item["resumed"] is True for item in second_progress)
    assert second["manifest"]["signal_rows"] == first["manifest"]["signal_rows"]


def test_stage2_manifest_keeps_geopolitical_split_diagnostic_only(monkeypatch, tmp_path):
    monkeypatch.setattr(index_option_stage2_history, "OPENING_RANGES", (15,))
    monkeypatch.setattr(index_option_stage2_history, "TRIGGER_WINDOWS", (1,))

    result = index_option_stage2_history.run_stage2_from_bars(
        _bars(),
        output_dir=tmp_path,
    )

    diagnostic = result["manifest"]["geopolitical_diagnostic"]
    assert diagnostic["purpose"] == "diagnostic_only_not_signal_filter_or_tuning_input"
    assert result["manifest"]["production_deployed"] is False
    assert result["manifest"]["v12_recorder_used_for_tuning"] is False
