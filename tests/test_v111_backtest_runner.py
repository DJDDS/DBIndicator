from contextlib import nullcontext


def test_v111_default_state_is_separate_from_historical_trial24_and_locked():
    from app import backtest
    state = backtest._default_v111_state()
    assert state["mode"] == "v111_development"
    assert state["build"] == "2026-09-04-INSTITUTIONAL-V11.1-DEVELOPMENT-FEASIBILITY-LAB"
    assert state["final_read"] is False
    assert state["production_activation"] is False


def test_v111_development_runner_is_closed_in_v12_even_without_runtime_state(monkeypatch, tmp_path):
    from app import backtest

    monkeypatch.setattr(backtest, "_V111_STATE_PATH", tmp_path / "state.json")
    backtest._v111_state = backtest._default_v111_state()

    class ForbiddenThread:
        def __init__(self, *a, **k):
            raise AssertionError("V11.1 development worker must not be reached in V12")
    monkeypatch.setattr(backtest.threading, "Thread", ForbiddenThread)

    out = backtest.start_v111_development_lab()
    assert out["started"] is False
    assert "closed" in out["reason"].lower()
    assert "read-only" in out["reason"].lower()
    assert out["final_read"] is False
    assert out["production_activation"] is False

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
