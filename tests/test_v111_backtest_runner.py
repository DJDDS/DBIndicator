from contextlib import nullcontext


def test_v111_default_state_is_separate_from_historical_trial24_and_locked():
    from app import backtest
    state = backtest._default_v111_state()
    assert state["mode"] == "v111_development"
    assert state["build"] == "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB"
    assert state["final_read"] is False
    assert state["production_activation"] is False


def test_v111_background_runner_uses_development_loader_and_never_sets_final_or_production(monkeypatch, tmp_path):
    from app import backtest

    fake_inputs = {"data_readiness": True, "meta": {}, "monthly_returns": None, "factors": None, "membership": None}
    fake_result = {
        "build": "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB",
        "status": "DEVELOPMENT_ONLY_NO_TRIAL25_YET",
        "final_read": False,
        "production_activation": False,
        "trial25_run": False,
    }
    monkeypatch.setattr(backtest, "_V111_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(backtest, "_V111_WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(backtest.v11_monthly_data, "build_trial24_inputs", lambda *a, **k: fake_inputs)
    monkeypatch.setattr(backtest.v111_lab, "run_development_lab", lambda *a, **k: fake_result)
    monkeypatch.setattr(backtest.research_runtime, "begin_research", lambda *a, **k: None)
    monkeypatch.setattr(backtest.research_runtime, "end_research", lambda *a, **k: None)
    monkeypatch.setattr(backtest.research_runtime, "release_memory_pressure", lambda *a, **k: None)
    monkeypatch.setattr(backtest.research_runtime, "research_slot", lambda *a, **k: nullcontext())

    class ImmediateThread:
        def __init__(self, target, daemon=True): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(backtest.threading, "Thread", ImmediateThread)

    backtest._v111_state = backtest._default_v111_state()
    started = backtest.start_v111_development_lab()
    assert started["started"] is True
    state = backtest.get_v111_development_state()
    assert state["status"] == "done"
    assert state["result"]["trial25_run"] is False
    assert state["final_read"] is False
    assert state["production_activation"] is False


def test_v111_permanently_refuses_historical_trial24_rerun_even_without_persisted_state(monkeypatch):
    from app import backtest
    backtest._v11_state = backtest._default_v11_state()

    class ForbiddenThread:
        def __init__(self, *a, **k):
            raise AssertionError("historical Trial 24 runner must not be reached in V11.1")
    monkeypatch.setattr(backtest.threading, "Thread", ForbiddenThread)

    out = backtest.start_v11_trial24()
    assert out["started"] is False
    assert "historical" in out["reason"].lower()
    assert "read-only" in out["reason"].lower()
