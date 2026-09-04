from app import early_research


def test_v93_lab_does_not_run_v92_goal_report(monkeypatch):
    calls = {"v92": 0, "v93": 0}

    def forbidden_v92(*args, **kwargs):
        calls["v92"] += 1
        raise AssertionError("V9.3 must not implicitly execute V9.2 goal-focused research")

    def fake_v93(rows, run_context=None):
        calls["v93"] += 1
        return {"build_id": "TEST-V93", "event_count": len(rows)}

    monkeypatch.setattr(early_research, "v91_goal_report", forbidden_v92)
    from app import v93_component_lab
    monkeypatch.setattr(v93_component_lab, "build_report", fake_v93)

    result = early_research.aggregate_v91_compact_events(
        [], confirmation_summary={}, run_context={"research_mode": "v93_lab"}
    )

    assert calls == {"v92": 0, "v93": 1}
    assert "v91_goal" not in result
    assert result["v93_component_lab"]["build_id"] == "TEST-V93"


def test_v92_manual_mode_still_runs_v92_goal_report(monkeypatch):
    calls = {"v92": 0}

    def fake_v92(*args, **kwargs):
        calls["v92"] += 1
        return {"build_id": "TEST-V92"}

    monkeypatch.setattr(early_research, "v91_goal_report", fake_v92)
    result = early_research.aggregate_v91_compact_events(
        [], confirmation_summary={}, run_context={"research_mode": "v91_fast"}
    )

    assert calls["v92"] == 1
    assert result["v91_goal"]["build_id"] == "TEST-V92"
    assert "v93_component_lab" not in result


def test_v93_backtest_copy_marks_v92_as_manual_diagnostic_only():
    from pathlib import Path
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert 'V10.2 Research Integrity &amp; Feasibility Repair is the primary research architecture' in text
    assert 'V9.4 remains visible as the completed measurement/audit path' in text
    assert 'V9.2 remains a manual diagnostic only' in text


def test_research_progress_identifies_the_explicit_job_mode():
    from pathlib import Path
    text = Path('app/templates/backtest.html').read_text(encoding='utf-8')
    assert "v93_lab: 'V9.3 Anticipation Lab'" in text
    assert "v91_fast: 'V9.2 Diagnostic Reset'" in text
    assert 'modeLabel' in text
